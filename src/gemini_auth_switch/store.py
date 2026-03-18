from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import fcntl
from functools import lru_cache
import hashlib
import json
import os
import pty
import re
import select
import signal
import shutil
import struct
import subprocess
import tempfile
import termios
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .paths import GeminiPaths


PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._@+-]+$")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OSC_ESCAPE_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")
MODEL_USAGE_LINE_RE = re.compile(r"^(?P<model>[A-Za-z0-9][A-Za-z0-9._-]*)\s+(?P<requests>-|\d+)\s+(?P<remaining>.+)$")
PERCENT_REMAINING_RE = re.compile(
    r"(?P<percent>\d+(?:\.\d+)?)%(?:\s+resets in\s+(?P<reset>.+))?$"
)
PICK_DISALLOWED_CHECK_STATUSES = {
    "validation_required",
    "auth_change_required",
    "error",
    "timeout",
}
DEFAULT_CODE_ASSIST_ENDPOINT = "https://cloudcode-pa.googleapis.com"
DEFAULT_CODE_ASSIST_API_VERSION = "v1internal"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
QUOTA_SOURCE_API = "api"
QUOTA_SOURCE_STATS = "stats"
DEFAULT_QUOTA_STALE_SECONDS = 300.0
DEFAULT_CANDIDATE_REFRESH_LIMIT = 2
DEFAULT_TRANSIENT_REFRESH_COOLDOWN_SECONDS = 180.0
DEFAULT_RATE_LIMIT_REFRESH_COOLDOWN_SECONDS = 300.0
DEFAULT_NONTRANSIENT_REFRESH_COOLDOWN_SECONDS = 21600.0
MAX_REFRESH_COOLDOWN_SECONDS = 43200.0
JS_STRING_CONSTANT_RE = re.compile(r"(?P<name>[A-Z0-9_]+)\s*=\s*'(?P<value>[^']+)'")


class PoolError(RuntimeError):
    pass


