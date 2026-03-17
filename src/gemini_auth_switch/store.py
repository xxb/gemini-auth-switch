from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .paths import GeminiPaths


PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9._@+-]+$")


class PoolError(RuntimeError):
    pass


@dataclass
class ProfileSummary:
    name: str
    email: str | None
    created_at: str
    updated_at: str
    is_current: bool


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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


class GeminiAuthPool:
    def __init__(self, paths: GeminiPaths):
        self.paths = paths

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
        summaries: list[ProfileSummary] = []
        for name in self.list_profile_names():
            meta = self.load_profile_meta(name)
            summaries.append(
                ProfileSummary(
                    name=name,
                    email=meta.get("email"),
                    created_at=meta.get("created_at", ""),
                    updated_at=meta.get("updated_at", ""),
                    is_current=name == current_name,
                )
            )
        return summaries

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

    def remove_profile(self, name: str) -> None:
        profile_name = self.validate_profile_name(name)
        profile_dir = self.paths.profile_dir(profile_name)
        if not profile_dir.exists():
            raise PoolError(f"unknown profile: {profile_name}")
        shutil.rmtree(profile_dir)
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
        ):
            if source.exists():
                copy_file(source, backup_dir / source.name)

    def restore_live_auth(self, backup_dir: Path) -> None:
        for target in (
            self.paths.live_creds_file,
            self.paths.live_account_id_file,
            self.paths.google_accounts_file,
        ):
            source = backup_dir / target.name
            if source.exists():
                copy_file(source, target)
            else:
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass

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
            env = os.environ.copy()
            env["GEMINI_FORCE_FILE_STORAGE"] = "true"
            env["GEMINI_FORCE_ENCRYPTED_FILE_STORAGE"] = "false"
            result = subprocess.run(command, env=env)
            if result.returncode != 0:
                self.restore_live_auth(backup_dir)
                raise PoolError(f"gemini login command failed with exit code {result.returncode}")
            if not self.paths.live_creds_file.exists():
                self.restore_live_auth(backup_dir)
                raise PoolError("login finished without producing oauth_creds.json")
            return self.save_current(profile_name, overwrite=overwrite)