class ApiRequestError(PoolError):
    def __init__(
        self,
        status_code: int | None,
        detail: str,
        payload: Any = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.payload = payload
        self.headers = headers or {}


@dataclass
class ProfileSummary:
    name: str
    email: str | None
    created_at: str
    updated_at: str
    is_current: bool
    last_check_status: str | None = None
    last_check_detail: str | None = None
    last_check_returncode: int | None = None
    last_checked_at: str | None = None


@dataclass
class ProfileCheckResult:
    name: str
    email: str | None
    status: str
    detail: str
    returncode: int | None
    checked_at: str


@dataclass
class ModelUsageStat:
    model: str
    requests: str
    usage_remaining: str
    remaining_percent: float | None = None
    reset_in: str | None = None
    reset_at: str | None = None


@dataclass
class ProfileStatsResult:
    name: str
    email: str | None
    status: str
    detail: str
    checked_at: str
    auth_method: str | None = None
    tier: str | None = None
    tier_id: str | None = None
    session_id: str | None = None
    usage_label: str | None = None
    source: str | None = None
    project_id: str | None = None
    last_refresh_attempt_at: str | None = None
    last_refresh_status: str | None = None
    last_refresh_detail: str | None = None
    blocked_until: str | None = None
    blocked_reason: str | None = None
    failure_streak: int = 0
    retry_after_seconds: float | None = None
    models: list[ModelUsageStat] = field(default_factory=list)

    def lowest_remaining_percent(self) -> float | None:
        values = [item.remaining_percent for item in self.models if item.remaining_percent is not None]
        if not values:
            return None
        return min(values)

    def model_count(self) -> int:
        return len(self.models)


@dataclass
class ProfilePickResult:
    name: str
    email: str | None
    matched_model: str
    usage_remaining: str
    remaining_percent: float
    quota_checked_at: str
    health_status: str | None = None
    health_checked_at: str | None = None
    tier: str | None = None
    usage_label: str | None = None
    is_current: bool = False
    match_terms: list[str] = field(default_factory=list)


@dataclass
class AutoSwitchDecision:
    action: str
    reason: str
    current_profile: str | None
    selected: ProfilePickResult
    threshold_percent: float


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_reset_in(reset_time_str: str | None) -> str | None:
    reset_at = parse_iso_datetime(reset_time_str)
    if reset_at is None:
        return None
    remaining_seconds = int((reset_at - datetime.now(timezone.utc)).total_seconds())
    if remaining_seconds <= 0:
        return "0m"
    hours, remainder = divmod(remaining_seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def future_time_iso(seconds: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(seconds, 0.0))
    ).replace(microsecond=0).isoformat()


@lru_cache(maxsize=4)
def load_official_gemini_cli_constants(gemini_bin: str) -> dict[str, str]:
    env_client_id = os.environ.get("GSWITCH_OAUTH_CLIENT_ID")
    env_client_secret = os.environ.get("GSWITCH_OAUTH_CLIENT_SECRET")
    candidate = Path(gemini_bin)
    if candidate.is_file():
        gemini_entry = candidate.resolve()
    else:
        resolved = shutil.which(gemini_bin)
        if not resolved:
            if env_client_id and env_client_secret:
                return {
                    "endpoint": DEFAULT_CODE_ASSIST_ENDPOINT,
                    "api_version": DEFAULT_CODE_ASSIST_API_VERSION,
                    "client_id": env_client_id,
                    "client_secret": env_client_secret,
                }
            raise PoolError(
                "cannot resolve Gemini CLI binary to discover OAuth client constants; "
                "set GSWITCH_OAUTH_CLIENT_ID and GSWITCH_OAUTH_CLIENT_SECRET to override"
            )
        gemini_entry = Path(resolved).resolve()

    package_root = gemini_entry.parent.parent
    code_assist_dir = package_root / "node_modules" / "@google" / "gemini-cli-core" / "dist" / "src" / "code_assist"
    oauth2_file = code_assist_dir / "oauth2.js"
    server_file = code_assist_dir / "server.js"

    constants = {
        "endpoint": DEFAULT_CODE_ASSIST_ENDPOINT,
        "api_version": DEFAULT_CODE_ASSIST_API_VERSION,
    }
    if env_client_id and env_client_secret:
        constants["client_id"] = env_client_id
        constants["client_secret"] = env_client_secret
    for path in (oauth2_file, server_file):
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in JS_STRING_CONSTANT_RE.finditer(text):
            name = match.group("name")
            value = match.group("value")
            if name == "CODE_ASSIST_ENDPOINT":
                constants["endpoint"] = value
            elif name == "CODE_ASSIST_API_VERSION":
                constants["api_version"] = value
            elif name == "OAUTH_CLIENT_ID":
                constants["client_id"] = value
            elif name == "OAUTH_CLIENT_SECRET":
                constants["client_secret"] = value
    if not constants.get("client_id") or not constants.get("client_secret"):
        raise PoolError(
            "could not discover Gemini OAuth client constants from the installed CLI; "
            "set GSWITCH_OAUTH_CLIENT_ID and GSWITCH_OAUTH_CLIENT_SECRET to override"
        )
    return constants


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(payload)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def canonical_creds_fingerprint(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def compact_output(text: str, limit: int = 160) -> str:
    normalized = " ".join(text.split())
    if not normalized:
        return "-"
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def normalize_subprocess_output(value: str | bytes | bytearray | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


def sanitize_terminal_output(text: str | bytes | None) -> str:
    normalized = normalize_subprocess_output(text).replace("\r", "\n")
    normalized = OSC_ESCAPE_RE.sub("", normalized)
    normalized = ANSI_ESCAPE_RE.sub("", normalized)
    normalized = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", normalized)

    lines: list[str] = []
    previous_blank = False
    for raw_line in normalized.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            if not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        lines.append(line)
        previous_blank = False
    return "\n".join(lines).strip()


def configure_pty(slave_fd: int, rows: int = 40, cols: int = 200) -> None:
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)


def parse_model_remaining(text: str) -> tuple[float | None, str | None]:
    match = PERCENT_REMAINING_RE.search(text)
    if not match:
        return None, None
    reset_in = match.group("reset")
    return float(match.group("percent")), reset_in.strip() if reset_in else None


def operation_locked(method: Callable[..., Any]) -> Callable[..., Any]:
    def wrapper(self: "GeminiAuthPool", *args: Any, **kwargs: Any) -> Any:
        with self.auth_operation_lock():
            return method(self, *args, **kwargs)

    return wrapper


class GeminiAuthPool:
    def __init__(self, paths: GeminiPaths):
        self.paths = paths
        self._operation_lock_fd: int | None = None
        self._operation_lock_depth = 0

    @contextmanager
    def auth_operation_lock(self) -> Any:
        self.ensure_layout()
        if self._operation_lock_depth == 0:
            lock_fd = os.open(self.paths.operation_lock_file, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.chmod(self.paths.operation_lock_file, 0o600)
            except OSError:
                pass
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._operation_lock_fd = lock_fd
            try:
                os.ftruncate(lock_fd, 0)
                os.write(lock_fd, f"pid={os.getpid()} acquired_at={utc_now_iso()}\n".encode("utf-8"))
                os.fsync(lock_fd)
            except OSError:
                pass
        self._operation_lock_depth += 1
        try:
            yield
        finally:
            self._operation_lock_depth -= 1
            if self._operation_lock_depth == 0:
                lock_fd = self._operation_lock_fd
                self._operation_lock_fd = None
                if lock_fd is not None:
                    try:
                        try:
                            os.ftruncate(lock_fd, 0)
                        except OSError:
                            pass
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_fd)

    def ensure_layout(self) -> None:
        self.paths.gemini_dir.mkdir(parents=True, exist_ok=True)
        self.paths.profiles_dir.mkdir(parents=True, exist_ok=True)

    def validate_profile_name(self, name: str) -> str:
        if not name:
            raise PoolError("profile name is required")
        if not PROFILE_NAME_RE.match(name):
            raise PoolError(
                "invalid profile name: use only letters, digits, dot, underscore, plus, dash, or @"
            )
        if name in {".", ".."}:
            raise PoolError("invalid profile name")
        return name

    def load_live_creds(self) -> dict[str, Any] | None:
        return load_json(self.paths.live_creds_file, None)

    def require_live_creds(self) -> dict[str, Any]:
        creds = self.load_live_creds()
        if not creds:
            raise PoolError(f"missing live Gemini credentials: {self.paths.live_creds_file}")
        return creds

    def load_live_email(self) -> str | None:
        data = load_json(self.paths.google_accounts_file, {"active": None, "old": []})
        value = data.get("active")
        return value if isinstance(value, str) and value else None

    def load_selected_auth_type(self) -> str | None:
        settings = load_json(self.paths.settings_file, {})
        if not isinstance(settings, dict):
            return None
        security = settings.get("security")
        if not isinstance(security, dict):
            return None
        auth = security.get("auth")
        if not isinstance(auth, dict):
            return None
        selected_type = auth.get("selectedType")
        return selected_type if isinstance(selected_type, str) and selected_type else None

    def load_state(self) -> dict[str, Any]:
        return load_json(self.paths.state_file, {"active_profile": None, "last_switched_at": None})

    def write_state(self, active_profile: str | None) -> None:
        state = {
            "active_profile": active_profile,
            "last_switched_at": utc_now_iso(),
        }
        write_json(self.paths.state_file, state)

    def load_check_state(self) -> dict[str, Any]:
        state = load_json(self.paths.check_state_file, {"profiles": {}, "updated_at": None})
        if not isinstance(state, dict):
            return {"profiles": {}, "updated_at": None}
        profiles = state.get("profiles")
        if not isinstance(profiles, dict):
            profiles = {}
        updated_at = state.get("updated_at")
        if updated_at is not None and not isinstance(updated_at, str):
            updated_at = None
        return {"profiles": profiles, "updated_at": updated_at}

    def load_check_result(self, profile_name: str) -> dict[str, Any] | None:
        profiles = self.load_check_state()["profiles"]
        entry = profiles.get(profile_name)
        return entry if isinstance(entry, dict) else None

    def load_quota_state(self) -> dict[str, Any]:
        state = load_json(self.paths.quota_state_file, {"profiles": {}, "updated_at": None})
        if not isinstance(state, dict):
            return {"profiles": {}, "updated_at": None}
        profiles = state.get("profiles")
        if not isinstance(profiles, dict):
            profiles = {}
        updated_at = state.get("updated_at")
        if updated_at is not None and not isinstance(updated_at, str):
            updated_at = None
        return {"profiles": profiles, "updated_at": updated_at}

    def parse_stored_model_usage(self, payload: Any) -> ModelUsageStat | None:
        if not isinstance(payload, dict):
            return None
        model = payload.get("model")
        requests = payload.get("requests")
        usage_remaining = payload.get("usage_remaining")
        if not isinstance(model, str) or not isinstance(requests, str):
            return None
        if not isinstance(usage_remaining, str):
            return None
        remaining_percent = payload.get("remaining_percent")
        if isinstance(remaining_percent, int):
            remaining_percent = float(remaining_percent)
        if remaining_percent is not None and not isinstance(remaining_percent, float):
            remaining_percent = None
        reset_in = payload.get("reset_in")
        if reset_in is not None and not isinstance(reset_in, str):
            reset_in = None
        reset_at = payload.get("reset_at")
        if reset_at is not None and not isinstance(reset_at, str):
            reset_at = None
        return ModelUsageStat(
            model=model,
            requests=requests,
            usage_remaining=usage_remaining,
            remaining_percent=remaining_percent,
            reset_in=reset_in,
            reset_at=reset_at,
        )

    def load_quota_result(self, profile_name: str) -> ProfileStatsResult | None:
        profiles = self.load_quota_state()["profiles"]
        entry = profiles.get(profile_name)
        if not isinstance(entry, dict):
            return None
        models_payload = entry.get("models")
        models: list[ModelUsageStat] = []
        if isinstance(models_payload, list):
            for item in models_payload:
                parsed = self.parse_stored_model_usage(item)
                if parsed is not None:
                    models.append(parsed)
        email = entry.get("email")
        status = entry.get("status")
        detail = entry.get("detail")
        checked_at = entry.get("checked_at")
        auth_method = entry.get("auth_method")
        tier = entry.get("tier")
        tier_id = entry.get("tier_id")
        session_id = entry.get("session_id")
        usage_label = entry.get("usage_label")
        source = entry.get("source")
        project_id = entry.get("project_id")
        last_refresh_attempt_at = entry.get("last_refresh_attempt_at")
        last_refresh_status = entry.get("last_refresh_status")
        last_refresh_detail = entry.get("last_refresh_detail")
        blocked_until = entry.get("blocked_until")
        blocked_reason = entry.get("blocked_reason")
        failure_streak = entry.get("failure_streak")
        return ProfileStatsResult(
            name=profile_name,
            email=email if isinstance(email, str) else None,
            status=status if isinstance(status, str) else "-",
            detail=detail if isinstance(detail, str) else "invalid cached quota data",
            checked_at=checked_at if isinstance(checked_at, str) else "-",
            auth_method=auth_method if isinstance(auth_method, str) else None,
            tier=tier if isinstance(tier, str) else None,
            tier_id=tier_id if isinstance(tier_id, str) else None,
            session_id=session_id if isinstance(session_id, str) else None,
            usage_label=usage_label if isinstance(usage_label, str) else None,
            source=source if isinstance(source, str) else QUOTA_SOURCE_STATS,
            project_id=project_id if isinstance(project_id, str) else None,
            last_refresh_attempt_at=(
                last_refresh_attempt_at if isinstance(last_refresh_attempt_at, str) else None
            ),
            last_refresh_status=(
                last_refresh_status if isinstance(last_refresh_status, str) else None
            ),
            last_refresh_detail=(
                last_refresh_detail if isinstance(last_refresh_detail, str) else None
            ),
            blocked_until=blocked_until if isinstance(blocked_until, str) else None,
            blocked_reason=blocked_reason if isinstance(blocked_reason, str) else None,
            failure_streak=failure_streak if isinstance(failure_streak, int) else 0,
            models=models,
        )

    def make_missing_quota_result(self, name: str, email: str | None) -> ProfileStatsResult:
        return ProfileStatsResult(
            name=name,
            email=email,
            status="-",
            detail="no cached quota; run gswitch stats or gswitch stats-all",
            checked_at="-",
        )

    def write_check_result(self, result: ProfileCheckResult) -> None:
        state = self.load_check_state()
        profiles = state["profiles"]
        profiles[result.name] = {
            "status": result.status,
            "detail": result.detail,
            "returncode": result.returncode,
            "checked_at": result.checked_at,
            "email": result.email,
        }
        state["updated_at"] = result.checked_at
        write_json(self.paths.check_state_file, state)

    def write_quota_result(self, result: ProfileStatsResult) -> None:
        state = self.load_quota_state()
        profiles = state["profiles"]
        profiles[result.name] = {
            "status": result.status,
            "detail": result.detail,
            "checked_at": result.checked_at,
            "email": result.email,
            "auth_method": result.auth_method,
            "tier": result.tier,
            "tier_id": result.tier_id,
            "session_id": result.session_id,
            "usage_label": result.usage_label,
            "source": result.source,
            "project_id": result.project_id,
            "last_refresh_attempt_at": result.last_refresh_attempt_at,
            "last_refresh_status": result.last_refresh_status,
            "last_refresh_detail": result.last_refresh_detail,
            "blocked_until": result.blocked_until,
            "blocked_reason": result.blocked_reason,
            "failure_streak": result.failure_streak,
            "models": [
                {
                    "model": item.model,
                    "requests": item.requests,
                    "usage_remaining": item.usage_remaining,
                    "remaining_percent": item.remaining_percent,
                    "reset_in": item.reset_in,
                    "reset_at": item.reset_at,
                }
                for item in result.models
            ],
        }
        state["updated_at"] = result.last_refresh_attempt_at or result.checked_at
        write_json(self.paths.quota_state_file, state)

    def drop_check_result(self, profile_name: str) -> None:
        state = self.load_check_state()
        profiles = state["profiles"]
        if profile_name not in profiles:
            return
        del profiles[profile_name]
        state["updated_at"] = utc_now_iso()
        write_json(self.paths.check_state_file, state)

    def drop_quota_result(self, profile_name: str) -> None:
        state = self.load_quota_state()
        profiles = state["profiles"]
        if profile_name not in profiles:
            return
        del profiles[profile_name]
        state["updated_at"] = utc_now_iso()
        write_json(self.paths.quota_state_file, state)

    def update_google_accounts(self, active_email: str | None) -> None:
        data = load_json(self.paths.google_accounts_file, {"active": None, "old": []})
        current_active = data.get("active")
        old = data.get("old")
        if not isinstance(old, list):
            old = []
        old = [item for item in old if isinstance(item, str)]

        if isinstance(current_active, str) and current_active and current_active != active_email:
            if current_active not in old:
                old.append(current_active)

        if active_email:
            old = [item for item in old if item != active_email]

        payload = {
            "active": active_email,
            "old": old,
        }
        write_json(self.paths.google_accounts_file, payload)

    def clear_token_caches(self) -> None:
        for path in (self.paths.token_cache_v1, self.paths.token_cache_v2):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def build_gemini_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GEMINI_FORCE_FILE_STORAGE"] = "true"
        env["GEMINI_FORCE_ENCRYPTED_FILE_STORAGE"] = "false"
        return env

    def list_profile_names(self) -> list[str]:
        self.ensure_layout()
        names = [entry.name for entry in self.paths.profiles_dir.iterdir() if entry.is_dir()]
        return sorted(names)

    def load_profile_meta(self, name: str) -> dict[str, Any]:
        meta = load_json(self.paths.profile_meta_file(name), {})
        if not isinstance(meta, dict):
            raise PoolError(f"invalid profile metadata for {name}")
        return meta

    def load_profile_creds(self, name: str) -> dict[str, Any]:
        creds = load_json(self.paths.profile_creds_file(name), None)
        if not creds:
            raise PoolError(f"missing credentials for profile {name}")
        return creds

    def profile_exists(self, name: str) -> bool:
        return self.paths.profile_dir(name).is_dir()

    def infer_profile_name(self, explicit_name: str | None) -> str:
        if explicit_name:
            return self.validate_profile_name(explicit_name)
        live_email = self.load_live_email()
        if live_email:
            return self.validate_profile_name(live_email)
        stamp = datetime.now(timezone.utc).strftime("profile-%Y%m%d-%H%M%S")
        return stamp

    def current_profile_name(self) -> str | None:
        state = self.load_state()
        active = state.get("active_profile")
        if isinstance(active, str) and self.profile_exists(active):
            return active

        live_creds = self.load_live_creds()
        if not live_creds:
            return None
        fingerprint = canonical_creds_fingerprint(live_creds)
        live_refresh_token = live_creds.get("refresh_token")

        for name in self.list_profile_names():
            meta = self.load_profile_meta(name)
            if meta.get("fingerprint") == fingerprint:
                return name
            profile_creds = self.load_profile_creds(name)
            if live_refresh_token and profile_creds.get("refresh_token") == live_refresh_token:
                return name
        return None

    def current_summary(self) -> dict[str, Any]:
        live_creds = self.load_live_creds()
        return {
            "profile": self.current_profile_name(),
            "email": self.load_live_email(),
            "has_live_creds": bool(live_creds),
            "live_creds_path": str(self.paths.live_creds_file),
            "selected_auth_type": self.load_selected_auth_type(),
        }

    def diagnostics_summary(self) -> dict[str, Any]:
        current = self.current_summary()
        current.update(
            {
                "token_cache_v1_exists": self.paths.token_cache_v1.exists(),
                "token_cache_v2_exists": self.paths.token_cache_v2.exists(),
                "force_file_storage": os.environ.get("GEMINI_FORCE_FILE_STORAGE") == "true",
                "force_encrypted_file_storage": os.environ.get(
                    "GEMINI_FORCE_ENCRYPTED_FILE_STORAGE"
                )
                == "true",
            }
        )
        return current

    def list_profiles(self) -> list[ProfileSummary]:
        current_name = self.current_profile_name()
        check_profiles = self.load_check_state()["profiles"]
        summaries: list[ProfileSummary] = []
        for name in self.list_profile_names():
            meta = self.load_profile_meta(name)
            check_entry = check_profiles.get(name)
            if not isinstance(check_entry, dict):
                check_entry = {}
            check_status = check_entry.get("status")
            check_detail = check_entry.get("detail")
            check_returncode = check_entry.get("returncode")
            checked_at = check_entry.get("checked_at")
            summaries.append(
                ProfileSummary(
                    name=name,
                    email=meta.get("email"),
                    created_at=meta.get("created_at", ""),
                    updated_at=meta.get("updated_at", ""),
                    is_current=name == current_name,
                    last_check_status=check_status if isinstance(check_status, str) else None,
                    last_check_detail=check_detail if isinstance(check_detail, str) else None,
                    last_check_returncode=(
                        check_returncode if isinstance(check_returncode, int) else None
                    ),
                    last_checked_at=checked_at if isinstance(checked_at, str) else None,
                )
            )
        return summaries

    def quota_profile(self, name: str | None = None) -> ProfileStatsResult:
        if name is None:
            current = self.current_profile_name()
            if current is None:
                raise PoolError("no current saved profile")
            profile_name = current
        else:
            profile_name = self.validate_profile_name(name)
        if not self.profile_exists(profile_name):
            raise PoolError(f"unknown profile: {profile_name}")
        result = self.load_quota_result(profile_name)
        if result is not None:
            return result
        meta = self.load_profile_meta(profile_name)
        return self.make_missing_quota_result(profile_name, meta.get("email"))

    def quota_all_profiles(self) -> list[ProfileStatsResult]:
        names = self.list_profile_names()
        if not names:
            raise PoolError("no saved profiles")
        results: list[ProfileStatsResult] = []
        for name in names:
            result = self.load_quota_result(name)
            if result is not None:
                results.append(result)
                continue
            meta = self.load_profile_meta(name)
            results.append(self.make_missing_quota_result(name, meta.get("email")))
        return results

    def normalize_pick_match_terms(self, match_terms: list[str] | None) -> list[str]:
        normalized: list[str] = []
        for raw_term in match_terms or []:
            term = raw_term.strip().lower()
            if not term:
                raise PoolError("match term must not be empty")
            normalized.append(term)
        return normalized

    def normalize_avoid_profiles(self, avoid_profiles: list[str] | None) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_name in avoid_profiles or []:
            name = self.validate_profile_name(raw_name)
            if not self.profile_exists(name):
                raise PoolError(f"unknown profile: {name}")
            if name in seen:
                continue
            normalized.append(name)
            seen.add(name)
        return normalized

    def validate_remaining_threshold(self, threshold_percent: float) -> float:
        if threshold_percent < 0 or threshold_percent > 100:
            raise PoolError("min remaining threshold must be between 0 and 100")
        return threshold_percent

    def model_matches_terms(self, model: str, match_terms: list[str]) -> bool:
        normalized_model = model.lower()
        return all(term in normalized_model for term in match_terms)

    def health_status_is_pick_eligible(self, status: str | None) -> bool:
        if status is None:
            return True
        return status not in PICK_DISALLOWED_CHECK_STATUSES

    def matching_models(
        self,
        quota_result: ProfileStatsResult,
        match_terms: list[str],
    ) -> list[ModelUsageStat]:
        return [
            item
            for item in quota_result.models
            if item.remaining_percent is not None
            and (not match_terms or self.model_matches_terms(item.model, match_terms))
        ]

    def make_pick_candidate(
        self,
        name: str,
        normalized_terms: list[str],
        current_name: str | None,
        check_profiles: dict[str, Any],
        avoid_names: set[str] | None = None,
    ) -> ProfilePickResult | None:
        if avoid_names and name in avoid_names:
            return None
        quota_result = self.load_quota_result(name)
        if quota_result is None or quota_result.status != "ok":
            return None

        check_entry = check_profiles.get(name)
        if not isinstance(check_entry, dict):
            check_entry = {}
        health_status = check_entry.get("status")
        if health_status is not None and not isinstance(health_status, str):
            health_status = None
        if not self.health_status_is_pick_eligible(health_status):
            return None
        health_checked_at = check_entry.get("checked_at")
        if health_checked_at is not None and not isinstance(health_checked_at, str):
            health_checked_at = None

        matched_models = self.matching_models(quota_result, normalized_terms)
        if not matched_models:
            return None

        best_model = max(
            matched_models,
            key=lambda item: (item.remaining_percent or -1.0, item.model),
        )
        return ProfilePickResult(
            name=name,
            email=quota_result.email,
            matched_model=best_model.model,
            usage_remaining=best_model.usage_remaining,
            remaining_percent=best_model.remaining_percent or 0.0,
            quota_checked_at=quota_result.checked_at,
            health_status=health_status,
            health_checked_at=health_checked_at,
            tier=quota_result.tier,
            usage_label=quota_result.usage_label,
            is_current=name == current_name,
            match_terms=normalized_terms,
        )

    def pick_candidates(
        self,
        match_terms: list[str] | None = None,
        avoid_profiles: list[str] | None = None,
    ) -> list[ProfilePickResult]:
        names = self.list_profile_names()
        if not names:
            raise PoolError("no saved profiles")

        normalized_terms = self.normalize_pick_match_terms(match_terms)
        avoid_names = set(self.normalize_avoid_profiles(avoid_profiles))
        check_profiles = self.load_check_state()["profiles"]
        current_name = self.current_profile_name()
        candidates: list[ProfilePickResult] = []
        for name in names:
            candidate = self.make_pick_candidate(
                name=name,
                normalized_terms=normalized_terms,
                current_name=current_name,
                check_profiles=check_profiles,
                avoid_names=avoid_names,
            )
            if candidate is not None:
                candidates.append(candidate)

        if candidates:
            return candidates
        if normalized_terms:
            filters = ",".join(normalized_terms)
            raise PoolError(
                f"no eligible cached quota match for filters: {filters}; "
                "run gswitch stats or gswitch stats-all first"
            )
        raise PoolError(
            "no eligible cached quota candidates; run gswitch stats or gswitch stats-all first"
        )

    def pick_profile(
        self,
        match_terms: list[str] | None = None,
        avoid_profiles: list[str] | None = None,
    ) -> ProfilePickResult:
        candidates = self.pick_candidates(match_terms, avoid_profiles=avoid_profiles)
        return max(candidates, key=self.candidate_sort_key)

    def maybe_refresh_pick_candidate(
        self,
        profile_name: str,
        normalized_terms: list[str],
        stale_seconds: float,
        gemini_bin: str,
        force: bool = False,
    ) -> ProfilePickResult | None:
        current_name = self.current_profile_name()
        check_profiles = self.load_check_state()["profiles"]
        current_result = self.load_quota_result(profile_name)
        should_refresh = force or self.quota_result_is_stale(
            current_result,
            normalized_terms,
            stale_seconds,
        )
        if should_refresh:
            self.refresh_profile_quota(profile_name, gemini_bin=gemini_bin)
        return self.make_pick_candidate(
            name=profile_name,
            normalized_terms=normalized_terms,
            current_name=current_name,
            check_profiles=check_profiles,
        )

    @operation_locked
    def auto_use_profile(
        self,
        match_terms: list[str] | None = None,
        min_remaining_percent: float = 15.0,
        stale_seconds: float = DEFAULT_QUOTA_STALE_SECONDS,
        candidate_refresh_limit: int = DEFAULT_CANDIDATE_REFRESH_LIMIT,
        gemini_bin: str = "gemini",
        avoid_profiles: list[str] | None = None,
    ) -> AutoSwitchDecision:
        threshold = self.validate_remaining_threshold(min_remaining_percent)
        stale_seconds = self.validate_stale_seconds(stale_seconds)
        candidate_refresh_limit = self.validate_candidate_refresh_limit(candidate_refresh_limit)
        normalized_terms = self.normalize_pick_match_terms(match_terms)
        avoid_names = set(self.normalize_avoid_profiles(avoid_profiles))
        current_name = self.current_profile_name()
        current_candidate: ProfilePickResult | None = None
        if current_name is not None and current_name not in avoid_names:
            current_candidate = self.maybe_refresh_pick_candidate(
                current_name,
                normalized_terms,
                stale_seconds,
                gemini_bin,
            )
        if current_candidate is not None and current_candidate.remaining_percent >= threshold:
            return AutoSwitchDecision(
                action="keep",
                reason=(
                    "current profile was refreshed if needed, matches the requested "
                    f"model filter, and remains above threshold {threshold:.1f}%"
                ),
                current_profile=current_name,
                selected=current_candidate,
                threshold_percent=threshold,
            )

        try:
            candidates = self.pick_candidates(
                normalized_terms,
                avoid_profiles=list(avoid_names),
            )
        except PoolError:
            candidates = []

        candidate_names = {item.name for item in candidates}
        ordered_noncurrent_names = [
            item.name
            for item in sorted(candidates, key=self.candidate_sort_key, reverse=True)
            if item.name != current_name and item.name not in avoid_names
        ]
        for name in self.list_profile_names():
            if (
                name != current_name
                and name not in candidate_names
                and name not in avoid_names
            ):
                ordered_noncurrent_names.append(name)

        refreshed_count = 0
        for name in ordered_noncurrent_names:
            if refreshed_count >= candidate_refresh_limit:
                break
            existing_result = self.load_quota_result(name)
            if not self.quota_result_is_stale(existing_result, normalized_terms, stale_seconds):
                continue
            self.refresh_profile_quota(name, gemini_bin=gemini_bin)
            refreshed_count += 1

        candidates = self.pick_candidates(
            normalized_terms,
            avoid_profiles=list(avoid_names),
        )
        selected = max(candidates, key=self.candidate_sort_key)
        if selected.is_current:
            return AutoSwitchDecision(
                action="keep",
                reason=(
                    "current profile is still the best eligible candidate after the "
                    f"refresh pass, even though it is below threshold {threshold:.1f}%"
                ),
                current_profile=current_name,
                selected=selected,
                threshold_percent=threshold,
            )

        previous_profile = current_name
        self.use_profile(selected.name)
        selected.is_current = True
        return AutoSwitchDecision(
            action="switch",
            reason=(
                "switched after refreshing the current profile and the top cached "
                f"candidates because the current profile was missing, unhealthy, "
                f"unmatched, or below threshold {threshold:.1f}%"
            ),
            current_profile=previous_profile,
            selected=selected,
            threshold_percent=threshold,
        )

    @operation_locked
    def mark_profile_rate_limited(
        self,
        name: str,
        detail: str = "Gemini request hit a rate limit.",
        retry_after_seconds: float | None = None,
    ) -> ProfileStatsResult:
        profile_name = self.validate_profile_name(name)
        if not self.profile_exists(profile_name):
            raise PoolError(f"unknown profile: {profile_name}")

        meta = self.load_profile_meta(profile_name)
        existing_result = self.load_quota_result(profile_name)
        checked_at = utc_now_iso()
        compact_detail = compact_output(detail)
        if not compact_detail:
            compact_detail = "Gemini request hit a rate limit."
        failure_streak = self.next_failure_streak(existing_result)
        blocked_until = future_time_iso(
            self.compute_failure_cooldown_seconds(
                "rate_limited",
                failure_streak,
                retry_after_seconds=retry_after_seconds,
            )
        )

        if existing_result is not None:
            result = replace(existing_result)
            if result.email is None:
                result.email = meta.get("email")
        else:
            result = self.make_stats_result(
                name=profile_name,
                email=meta.get("email"),
                status="rate_limited",
                detail=compact_detail,
                source=QUOTA_SOURCE_API,
            )

        result.status = "rate_limited"
        result.detail = compact_detail
        result.checked_at = checked_at
        result.last_refresh_attempt_at = checked_at
        result.last_refresh_status = "rate_limited"
        result.last_refresh_detail = compact_detail
        result.blocked_until = blocked_until
        result.blocked_reason = "request rate limited"
        result.failure_streak = failure_streak
        result.retry_after_seconds = None
        if result.source is None:
            result.source = QUOTA_SOURCE_API

        self.write_quota_result(result)
        return result

    @operation_locked
    def save_current(self, name: str | None = None, overwrite: bool = False) -> ProfileSummary:
        self.ensure_layout()
        live_creds = self.require_live_creds()
        profile_name = self.infer_profile_name(name)
        profile_dir = self.paths.profile_dir(profile_name)
        meta_file = self.paths.profile_meta_file(profile_name)
        existing_meta = self.load_profile_meta(profile_name) if profile_dir.exists() else {}
        fingerprint = canonical_creds_fingerprint(live_creds)

        if profile_dir.exists() and not overwrite:
            existing_fingerprint = existing_meta.get("fingerprint")
            if existing_fingerprint and existing_fingerprint != fingerprint:
                raise PoolError(f"profile already exists and differs: {profile_name}")

        profile_dir.mkdir(parents=True, exist_ok=True)
        copy_file(self.paths.live_creds_file, self.paths.profile_creds_file(profile_name))
        if self.paths.live_account_id_file.exists():
            copy_file(self.paths.live_account_id_file, self.paths.profile_account_id_file(profile_name))
        else:
            try:
                self.paths.profile_account_id_file(profile_name).unlink()
            except FileNotFoundError:
                pass

        created_at = existing_meta.get("created_at") or utc_now_iso()
        email = self.load_live_email()
        meta = {
            "name": profile_name,
            "email": email,
            "fingerprint": fingerprint,
            "created_at": created_at,
            "updated_at": utc_now_iso(),
        }
        write_json(meta_file, meta)

        return ProfileSummary(
            name=profile_name,
            email=email,
            created_at=meta["created_at"],
            updated_at=meta["updated_at"],
            is_current=self.current_profile_name() == profile_name,
        )

    @operation_locked
    def use_profile(self, name: str) -> ProfileSummary:
        profile_name = self.validate_profile_name(name)
        if not self.profile_exists(profile_name):
            raise PoolError(f"unknown profile: {profile_name}")

        meta = self.load_profile_meta(profile_name)
        copy_file(self.paths.profile_creds_file(profile_name), self.paths.live_creds_file)
        profile_account_id = self.paths.profile_account_id_file(profile_name)
        if profile_account_id.exists():
            copy_file(profile_account_id, self.paths.live_account_id_file)
        else:
            try:
                self.paths.live_account_id_file.unlink()
            except FileNotFoundError:
                pass

        self.clear_token_caches()
        self.update_google_accounts(meta.get("email"))
        self.write_state(profile_name)

        refreshed_meta = self.load_profile_meta(profile_name)
        return ProfileSummary(
            name=profile_name,
            email=refreshed_meta.get("email"),
            created_at=refreshed_meta.get("created_at", ""),
            updated_at=refreshed_meta.get("updated_at", ""),
            is_current=True,
        )

    @operation_locked
    def next_profile(self) -> ProfileSummary:
        names = self.list_profile_names()
        if not names:
            raise PoolError("no saved profiles")

        current = self.current_profile_name()
        if current not in names:
            target = names[0]
        else:
            current_index = names.index(current)
            target = names[(current_index + 1) % len(names)]
        return self.use_profile(target)

    @operation_locked
    def remove_profile(self, name: str) -> None:
        profile_name = self.validate_profile_name(name)
        profile_dir = self.paths.profile_dir(profile_name)
        if not profile_dir.exists():
            raise PoolError(f"unknown profile: {profile_name}")
        shutil.rmtree(profile_dir)
        self.drop_check_result(profile_name)
        self.drop_quota_result(profile_name)
        if self.current_profile_name() == profile_name:
            self.write_state(None)

    def clear_live_auth(self) -> None:
        for path in (
            self.paths.live_creds_file,
            self.paths.live_account_id_file,
            self.paths.token_cache_v1,
            self.paths.token_cache_v2,
        ):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self.update_google_accounts(None)

    def backup_live_auth(self, backup_dir: Path) -> None:
        backup_dir.mkdir(parents=True, exist_ok=True)
        for source in (
            self.paths.live_creds_file,
            self.paths.live_account_id_file,
            self.paths.google_accounts_file,
            self.paths.state_file,
        ):
            if source.exists():
                copy_file(source, backup_dir / source.name)

    def restore_live_auth(self, backup_dir: Path) -> None:
        for target in (
            self.paths.live_creds_file,
            self.paths.live_account_id_file,
            self.paths.google_accounts_file,
            self.paths.state_file,
        ):
            source = backup_dir / target.name
            if source.exists():
                copy_file(source, target)
            else:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass

    def classify_probe_result(self, returncode: int, output: str) -> tuple[str, str]:
        if returncode == 0:
            if "pong" in output:
                return "ok", "pong"
            return "ok", compact_output(output)

        lowered = output.lower()
        if "validationrequirederror" in lowered or "verify your account" in lowered:
            return "validation_required", "Verify your account to continue."
        if "change login" in lowered or "change_auth" in lowered:
            return "auth_change_required", "Gemini asked to change login."
        return "error", compact_output(output)

    def make_check_result(
        self,
        name: str,
        email: str | None,
        status: str,
        detail: str,
        returncode: int | None,
    ) -> ProfileCheckResult:
        return ProfileCheckResult(
            name=name,
            email=email,
            status=status,
            detail=detail,
            returncode=returncode,
            checked_at=utc_now_iso(),
        )

    def make_stats_result(
        self,
        name: str,
        email: str | None,
        status: str,
        detail: str,
        auth_method: str | None = None,
        tier: str | None = None,
        tier_id: str | None = None,
        session_id: str | None = None,
        usage_label: str | None = None,
        source: str | None = QUOTA_SOURCE_STATS,
        project_id: str | None = None,
        last_refresh_attempt_at: str | None = None,
        last_refresh_status: str | None = None,
        last_refresh_detail: str | None = None,
        blocked_until: str | None = None,
        blocked_reason: str | None = None,
        failure_streak: int = 0,
        retry_after_seconds: float | None = None,
        models: list[ModelUsageStat] | None = None,
    ) -> ProfileStatsResult:
        return ProfileStatsResult(
            name=name,
            email=email,
            status=status,
            detail=detail,
            checked_at=utc_now_iso(),
            auth_method=auth_method,
            tier=tier,
            tier_id=tier_id,
            session_id=session_id,
            usage_label=usage_label,
            source=source,
            project_id=project_id,
            last_refresh_attempt_at=last_refresh_attempt_at,
            last_refresh_status=last_refresh_status,
            last_refresh_detail=last_refresh_detail,
            blocked_until=blocked_until,
            blocked_reason=blocked_reason,
            failure_streak=failure_streak,
            retry_after_seconds=retry_after_seconds,
            models=models or [],
        )

    def validate_stale_seconds(self, stale_seconds: float) -> float:
        if stale_seconds < 0:
            raise PoolError("stale seconds must be zero or greater")
        return stale_seconds

    def validate_candidate_refresh_limit(self, limit: int) -> int:
        if limit < 0:
            raise PoolError("candidate refresh limit must be zero or greater")
        return limit

    def candidate_sort_key(self, item: ProfilePickResult) -> tuple[float, int, str, str]:
        return (
            item.remaining_percent,
            1 if item.is_current else 0,
            item.quota_checked_at,
            item.name,
        )

    def load_code_assist_constants(self, gemini_bin: str = "gemini") -> dict[str, str]:
        return load_official_gemini_cli_constants(gemini_bin)

    def parse_retry_after_seconds(self, headers: dict[str, str] | None) -> float | None:
        if not headers:
            return None
        value: str | None = None
        for key, candidate in headers.items():
            if key.lower() == "retry-after":
                value = candidate
                break
        if not isinstance(value, str) or not value.strip():
            return None
        stripped = value.strip()
        try:
            seconds = float(stripped)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(stripped)
            except (TypeError, ValueError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
        return max(seconds, 0.0)

    def next_failure_streak(self, result: ProfileStatsResult | None) -> int:
        if result is None:
            return 1
        if result.last_refresh_status == "ok":
            return 1
        return max(result.failure_streak, 0) + 1

    def compute_failure_cooldown_seconds(
        self,
        status: str,
        failure_streak: int,
        retry_after_seconds: float | None = None,
    ) -> float:
        if retry_after_seconds is not None:
            return min(max(retry_after_seconds, 1.0), MAX_REFRESH_COOLDOWN_SECONDS)
        if status == "rate_limited":
            base_seconds = DEFAULT_RATE_LIMIT_REFRESH_COOLDOWN_SECONDS
        elif status in {"validation_required", "auth_change_required", "project_required"}:
            return DEFAULT_NONTRANSIENT_REFRESH_COOLDOWN_SECONDS
        else:
            base_seconds = DEFAULT_TRANSIENT_REFRESH_COOLDOWN_SECONDS
        multiplier = 2 ** min(max(failure_streak - 1, 0), 3)
        return min(base_seconds * multiplier, MAX_REFRESH_COOLDOWN_SECONDS)

    def build_failure_block_reason(self, status: str, detail: str) -> str:
        if status == "rate_limited":
            return "refresh rate limited"
        if status == "validation_required":
            return "refresh requires account verification"
        if status == "auth_change_required":
            return "refresh requires login change"
        if status == "project_required":
            return "refresh requires GOOGLE_CLOUD_PROJECT"
        return compact_output(detail)

    def matching_models_for_refresh_window(
        self,
        quota_result: ProfileStatsResult,
        match_terms: list[str],
    ) -> list[ModelUsageStat]:
        if match_terms:
            return self.matching_models(quota_result, match_terms)
        return [
            item
            for item in quota_result.models
            if item.remaining_percent is not None
        ]

    def quota_result_refresh_blocked_until(
        self,
        result: ProfileStatsResult | None,
        match_terms: list[str],
    ) -> str | None:
        if result is None:
            return None
        now = datetime.now(timezone.utc)
        blocked_until = parse_iso_datetime(result.blocked_until)
        if blocked_until is not None and blocked_until > now:
            return blocked_until.replace(microsecond=0).isoformat()
        if result.status != "ok" or result.source != QUOTA_SOURCE_API:
            return None
        relevant_models = self.matching_models_for_refresh_window(result, match_terms)
        if not relevant_models:
            return None
        future_resets: list[datetime] = []
        for item in relevant_models:
            if item.remaining_percent is None or item.remaining_percent > 0:
                return None
            reset_at = parse_iso_datetime(item.reset_at)
            if reset_at is None or reset_at <= now:
                return None
            future_resets.append(reset_at)
        if not future_resets:
            return None
        return min(future_resets).replace(microsecond=0).isoformat()

    def merge_refreshed_quota_result(
        self,
        existing_result: ProfileStatsResult | None,
        refreshed_result: ProfileStatsResult,
    ) -> ProfileStatsResult:
        attempt_at = refreshed_result.checked_at
        if refreshed_result.status == "ok":
            refreshed_result.last_refresh_attempt_at = attempt_at
            refreshed_result.last_refresh_status = "ok"
            refreshed_result.last_refresh_detail = refreshed_result.detail
            refreshed_result.blocked_until = None
            refreshed_result.blocked_reason = None
            refreshed_result.failure_streak = 0
            refreshed_result.retry_after_seconds = None
            return refreshed_result

        failure_streak = self.next_failure_streak(existing_result)
        blocked_until = future_time_iso(
            self.compute_failure_cooldown_seconds(
                refreshed_result.status,
                failure_streak,
                retry_after_seconds=refreshed_result.retry_after_seconds,
            )
        )
        blocked_reason = self.build_failure_block_reason(
            refreshed_result.status,
            refreshed_result.detail,
        )
        if existing_result is not None:
            merged_result = replace(existing_result)
            merged_result.last_refresh_attempt_at = attempt_at
            merged_result.last_refresh_status = refreshed_result.status
            merged_result.last_refresh_detail = refreshed_result.detail
            merged_result.blocked_until = blocked_until
            merged_result.blocked_reason = blocked_reason
            merged_result.failure_streak = failure_streak
            merged_result.retry_after_seconds = None
            if existing_result.status != "ok":
                merged_result.status = refreshed_result.status
                merged_result.detail = refreshed_result.detail
                merged_result.checked_at = refreshed_result.checked_at
                merged_result.email = refreshed_result.email or existing_result.email
                merged_result.tier = refreshed_result.tier or existing_result.tier
                merged_result.tier_id = refreshed_result.tier_id or existing_result.tier_id
                merged_result.source = refreshed_result.source or existing_result.source
                merged_result.project_id = (
                    refreshed_result.project_id or existing_result.project_id
                )
                if refreshed_result.models:
                    merged_result.models = refreshed_result.models
            return merged_result

        refreshed_result.last_refresh_attempt_at = attempt_at
        refreshed_result.last_refresh_status = refreshed_result.status
        refreshed_result.last_refresh_detail = refreshed_result.detail
        refreshed_result.blocked_until = blocked_until
        refreshed_result.blocked_reason = blocked_reason
        refreshed_result.failure_streak = failure_streak
        refreshed_result.retry_after_seconds = None
        return refreshed_result

    def http_post_json(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                payload_obj = json.loads(body)
            except json.JSONDecodeError:
                payload_obj = None
            detail = compact_output(body or str(exc))
            response_headers = dict(exc.headers.items()) if exc.headers is not None else None
            raise ApiRequestError(exc.code, detail, payload_obj, headers=response_headers) from exc
        except urllib.error.URLError as exc:
            raise PoolError(f"request failed: {exc.reason}") from exc

        if not body:
            return {}
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {}

    def http_post_form(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        encoded = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=encoded,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                **(headers or {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                payload_obj = json.loads(body)
            except json.JSONDecodeError:
                payload_obj = None
            detail = compact_output(body or str(exc))
            response_headers = dict(exc.headers.items()) if exc.headers is not None else None
            raise ApiRequestError(exc.code, detail, payload_obj, headers=response_headers) from exc
        except urllib.error.URLError as exc:
            raise PoolError(f"request failed: {exc.reason}") from exc

        if not body:
            return {}
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {}

    def oauth_token_is_expired(self, creds: dict[str, Any], skew_seconds: float = 60.0) -> bool:
        expiry_date = creds.get("expiry_date")
        if not isinstance(expiry_date, (int, float)):
            return False
        return time.time() * 1000 >= float(expiry_date) - skew_seconds * 1000

    def refresh_live_access_token(self, gemini_bin: str = "gemini") -> dict[str, Any]:
        creds = self.require_live_creds()
        refresh_token = creds.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise PoolError("missing refresh token in live Gemini credentials")
        constants = self.load_code_assist_constants(gemini_bin)
        response = self.http_post_form(
            TOKEN_ENDPOINT,
            {
                "client_id": constants["client_id"],
                "client_secret": constants["client_secret"],
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        access_token = response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise PoolError("token refresh returned no access token")

        updated = dict(creds)
        updated["access_token"] = access_token
        new_refresh_token = response.get("refresh_token")
        if isinstance(new_refresh_token, str) and new_refresh_token:
            updated["refresh_token"] = new_refresh_token
        token_type = response.get("token_type")
        if isinstance(token_type, str) and token_type:
            updated["token_type"] = token_type
        scope = response.get("scope")
        if isinstance(scope, str) and scope:
            updated["scope"] = scope
        id_token = response.get("id_token")
        if isinstance(id_token, str) and id_token:
            updated["id_token"] = id_token
        expires_in = response.get("expires_in")
        try:
            expires_in_seconds = float(expires_in)
        except (TypeError, ValueError):
            expires_in_seconds = None
        if expires_in_seconds is not None:
            updated["expiry_date"] = int(time.time() * 1000 + expires_in_seconds * 1000)

        write_json(self.paths.live_creds_file, updated)
        return updated

    def refresh_live_access_token_if_needed(
        self,
        gemini_bin: str = "gemini",
        force: bool = False,
    ) -> dict[str, Any]:
        creds = self.require_live_creds()
        if force or self.oauth_token_is_expired(creds):
            return self.refresh_live_access_token(gemini_bin=gemini_bin)
        return creds

    def code_assist_method_url(self, method: str, gemini_bin: str = "gemini") -> str:
        constants = self.load_code_assist_constants(gemini_bin)
        return f"{constants['endpoint']}/{constants['api_version']}:{method}"

    def code_assist_post(
        self,
        method: str,
        payload: dict[str, Any],
        gemini_bin: str = "gemini",
        retry_on_401: bool = True,
    ) -> dict[str, Any]:
        creds = self.refresh_live_access_token_if_needed(gemini_bin=gemini_bin)
        headers = {"Authorization": f"Bearer {creds['access_token']}"}
        url = self.code_assist_method_url(method, gemini_bin=gemini_bin)
        try:
            return self.http_post_json(url, payload, headers=headers)
        except ApiRequestError as exc:
            if exc.status_code == 401 and retry_on_401:
                refreshed = self.refresh_live_access_token_if_needed(
                    gemini_bin=gemini_bin,
                    force=True,
                )
                retry_headers = {"Authorization": f"Bearer {refreshed['access_token']}"}
                return self.http_post_json(url, payload, headers=retry_headers)
            raise

    def extract_api_error_detail(self, payload: Any, fallback: str) -> str:
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str) and message:
                    return compact_output(message)
            message = payload.get("message")
            if isinstance(message, str) and message:
                return compact_output(message)
            return compact_output(json.dumps(payload, ensure_ascii=False))
        return compact_output(fallback)

    def classify_api_error(self, error: ApiRequestError) -> tuple[str, str, float | None]:
        detail = self.extract_api_error_detail(error.payload, error.detail)
        lowered = detail.lower()
        retry_after_seconds = self.parse_retry_after_seconds(error.headers)
        if error.status_code == 429:
            return "rate_limited", detail, retry_after_seconds
        if "validation_required" in lowered or "verify your account" in lowered:
            return "validation_required", "Verify your account to continue.", retry_after_seconds
        if "change_auth" in lowered or "change login" in lowered:
            return "auth_change_required", "Gemini asked to change login.", retry_after_seconds
        if "google_cloud_project" in lowered or "requires setting the google_cloud_project" in lowered:
            return (
                "project_required",
                "This account requires GOOGLE_CLOUD_PROJECT or GOOGLE_CLOUD_PROJECT_ID.",
                retry_after_seconds,
            )
        return "error", detail, retry_after_seconds

    def build_code_assist_metadata(self, project_id: str | None) -> dict[str, str]:
        metadata = {
            "ideType": "IDE_UNSPECIFIED",
            "platform": "PLATFORM_UNSPECIFIED",
            "pluginType": "GEMINI",
        }
        if project_id:
            metadata["duetProject"] = project_id
        return metadata

    def parse_load_code_assist_response(
        self,
        response: dict[str, Any],
    ) -> tuple[str, str, str | None, str | None, str | None]:
        current_tier = response.get("currentTier")
        paid_tier = response.get("paidTier")
        tier_payload = paid_tier if isinstance(paid_tier, dict) else current_tier
        tier_name = tier_payload.get("name") if isinstance(tier_payload, dict) else None
        tier_id = tier_payload.get("id") if isinstance(tier_payload, dict) else None

        if isinstance(current_tier, dict):
            project_id = response.get("cloudaicompanionProject")
            if isinstance(project_id, str) and project_id:
                return "ok", "-", project_id, tier_name if isinstance(tier_name, str) else None, tier_id if isinstance(tier_id, str) else None
            env_project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get(
                "GOOGLE_CLOUD_PROJECT_ID"
            )
            if env_project:
                return "ok", "-", env_project, tier_name if isinstance(tier_name, str) else None, tier_id if isinstance(tier_id, str) else None
            return (
                "project_required",
                "This account requires GOOGLE_CLOUD_PROJECT or GOOGLE_CLOUD_PROJECT_ID.",
                None,
                tier_name if isinstance(tier_name, str) else None,
                tier_id if isinstance(tier_id, str) else None,
            )

        ineligible_tiers = response.get("ineligibleTiers")
        if isinstance(ineligible_tiers, list):
            for item in ineligible_tiers:
                if not isinstance(item, dict):
                    continue
                reason_code = item.get("reasonCode")
                reason_message = item.get("reasonMessage")
                if reason_code == "VALIDATION_REQUIRED":
                    return (
                        "validation_required",
                        reason_message if isinstance(reason_message, str) and reason_message else "Verify your account to continue.",
                        None,
                        tier_name if isinstance(tier_name, str) else None,
                        tier_id if isinstance(tier_id, str) else None,
                    )
            messages = [
                item.get("reasonMessage")
                for item in ineligible_tiers
                if isinstance(item, dict) and isinstance(item.get("reasonMessage"), str)
            ]
            if messages:
                return (
                    "error",
                    compact_output("; ".join(messages)),
                    None,
                    tier_name if isinstance(tier_name, str) else None,
                    tier_id if isinstance(tier_id, str) else None,
                )
        return (
            "error",
            "loadCodeAssist returned no usable tier information",
            None,
            tier_name if isinstance(tier_name, str) else None,
            tier_id if isinstance(tier_id, str) else None,
        )

    def parse_api_quota_models(self, payload: dict[str, Any]) -> list[ModelUsageStat]:
        buckets = payload.get("buckets")
        if not isinstance(buckets, list):
            return []
        models: list[ModelUsageStat] = []
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            model_id = bucket.get("modelId")
            remaining_fraction = bucket.get("remainingFraction")
            if not isinstance(model_id, str) or not isinstance(remaining_fraction, (int, float)):
                continue
            percent = float(remaining_fraction) * 100.0
            reset_at = bucket.get("resetTime")
            if not isinstance(reset_at, str):
                reset_at = None
            reset_in = format_reset_in(reset_at)
            usage_remaining = f"{percent:.1f}%"
            if reset_in:
                usage_remaining = f"{usage_remaining} resets in {reset_in}"
            models.append(
                ModelUsageStat(
                    model=model_id,
                    requests="-",
                    usage_remaining=usage_remaining,
                    remaining_percent=percent,
                    reset_in=reset_in,
                    reset_at=reset_at,
                )
            )
        return models

    def sync_live_profile_creds(self, profile_name: str) -> None:
        copy_file(self.paths.live_creds_file, self.paths.profile_creds_file(profile_name))

    def terminate_process_group(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=1.0)
            return
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=1.0)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            pass

    def collect_current_profile_stats_output(
        self,
        gemini_bin: str = "gemini",
        timeout_seconds: float = 30.0,
        gemini_args: list[str] | None = None,
    ) -> tuple[str, bool]:
        command = [gemini_bin, *(gemini_args or []), "--screen-reader", "-i", "/stats"]
        env = self.build_gemini_env()
        env["TERM"] = "xterm-256color"

        master_fd, slave_fd = pty.openpty()
        configure_pty(slave_fd)
        process: subprocess.Popen[bytes] | None = None
        raw_output = bytearray()
        timed_out = False
        try:
            process = subprocess.Popen(
                command,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
            os.close(slave_fd)
            slave_fd = -1

            saw_stats = False
            saw_command = False
            deadline = time.monotonic() + timeout_seconds

            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                ready, _, _ = select.select([master_fd], [], [], 0.2)
                if not ready:
                    continue
                try:
                    chunk = os.read(master_fd, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                raw_output.extend(chunk)
                cleaned = sanitize_terminal_output(raw_output)
                saw_command = saw_command or "User: /stats" in cleaned
                saw_stats = saw_stats or "Session Stats" in cleaned
                if "Verify your account to continue." in cleaned:
                    break
                if "change login" in cleaned.lower():
                    break
                if saw_command and saw_stats and "Type your message or @path/to/file" in cleaned:
                    break
            else:
                timed_out = True
        finally:
            if process is not None:
                self.terminate_process_group(process)
            try:
                os.close(master_fd)
            except OSError:
                pass
            if slave_fd != -1:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass

        return sanitize_terminal_output(raw_output), timed_out

    def parse_profile_stats_output(
        self,
        name: str,
        email: str | None,
        output: str,
        timed_out: bool = False,
    ) -> ProfileStatsResult:
        cleaned = sanitize_terminal_output(output)
        lowered = cleaned.lower()

        if timed_out and "Session Stats" not in cleaned:
            return self.make_stats_result(
                name=name,
                email=email,
                status="timeout",
                detail="stats timed out before Gemini returned quota data",
            )
        if "verify your account" in lowered or "validationrequirederror" in lowered:
            return self.make_stats_result(
                name=name,
                email=email,
                status="validation_required",
                detail="Verify your account to continue.",
            )
        if "change login" in lowered or "change_auth" in lowered:
            return self.make_stats_result(
                name=name,
                email=email,
                status="auth_change_required",
                detail="Gemini asked to change login.",
            )
        if "Session Stats" not in cleaned:
            detail = compact_output(cleaned)
            if timed_out and detail == "-":
                detail = "stats timed out before Gemini returned output"
            return self.make_stats_result(
                name=name,
                email=email,
                status="error",
                detail=detail,
            )

        lines = [line for line in cleaned.splitlines() if line]
        session_id: str | None = None
        auth_method: str | None = None
        tier: str | None = None
        usage_label: str | None = None
        models: list[ModelUsageStat] = []
        table_started = False

        for index, line in enumerate(lines):
            if line.startswith("Session ID:"):
                session_id = line.partition(":")[2].strip() or None
                continue
            if line.startswith("Auth Method:"):
                auth_method = line.partition(":")[2].strip() or None
                continue
            if line.startswith("Tier:"):
                tier = line.partition(":")[2].strip() or None
                continue
            if line == "Model Reqs Usage remaining":
                table_started = True
                if index > 0 and lines[index - 1].endswith(" Usage"):
                    usage_label = lines[index - 1][: -len(" Usage")] or None
                continue
            if not table_started:
                continue
            if line.startswith(("⚠", "?", "shift+tab", ">", "gemini-auth-switch", "User:")):
                if models:
                    break
                continue
            match = MODEL_USAGE_LINE_RE.match(line)
            if not match:
                if models:
                    break
                continue
            remaining = match.group("remaining").strip()
            remaining_percent, reset_in = parse_model_remaining(remaining)
            models.append(
                ModelUsageStat(
                    model=match.group("model"),
                    requests=match.group("requests"),
                    usage_remaining=remaining,
                    remaining_percent=remaining_percent,
                    reset_in=reset_in,
                )
            )

        detail = f"models={len(models)}"
        percentages = [item.remaining_percent for item in models if item.remaining_percent is not None]
        if percentages:
            detail = f"models={len(models)} lowest_remaining={min(percentages):.1f}%"
        elif not models:
            detail = "no model quota lines returned"

        return self.make_stats_result(
            name=name,
            email=email,
            status="ok",
            detail=detail,
            auth_method=auth_method,
            tier=tier,
            session_id=session_id,
            usage_label=usage_label,
            models=models,
        )

    def quota_result_is_stale(
        self,
        result: ProfileStatsResult | None,
        match_terms: list[str],
        stale_seconds: float,
    ) -> bool:
        blocked_until = self.quota_result_refresh_blocked_until(result, match_terms)
        if blocked_until is not None:
            return False
        if result is None:
            return True
        checked_at = parse_iso_datetime(result.checked_at)
        if checked_at is None:
            return True
        age_seconds = (datetime.now(timezone.utc) - checked_at).total_seconds()
        if result.status != "ok":
            return age_seconds > stale_seconds
        if result.source != QUOTA_SOURCE_API:
            return True
        if age_seconds > stale_seconds:
            return True
        if match_terms and not self.matching_models(result, match_terms):
            return True
        return False

    def api_quota_current_profile(
        self,
        name: str,
        email: str | None,
        gemini_bin: str = "gemini",
    ) -> ProfileStatsResult:
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get(
            "GOOGLE_CLOUD_PROJECT_ID"
        )
        try:
            load_response = self.code_assist_post(
                "loadCodeAssist",
                {
                    "cloudaicompanionProject": project_id,
                    "metadata": self.build_code_assist_metadata(project_id),
                },
                gemini_bin=gemini_bin,
            )
        except ApiRequestError as exc:
            status, detail, retry_after_seconds = self.classify_api_error(exc)
            return self.make_stats_result(
                name=name,
                email=email,
                status=status,
                detail=detail,
                source=QUOTA_SOURCE_API,
                retry_after_seconds=retry_after_seconds,
            )

        load_status, load_detail, resolved_project_id, tier_name, tier_id = (
            self.parse_load_code_assist_response(load_response)
        )
        if load_status != "ok" or resolved_project_id is None:
            return self.make_stats_result(
                name=name,
                email=email,
                status=load_status,
                detail=load_detail,
                tier=tier_name,
                tier_id=tier_id,
                source=QUOTA_SOURCE_API,
                project_id=resolved_project_id,
            )

        try:
            quota_response = self.code_assist_post(
                "retrieveUserQuota",
                {"project": resolved_project_id},
                gemini_bin=gemini_bin,
            )
        except ApiRequestError as exc:
            status, detail, retry_after_seconds = self.classify_api_error(exc)
            return self.make_stats_result(
                name=name,
                email=email,
                status=status,
                detail=detail,
                tier=tier_name,
                tier_id=tier_id,
                source=QUOTA_SOURCE_API,
                project_id=resolved_project_id,
                retry_after_seconds=retry_after_seconds,
            )

        models = self.parse_api_quota_models(quota_response)
        detail = f"models={len(models)}"
        percentages = [item.remaining_percent for item in models if item.remaining_percent is not None]
        if percentages:
            detail = f"models={len(models)} lowest_remaining={min(percentages):.1f}%"
        elif not models:
            detail = "no model quota lines returned"
        return self.make_stats_result(
            name=name,
            email=email,
            status="ok",
            detail=detail,
            tier=tier_name,
            tier_id=tier_id,
            source=QUOTA_SOURCE_API,
            project_id=resolved_project_id,
            models=models,
        )

    @operation_locked
    def refresh_profile_quota(
        self,
        name: str,
        gemini_bin: str = "gemini",
    ) -> ProfileStatsResult:
        profile_name = self.validate_profile_name(name)
        if not self.profile_exists(profile_name):
            raise PoolError(f"unknown profile: {profile_name}")

        meta = self.load_profile_meta(profile_name)
        existing_result = self.load_quota_result(profile_name)
        with tempfile.TemporaryDirectory(prefix="gemini-auth-switch-quota-api-") as temp_dir:
            backup_dir = Path(temp_dir)
            self.backup_live_auth(backup_dir)
            try:
                try:
                    self.use_profile(profile_name)
                    result = self.api_quota_current_profile(
                        name=profile_name,
                        email=meta.get("email"),
                        gemini_bin=gemini_bin,
                    )
                    self.sync_live_profile_creds(profile_name)
                except Exception as exc:  # pragma: no cover - defensive guard
                    result = self.make_stats_result(
                        name=profile_name,
                        email=meta.get("email"),
                        status="error",
                        detail=compact_output(str(exc)),
                        source=QUOTA_SOURCE_API,
                    )
            finally:
                self.restore_live_auth(backup_dir)
                self.clear_token_caches()

        result = self.merge_refreshed_quota_result(existing_result, result)
        self.write_quota_result(result)
        return result

    def stats_current_profile(
        self,
        name: str,
        email: str | None,
        gemini_bin: str = "gemini",
        timeout_seconds: float = 30.0,
        gemini_args: list[str] | None = None,
    ) -> ProfileStatsResult:
        output, timed_out = self.collect_current_profile_stats_output(
            gemini_bin=gemini_bin,
            timeout_seconds=timeout_seconds,
            gemini_args=gemini_args,
        )
        return self.parse_profile_stats_output(name, email, output, timed_out=timed_out)

    def probe_current_profile(
        self,
        gemini_bin: str = "gemini",
        prompt: str = "ping",
        timeout_seconds: float = 30.0,
        gemini_args: list[str] | None = None,
    ) -> tuple[str, str, int | None]:
        command = [gemini_bin, *(gemini_args or []), "-p", prompt]
        try:
            result = subprocess.run(
                command,
                env=self.build_gemini_env(),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            output = normalize_subprocess_output(exc.stdout) + normalize_subprocess_output(exc.stderr)
            detail = compact_output(output)
            if detail == "-":
                detail = f"probe timed out after {timeout_seconds:.1f}s"
            return "timeout", detail, None

        output = normalize_subprocess_output(result.stdout) + normalize_subprocess_output(result.stderr)
        status, detail = self.classify_probe_result(result.returncode, output)
        return status, detail, result.returncode

    @operation_locked
    def check_all_profiles(
        self,
        gemini_bin: str = "gemini",
        prompt: str = "ping",
        timeout_seconds: float = 30.0,
        delay_seconds: float = 5.0,
        limit: int | None = None,
        gemini_args: list[str] | None = None,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> list[ProfileCheckResult]:
        names = self.list_profile_names()
        if not names:
            raise PoolError("no saved profiles")
        if limit is not None:
            if limit <= 0:
                raise PoolError("limit must be greater than zero")
            names = names[:limit]

        results: list[ProfileCheckResult] = []
        with tempfile.TemporaryDirectory(prefix="gemini-auth-switch-check-") as temp_dir:
            backup_dir = Path(temp_dir)
            self.backup_live_auth(backup_dir)
            try:
                for index, name in enumerate(names):
                    meta = self.load_profile_meta(name)
                    if progress_callback is not None:
                        progress_callback(
                            "start",
                            {
                                "index": index + 1,
                                "total": len(names),
                                "name": name,
                                "email": meta.get("email"),
                            },
                        )
                    try:
                        self.use_profile(name)
                        status, detail, returncode = self.probe_current_profile(
                            gemini_bin=gemini_bin,
                            prompt=prompt,
                            timeout_seconds=timeout_seconds,
                            gemini_args=gemini_args,
                        )
                    except Exception as exc:  # pragma: no cover - defensive guard
                        status = "error"
                        detail = compact_output(str(exc))
                        returncode = None

                    result = self.make_check_result(
                        name=name,
                        email=meta.get("email"),
                        status=status,
                        detail=detail,
                        returncode=returncode,
                    )
                    self.write_check_result(result)
                    results.append(result)
                    if progress_callback is not None:
                        progress_callback(
                            "result",
                            {
                                "index": index + 1,
                                "total": len(names),
                                "result": results[-1],
                            },
                        )
                    if delay_seconds > 0 and index + 1 < len(names):
                        if progress_callback is not None:
                            progress_callback(
                                "delay",
                                {
                                    "seconds": delay_seconds,
                                    "next_index": index + 2,
                                    "total": len(names),
                                },
                            )
                        time.sleep(delay_seconds)
            finally:
                self.restore_live_auth(backup_dir)
                self.clear_token_caches()
        return results

    @operation_locked
    def check_profile(
        self,
        name: str,
        gemini_bin: str = "gemini",
        prompt: str = "ping",
        timeout_seconds: float = 30.0,
        gemini_args: list[str] | None = None,
    ) -> ProfileCheckResult:
        profile_name = self.validate_profile_name(name)
        if not self.profile_exists(profile_name):
            raise PoolError(f"unknown profile: {profile_name}")

        meta = self.load_profile_meta(profile_name)
        with tempfile.TemporaryDirectory(prefix="gemini-auth-switch-check-one-") as temp_dir:
            backup_dir = Path(temp_dir)
            self.backup_live_auth(backup_dir)
            try:
                try:
                    self.use_profile(profile_name)
                    status, detail, returncode = self.probe_current_profile(
                        gemini_bin=gemini_bin,
                        prompt=prompt,
                        timeout_seconds=timeout_seconds,
                        gemini_args=gemini_args,
                    )
                except Exception as exc:  # pragma: no cover - defensive guard
                    status = "error"
                    detail = compact_output(str(exc))
                    returncode = None
            finally:
                self.restore_live_auth(backup_dir)
                self.clear_token_caches()

        result = self.make_check_result(
            name=profile_name,
            email=meta.get("email"),
            status=status,
            detail=detail,
            returncode=returncode,
        )
        self.write_check_result(result)
        return result

    @operation_locked
    def stats_all_profiles(
        self,
        gemini_bin: str = "gemini",
        timeout_seconds: float = 30.0,
        delay_seconds: float = 5.0,
        limit: int | None = None,
        gemini_args: list[str] | None = None,
        progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> list[ProfileStatsResult]:
        names = self.list_profile_names()
        if not names:
            raise PoolError("no saved profiles")
        if limit is not None:
            if limit <= 0:
                raise PoolError("limit must be greater than zero")
            names = names[:limit]

        results: list[ProfileStatsResult] = []
        with tempfile.TemporaryDirectory(prefix="gemini-auth-switch-stats-") as temp_dir:
            backup_dir = Path(temp_dir)
            self.backup_live_auth(backup_dir)
            try:
                for index, name in enumerate(names):
                    meta = self.load_profile_meta(name)
                    if progress_callback is not None:
                        progress_callback(
                            "start",
                            {
                                "index": index + 1,
                                "total": len(names),
                                "name": name,
                                "email": meta.get("email"),
                            },
                        )
                    try:
                        self.use_profile(name)
                        result = self.stats_current_profile(
                            name=name,
                            email=meta.get("email"),
                            gemini_bin=gemini_bin,
                            timeout_seconds=timeout_seconds,
                            gemini_args=gemini_args,
                        )
                    except Exception as exc:  # pragma: no cover - defensive guard
                        result = self.make_stats_result(
                            name=name,
                            email=meta.get("email"),
                            status="error",
                            detail=compact_output(str(exc)),
                        )

                    self.write_quota_result(result)
                    results.append(result)
                    if progress_callback is not None:
                        progress_callback(
                            "result",
                            {
                                "index": index + 1,
                                "total": len(names),
                                "result": result,
                            },
                        )
                    if delay_seconds > 0 and index + 1 < len(names):
                        if progress_callback is not None:
                            progress_callback(
                                "delay",
                                {
                                    "seconds": delay_seconds,
                                    "next_index": index + 2,
                                    "total": len(names),
                                },
                            )
                        time.sleep(delay_seconds)
            finally:
                self.restore_live_auth(backup_dir)
                self.clear_token_caches()
        return results

    @operation_locked
    def stats_profile(
        self,
        name: str,
        gemini_bin: str = "gemini",
        timeout_seconds: float = 30.0,
        gemini_args: list[str] | None = None,
    ) -> ProfileStatsResult:
        profile_name = self.validate_profile_name(name)
        if not self.profile_exists(profile_name):
            raise PoolError(f"unknown profile: {profile_name}")

        meta = self.load_profile_meta(profile_name)
        with tempfile.TemporaryDirectory(prefix="gemini-auth-switch-stats-one-") as temp_dir:
            backup_dir = Path(temp_dir)
            self.backup_live_auth(backup_dir)
            try:
                try:
                    self.use_profile(profile_name)
                    result = self.stats_current_profile(
                        name=profile_name,
                        email=meta.get("email"),
                        gemini_bin=gemini_bin,
                        timeout_seconds=timeout_seconds,
                        gemini_args=gemini_args,
                    )
                except Exception as exc:  # pragma: no cover - defensive guard
                    result = self.make_stats_result(
                        name=profile_name,
                        email=meta.get("email"),
                        status="error",
                        detail=compact_output(str(exc)),
                    )
            finally:
                self.restore_live_auth(backup_dir)
                self.clear_token_caches()

        self.write_quota_result(result)
        return result

    @operation_locked
    def login(
        self,
        profile_name: str | None,
        gemini_bin: str = "gemini",
        gemini_args: list[str] | None = None,
        overwrite: bool = False,
    ) -> ProfileSummary:
        self.ensure_layout()
        with tempfile.TemporaryDirectory(prefix="gemini-auth-switch-") as temp_dir:
            backup_dir = Path(temp_dir)
            self.backup_live_auth(backup_dir)
            self.clear_live_auth()

            command = [gemini_bin, *(gemini_args or [])]
            env = self.build_gemini_env()
            result = subprocess.run(command, env=env)
            if result.returncode != 0:
                self.restore_live_auth(backup_dir)
                raise PoolError(f"gemini login command failed with exit code {result.returncode}")
            if not self.paths.live_creds_file.exists():
                self.restore_live_auth(backup_dir)
                raise PoolError("login finished without producing oauth_creds.json")
            return self.save_current(profile_name, overwrite=overwrite)
