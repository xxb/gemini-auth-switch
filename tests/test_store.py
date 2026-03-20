from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from gemini_auth_switch.paths import GeminiPaths
from gemini_auth_switch.cli import run
from gemini_auth_switch.store import (
    GeminiAuthPool,
    ModelUsageStat,
    PoolError,
    QUOTA_SOURCE_API,
    QUOTA_SOURCE_STATS,
)


def make_stats_output(email: str = "user@example.com") -> str:
    return f"""
User: /stats
Session Stats
Interaction Summary
Session ID: 34ab025b-7c21-46d7-95c8-b4c549e6bdc8
Auth Method: Logged in with Google ({email})
Tier: Gemini Code Assist in Google One AI Pro
Tool Calls: 0 ( ✓ 0 x 0 )
Success Rate: 0.0%
Performance
Wall Time: 969ms
Agent Active: 0s
» API Time: 0s (0.0%)
» Tool Time: 0s (0.0%)
Auto (Gemini 3) Usage
Model Reqs Usage remaining
gemini-2.5-flash - 96.4% resets in 22h 25m
gemini-2.5-flash-lite - 98.3% resets in 22h 25m
gemini-2.5-pro - 93.3% resets in 22h 24m
> Type your message or @path/to/file
""".strip()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def iso_now(offset_seconds: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).replace(
        microsecond=0
    ).isoformat()


def make_quota_state_entry(
    email: str,
    remaining_percent: float,
    *,
    checked_at: str | None = None,
    source: str | None = QUOTA_SOURCE_API,
    model: str = "gemini-3.1-pro-preview",
    reset_in: str = "10h",
    reset_at: str | None = None,
    status: str = "ok",
    detail: str | None = None,
    tier: str = "Gemini Code Assist",
    tier_id: str | None = None,
    usage_label: str = "Auto (Gemini 3)",
    project_id: str | None = None,
    last_refresh_attempt_at: str | None = None,
    last_refresh_status: str | None = None,
    last_refresh_detail: str | None = None,
    blocked_until: str | None = None,
    blocked_reason: str | None = None,
    failure_streak: int | None = None,
) -> dict:
    usage_remaining = f"{remaining_percent:.1f}%"
    if reset_in:
        usage_remaining = f"{usage_remaining} resets in {reset_in}"
    entry = {
        "status": status,
        "detail": detail or f"models=1 lowest_remaining={remaining_percent:.1f}%",
        "checked_at": checked_at or iso_now(),
        "email": email,
        "tier": tier,
        "usage_label": usage_label,
        "models": [
            {
                "model": model,
                "requests": "-",
                "usage_remaining": usage_remaining,
                "remaining_percent": remaining_percent,
                "reset_in": reset_in,
                "reset_at": reset_at,
            }
        ],
    }
    if source is not None:
        entry["source"] = source
    if tier_id is not None:
        entry["tier_id"] = tier_id
    if project_id is not None:
        entry["project_id"] = project_id
    if last_refresh_attempt_at is not None:
        entry["last_refresh_attempt_at"] = last_refresh_attempt_at
    if last_refresh_status is not None:
        entry["last_refresh_status"] = last_refresh_status
    if last_refresh_detail is not None:
        entry["last_refresh_detail"] = last_refresh_detail
    if blocked_until is not None:
        entry["blocked_until"] = blocked_until
    if blocked_reason is not None:
        entry["blocked_reason"] = blocked_reason
    if failure_streak is not None:
        entry["failure_streak"] = failure_streak
    return entry


class GeminiAuthPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        gemini_dir = Path(self.temp_dir.name) / ".gemini"
        gemini_dir.mkdir(parents=True, exist_ok=True)
        self.paths = GeminiPaths(gemini_dir)
        self.pool = GeminiAuthPool(self.paths)

    def seed_live_auth(self, refresh_token: str, email: str = "user@example.com") -> None:
        write_json(
            self.paths.live_creds_file,
            {
                "access_token": "access",
                "refresh_token": refresh_token,
                "token_type": "Bearer",
                "scope": "scope",
                "expiry_date": 123,
            },
        )
        write_json(self.paths.google_accounts_file, {"active": email, "old": []})
        self.paths.live_account_id_file.write_text("account-id", encoding="utf-8")

    def seed_settings(self, selected_type: str = "oauth-personal") -> None:
        write_json(
            self.paths.settings_file,
            {"security": {"auth": {"selectedType": selected_type}}},
        )

    def test_save_and_list_profile(self) -> None:
        self.seed_live_auth("rt-1")
        summary = self.pool.save_current()
        self.assertEqual(summary.name, "user@example.com")
        profiles = self.pool.list_profiles()
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].email, "user@example.com")
        self.assertIsNone(profiles[0].last_check_status)

    def test_auth_operation_lock_is_reentrant(self) -> None:
        with self.pool.auth_operation_lock():
            self.assertTrue(self.paths.operation_lock_file.exists())
            self.assertEqual(self.pool._operation_lock_depth, 1)
            payload = self.paths.operation_lock_file.read_text(encoding="utf-8")
            self.assertIn("pid=", payload)
            with self.pool.auth_operation_lock():
                self.assertEqual(self.pool._operation_lock_depth, 2)

        self.assertEqual(self.pool._operation_lock_depth, 0)
        self.assertIsNone(self.pool._operation_lock_fd)

    def test_use_profile_updates_live_files(self) -> None:
        self.seed_live_auth("rt-1", email="first@example.com")
        self.pool.save_current("first")
        self.seed_live_auth("rt-2", email="second@example.com")
        self.pool.save_current("second")

        summary = self.pool.use_profile("first")
        live_creds = json.loads(self.paths.live_creds_file.read_text(encoding="utf-8"))
        accounts = json.loads(self.paths.google_accounts_file.read_text(encoding="utf-8"))

        self.assertEqual(summary.name, "first")
        self.assertEqual(live_creds["refresh_token"], "rt-1")
        self.assertEqual(accounts["active"], "first@example.com")

    def test_next_profile_rotates(self) -> None:
        self.seed_live_auth("rt-1", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-2", email="b@example.com")
        self.pool.save_current("b")
        self.pool.use_profile("a")

        summary = self.pool.next_profile()
        self.assertEqual(summary.name, "b")

    def test_login_failure_restores_previous_live_auth(self) -> None:
        self.seed_live_auth("rt-1", email="keep@example.com")

        with patch("gemini_auth_switch.store.subprocess.run") as run_mock:
            run_mock.return_value.returncode = 1
            with self.assertRaisesRegex(Exception, "gemini login command failed"):
                self.pool.login("new-profile", gemini_bin="gemini")

        live_creds = json.loads(self.paths.live_creds_file.read_text(encoding="utf-8"))
        accounts = json.loads(self.paths.google_accounts_file.read_text(encoding="utf-8"))
        self.assertEqual(live_creds["refresh_token"], "rt-1")
        self.assertEqual(accounts["active"], "keep@example.com")

    def test_diagnostics_summary_reports_auth_and_cache_state(self) -> None:
        self.seed_live_auth("rt-1", email="diag@example.com")
        self.seed_settings()
        self.pool.save_current("diag")
        self.paths.token_cache_v2.write_text("cached", encoding="utf-8")

        summary = self.pool.diagnostics_summary()

        self.assertEqual(summary["profile"], "diag")
        self.assertEqual(summary["email"], "diag@example.com")
        self.assertEqual(summary["selected_auth_type"], "oauth-personal")
        self.assertFalse(summary["token_cache_v1_exists"])
        self.assertTrue(summary["token_cache_v2_exists"])

    def test_check_all_profiles_restores_original_live_auth(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")
        self.pool.use_profile("a")

        def fake_run(*_args, **_kwargs):
            live_creds = json.loads(self.paths.live_creds_file.read_text(encoding="utf-8"))
            refresh_token = live_creds["refresh_token"]
            if refresh_token == "rt-a":
                return SimpleNamespace(
                    returncode=0,
                    stdout="Loaded cached credentials.\npong\n",
                    stderr="",
                )
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="ValidationRequiredError: Verify your account to continue.\n",
            )

        with patch("gemini_auth_switch.store.subprocess.run", side_effect=fake_run):
            results = self.pool.check_all_profiles(delay_seconds=0.0)

        self.assertEqual([result.name for result in results], ["a", "b"])
        self.assertEqual([result.status for result in results], ["ok", "validation_required"])
        self.assertTrue(self.paths.check_state_file.exists())
        check_state = json.loads(self.paths.check_state_file.read_text(encoding="utf-8"))
        self.assertEqual(check_state["profiles"]["a"]["status"], "ok")
        self.assertEqual(check_state["profiles"]["b"]["status"], "validation_required")
        profiles = self.pool.list_profiles()
        self.assertEqual([profile.last_check_status for profile in profiles], ["ok", "validation_required"])

        live_creds = json.loads(self.paths.live_creds_file.read_text(encoding="utf-8"))
        self.assertEqual(live_creds["refresh_token"], "rt-a")
        self.assertEqual(self.pool.current_profile_name(), "a")

    def test_check_profile_restores_original_live_auth(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")
        self.pool.use_profile("a")

        def fake_run(*_args, **_kwargs):
            live_creds = json.loads(self.paths.live_creds_file.read_text(encoding="utf-8"))
            refresh_token = live_creds["refresh_token"]
            if refresh_token == "rt-b":
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="ValidationRequiredError: Verify your account to continue.\n",
                )
            return SimpleNamespace(returncode=0, stdout="pong\n", stderr="")

        with patch("gemini_auth_switch.store.subprocess.run", side_effect=fake_run):
            result = self.pool.check_profile("b")

        self.assertEqual(result.name, "b")
        self.assertEqual(result.status, "validation_required")
        self.assertEqual(
            json.loads(self.paths.check_state_file.read_text(encoding="utf-8"))["profiles"]["b"][
                "status"
            ],
            "validation_required",
        )
        live_creds = json.loads(self.paths.live_creds_file.read_text(encoding="utf-8"))
        self.assertEqual(live_creds["refresh_token"], "rt-a")
        self.assertEqual(self.pool.current_profile_name(), "a")

    def test_check_all_profiles_reports_progress_events(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")

        events: list[tuple[str, dict]] = []

        def fake_run(*_args, **_kwargs):
            return SimpleNamespace(returncode=0, stdout="pong\n", stderr="")

        def capture(event: str, payload: dict) -> None:
            events.append((event, payload))

        with patch("gemini_auth_switch.store.subprocess.run", side_effect=fake_run):
            self.pool.check_all_profiles(delay_seconds=0.0, progress_callback=capture)

        self.assertEqual(
            [event for event, _payload in events],
            ["start", "result", "start", "result"],
        )
        self.assertEqual(events[0][1]["name"], "a")
        self.assertEqual(events[2][1]["name"], "b")

    def test_probe_current_profile_handles_timeout_bytes_output(self) -> None:
        timeout = subprocess.TimeoutExpired(
            cmd=["gemini", "-p", "ping"],
            timeout=30.0,
            output=b"slow output\n",
            stderr=b"more detail\n",
        )

        with patch("gemini_auth_switch.store.subprocess.run", side_effect=timeout):
            status, detail, returncode = self.pool.probe_current_profile()

        self.assertEqual(status, "timeout")
        self.assertIsNone(returncode)
        self.assertIn("slow output", detail)
        self.assertIn("more detail", detail)

    def test_list_command_is_concise_by_default(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")

        with patch(
            "gemini_auth_switch.store.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="pong\n", stderr=""),
        ):
            self.pool.check_profile("a")

        output = io.StringIO()
        with patch("gemini_auth_switch.cli.GeminiPaths.from_home", return_value=self.paths):
            with redirect_stdout(output):
                exit_code = run(["list"])

        rendered = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("status=ok", rendered)
        self.assertIn("checked=", rendered)
        self.assertNotIn("detail=", rendered)
        self.assertNotIn("updated=", rendered)

    def test_list_command_verbose_includes_probe_detail(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")

        with patch(
            "gemini_auth_switch.store.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="pong\n", stderr=""),
        ):
            self.pool.check_profile("a")

        output = io.StringIO()
        with patch("gemini_auth_switch.cli.GeminiPaths.from_home", return_value=self.paths):
            with redirect_stdout(output):
                exit_code = run(["list", "--verbose"])

        rendered = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("status=ok", rendered)
        self.assertIn("checked=", rendered)
        self.assertIn("detail=pong", rendered)
        self.assertIn("updated=", rendered)

    def test_check_all_command_prints_final_results_summary(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")

        def fake_run(*_args, **_kwargs):
            live_creds = json.loads(self.paths.live_creds_file.read_text(encoding="utf-8"))
            refresh_token = live_creds["refresh_token"]
            if refresh_token == "rt-a":
                return SimpleNamespace(returncode=0, stdout="pong\n", stderr="")
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="ValidationRequiredError: Verify your account to continue.\n",
            )

        output = io.StringIO()
        with patch("gemini_auth_switch.cli.GeminiPaths.from_home", return_value=self.paths):
            with patch("gemini_auth_switch.store.subprocess.run", side_effect=fake_run):
                with redirect_stdout(output):
                    exit_code = run(["check-all", "--delay", "0"])

        rendered = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("results:", rendered)
        self.assertIn("status=ok", rendered)
        self.assertIn("status=validation_required", rendered)
        self.assertIn("detail=Verify your account to continue.", rendered)

    def test_parse_profile_stats_output_extracts_model_usage(self) -> None:
        result = self.pool.parse_profile_stats_output(
            "stats@example.com",
            "stats@example.com",
            make_stats_output("stats@example.com"),
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.session_id, "34ab025b-7c21-46d7-95c8-b4c549e6bdc8")
        self.assertEqual(result.auth_method, "Logged in with Google (stats@example.com)")
        self.assertEqual(result.tier, "Gemini Code Assist in Google One AI Pro")
        self.assertEqual(result.usage_label, "Auto (Gemini 3)")
        self.assertEqual(len(result.models), 3)
        self.assertEqual(result.models[0].model, "gemini-2.5-flash")
        self.assertEqual(result.models[0].requests, "-")
        self.assertEqual(result.models[0].usage_remaining, "96.4% resets in 22h 25m")
        self.assertEqual(result.models[0].remaining_percent, 96.4)
        self.assertEqual(result.models[0].reset_in, "22h 25m")
        self.assertEqual(result.detail, "models=3 lowest_remaining=93.3%")

    def test_stats_profile_restores_original_live_auth(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")
        self.pool.use_profile("a")

        with patch.object(
            GeminiAuthPool,
            "collect_current_profile_stats_output",
            return_value=(make_stats_output("b@example.com"), False),
        ):
            result = self.pool.stats_profile("b")

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.email, "b@example.com")
        self.assertEqual(len(result.models), 3)
        live_creds = json.loads(self.paths.live_creds_file.read_text(encoding="utf-8"))
        self.assertEqual(live_creds["refresh_token"], "rt-a")
        self.assertEqual(self.pool.current_profile_name(), "a")

    def test_stats_profile_persists_quota_state(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")

        with patch.object(
            GeminiAuthPool,
            "collect_current_profile_stats_output",
            return_value=(make_stats_output("b@example.com"), False),
        ):
            self.pool.stats_profile("b")

        quota_state = json.loads(self.paths.quota_state_file.read_text(encoding="utf-8"))
        self.assertEqual(quota_state["profiles"]["b"]["status"], "ok")
        self.assertEqual(quota_state["profiles"]["b"]["usage_label"], "Auto (Gemini 3)")
        self.assertEqual(quota_state["profiles"]["b"]["models"][0]["model"], "gemini-2.5-flash")

    def test_load_quota_result_defaults_legacy_source_to_stats(self) -> None:
        write_json(
            self.paths.quota_state_file,
            {
                "profiles": {
                    "legacy": {
                        "status": "ok",
                        "detail": "models=1 lowest_remaining=91.0%",
                        "checked_at": iso_now(),
                        "email": "legacy@example.com",
                        "models": [
                            {
                                "model": "gemini-3.1-pro-preview",
                                "requests": "-",
                                "usage_remaining": "91.0% resets in 10h",
                                "remaining_percent": 91.0,
                                "reset_in": "10h",
                            }
                        ],
                    }
                },
                "updated_at": iso_now(),
            },
        )

        result = self.pool.load_quota_result("legacy")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.source, QUOTA_SOURCE_STATS)

    def test_load_quota_result_reads_refresh_cooldown_fields(self) -> None:
        blocked_until = iso_now(600)
        reset_at = iso_now(3600)
        write_json(
            self.paths.quota_state_file,
            {
                "profiles": {
                    "cooldown": make_quota_state_entry(
                        "cooldown@example.com",
                        0.0,
                        checked_at=iso_now(-30),
                        source=QUOTA_SOURCE_API,
                        reset_in="1h",
                        reset_at=reset_at,
                        last_refresh_attempt_at=iso_now(-10),
                        last_refresh_status="rate_limited",
                        last_refresh_detail="Too many requests",
                        blocked_until=blocked_until,
                        blocked_reason="refresh rate limited",
                        failure_streak=2,
                    )
                },
                "updated_at": iso_now(),
            },
        )

        result = self.pool.load_quota_result("cooldown")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.models[0].reset_at, reset_at)
        self.assertEqual(result.blocked_until, blocked_until)
        self.assertEqual(result.last_refresh_status, "rate_limited")
        self.assertEqual(result.failure_streak, 2)

    def test_refresh_live_access_token_updates_live_creds(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")

        with patch.object(
            GeminiAuthPool,
            "load_code_assist_constants",
            return_value={"client_id": "cid", "client_secret": "secret"},
        ):
            with patch.object(
                GeminiAuthPool,
                "http_post_form",
                return_value={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "token_type": "Bearer",
                    "scope": "scope-2",
                    "id_token": "id-2",
                    "expires_in": 3600,
                },
            ):
                with patch("gemini_auth_switch.store.time.time", return_value=100.0):
                    refreshed = self.pool.refresh_live_access_token()

        self.assertEqual(refreshed["access_token"], "new-access")
        self.assertEqual(refreshed["refresh_token"], "new-refresh")
        self.assertEqual(refreshed["expiry_date"], 3_700_000)
        live_creds = json.loads(self.paths.live_creds_file.read_text(encoding="utf-8"))
        self.assertEqual(live_creds["access_token"], "new-access")
        self.assertEqual(live_creds["refresh_token"], "new-refresh")

    def test_api_quota_current_profile_reads_code_assist_quota(self) -> None:
        quota_responses = [
            {
                "currentTier": {"name": "Google One AI Pro", "id": "google_one_ai_premium"},
                "cloudaicompanionProject": "quota-project",
            },
            {
                "buckets": [
                    {
                        "modelId": "gemini-3.1-pro-preview",
                        "remainingFraction": 0.42,
                        "resetTime": "2099-01-01T01:00:00+00:00",
                    },
                    {
                        "modelId": "gemini-2.5-flash",
                        "remainingFraction": 0.90,
                        "resetTime": "2099-01-01T02:00:00+00:00",
                    },
                ]
            },
        ]

        with patch.object(GeminiAuthPool, "code_assist_post", side_effect=quota_responses):
            result = self.pool.api_quota_current_profile("a", "a@example.com")

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.source, QUOTA_SOURCE_API)
        self.assertEqual(result.project_id, "quota-project")
        self.assertEqual(result.tier, "Google One AI Pro")
        self.assertEqual(result.tier_id, "google_one_ai_premium")
        self.assertEqual([item.model for item in result.models], ["gemini-3.1-pro-preview", "gemini-2.5-flash"])
        self.assertEqual(result.models[0].remaining_percent, 42.0)
        self.assertTrue(result.models[0].usage_remaining.startswith("42.0%"))
        self.assertEqual(result.models[0].reset_at, "2099-01-01T01:00:00+00:00")

    def test_api_quota_current_profile_requires_project_for_project_tier(self) -> None:
        with patch.object(
            GeminiAuthPool,
            "code_assist_post",
            return_value={"currentTier": {"name": "Enterprise", "id": "enterprise"}},
        ):
            result = self.pool.api_quota_current_profile("corp", "corp@example.com")

        self.assertEqual(result.status, "project_required")
        self.assertEqual(result.source, QUOTA_SOURCE_API)
        self.assertEqual(
            result.detail,
            "This account requires GOOGLE_CLOUD_PROJECT or GOOGLE_CLOUD_PROJECT_ID.",
        )

    def test_refresh_profile_quota_persists_api_result_and_syncs_profile_creds(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")
        self.pool.use_profile("a")

        def fake_api_quota(
            pool: GeminiAuthPool,
            name: str,
            email: str | None,
            gemini_bin: str = "gemini",
        ):
            live_creds = json.loads(pool.paths.live_creds_file.read_text(encoding="utf-8"))
            live_creds["access_token"] = "fresh-access-token"
            live_creds["expiry_date"] = 999_999
            write_json(pool.paths.live_creds_file, live_creds)
            return pool.make_stats_result(
                name=name,
                email=email,
                status="ok",
                detail="models=1 lowest_remaining=88.0%",
                tier="Google One AI Pro",
                tier_id="google_one_ai_premium",
                source=QUOTA_SOURCE_API,
                project_id="quota-project",
                models=[
                    ModelUsageStat(
                        model="gemini-3.1-pro-preview",
                        requests="-",
                        usage_remaining="88.0% resets in 10h",
                        remaining_percent=88.0,
                        reset_in="10h",
                    )
                ],
            )

        with patch.object(GeminiAuthPool, "api_quota_current_profile", fake_api_quota):
            result = self.pool.refresh_profile_quota("b")

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.source, QUOTA_SOURCE_API)
        quota_state = json.loads(self.paths.quota_state_file.read_text(encoding="utf-8"))
        self.assertEqual(quota_state["profiles"]["b"]["source"], QUOTA_SOURCE_API)
        self.assertEqual(quota_state["profiles"]["b"]["project_id"], "quota-project")

        profile_creds = json.loads(self.paths.profile_creds_file("b").read_text(encoding="utf-8"))
        self.assertEqual(profile_creds["access_token"], "fresh-access-token")

        live_creds = json.loads(self.paths.live_creds_file.read_text(encoding="utf-8"))
        self.assertEqual(live_creds["refresh_token"], "rt-a")
        self.assertEqual(self.pool.current_profile_name(), "a")

    def test_refresh_profile_quota_preserves_previous_cache_on_rate_limit(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")

        write_json(
            self.paths.quota_state_file,
            {
                "profiles": {
                    "a": make_quota_state_entry(
                        "a@example.com",
                        76.0,
                        checked_at=iso_now(-60),
                        source=QUOTA_SOURCE_API,
                    )
                },
                "updated_at": iso_now(-60),
            },
        )

        def fake_api_quota(
            pool: GeminiAuthPool,
            name: str,
            email: str | None,
            gemini_bin: str = "gemini",
        ):
            return pool.make_stats_result(
                name=name,
                email=email,
                status="rate_limited",
                detail="Too many requests",
                source=QUOTA_SOURCE_API,
                retry_after_seconds=120.0,
            )

        with patch.object(GeminiAuthPool, "api_quota_current_profile", fake_api_quota):
            result = self.pool.refresh_profile_quota("a")

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.models[0].remaining_percent, 76.0)
        self.assertEqual(result.last_refresh_status, "rate_limited")
        self.assertEqual(result.failure_streak, 1)
        self.assertIsNotNone(result.blocked_until)

    def test_stats_all_command_prints_model_usage(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")

        outputs = {
            "a@example.com": (make_stats_output("a@example.com"), False),
            "b@example.com": (make_stats_output("b@example.com"), False),
        }

        def fake_collect(self: GeminiAuthPool, *_args, **_kwargs):
            current_email = self.load_live_email()
            assert current_email in outputs
            return outputs[current_email]

        output = io.StringIO()
        with patch("gemini_auth_switch.cli.GeminiPaths.from_home", return_value=self.paths):
            with patch.object(GeminiAuthPool, "collect_current_profile_stats_output", fake_collect):
                with redirect_stdout(output):
                    exit_code = run(["stats-all", "--delay", "0"])

        rendered = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("summary total=2 ok=2", rendered)
        self.assertIn("model=gemini-2.5-flash requests=- remaining=96.4% resets in 22h 25m", rendered)
        self.assertIn("usage=Auto (Gemini 3)", rendered)

    def test_quota_command_reads_cached_state_for_current_profile(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.pool.use_profile("a")

        with patch.object(
            GeminiAuthPool,
            "collect_current_profile_stats_output",
            return_value=(make_stats_output("a@example.com"), False),
        ):
            self.pool.stats_profile("a")

        output = io.StringIO()
        with patch("gemini_auth_switch.cli.GeminiPaths.from_home", return_value=self.paths):
            with redirect_stdout(output):
                exit_code = run(["quota"])

        rendered = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("quota=ok profile=a", rendered)
        self.assertIn("lowest=93.3%", rendered)
        self.assertIn("model=gemini-2.5-flash requests=- remaining=96.4% resets in 22h 25m", rendered)

    def test_quota_all_command_is_concise_and_marks_missing_cache(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")
        self.pool.use_profile("a")

        with patch.object(
            GeminiAuthPool,
            "collect_current_profile_stats_output",
            return_value=(make_stats_output("a@example.com"), False),
        ):
            self.pool.stats_profile("a")

        output = io.StringIO()
        with patch("gemini_auth_switch.cli.GeminiPaths.from_home", return_value=self.paths):
            with redirect_stdout(output):
                exit_code = run(["quota-all"])

        rendered = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("* a ", rendered)
        self.assertIn("quota=ok", rendered)
        self.assertIn("lowest=93.3%", rendered)
        self.assertIn("quota=-", rendered)
        self.assertIn("models=0", rendered)
        self.assertNotIn("model=gemini-2.5-flash", rendered)

    def test_pick_profile_uses_keyword_matching_and_skips_unhealthy_profiles(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")
        self.seed_live_auth("rt-c", email="c@example.com")
        self.pool.save_current("c")
        self.pool.use_profile("a")

        write_json(
            self.paths.quota_state_file,
            {
                "profiles": {
                    "a": {
                        "status": "ok",
                        "detail": "models=2 lowest_remaining=90.0%",
                        "checked_at": "2026-03-17T10:00:00+00:00",
                        "email": "a@example.com",
                        "tier": "tier-a",
                        "usage_label": "Auto (Gemini 3)",
                        "models": [
                            {
                                "model": "gemini-3.1-pro-preview",
                                "requests": "-",
                                "usage_remaining": "90.0% resets in 10h",
                                "remaining_percent": 90.0,
                                "reset_in": "10h",
                            }
                        ],
                    },
                    "b": {
                        "status": "ok",
                        "detail": "models=2 lowest_remaining=96.0%",
                        "checked_at": "2026-03-17T10:05:00+00:00",
                        "email": "b@example.com",
                        "tier": "tier-b",
                        "usage_label": "Auto (Gemini 3)",
                        "models": [
                            {
                                "model": "gemini-3.1-pro-preview",
                                "requests": "-",
                                "usage_remaining": "96.0% resets in 12h",
                                "remaining_percent": 96.0,
                                "reset_in": "12h",
                            }
                        ],
                    },
                    "c": {
                        "status": "ok",
                        "detail": "models=2 lowest_remaining=99.0%",
                        "checked_at": "2026-03-17T10:06:00+00:00",
                        "email": "c@example.com",
                        "tier": "tier-c",
                        "usage_label": "Auto (Gemini 3)",
                        "models": [
                            {
                                "model": "gemini-3.1-pro-preview",
                                "requests": "-",
                                "usage_remaining": "99.0% resets in 20h",
                                "remaining_percent": 99.0,
                                "reset_in": "20h",
                            }
                        ],
                    },
                },
                "updated_at": "2026-03-17T10:06:00+00:00",
            },
        )
        write_json(
            self.paths.check_state_file,
            {
                "profiles": {
                    "a": {"status": "ok", "checked_at": "2026-03-17T09:00:00+00:00"},
                    "b": {"status": "ok", "checked_at": "2026-03-17T09:05:00+00:00"},
                    "c": {
                        "status": "validation_required",
                        "checked_at": "2026-03-17T09:10:00+00:00",
                    },
                },
                "updated_at": "2026-03-17T09:10:00+00:00",
            },
        )

        result = self.pool.pick_profile(["3.1-pro"])

        self.assertEqual(result.name, "b")
        self.assertEqual(result.matched_model, "gemini-3.1-pro-preview")
        self.assertEqual(result.remaining_percent, 96.0)
        self.assertEqual(result.health_status, "ok")

    def test_pick_command_prints_selected_cached_profile(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")
        self.pool.use_profile("a")

        write_json(
            self.paths.quota_state_file,
            {
                "profiles": {
                    "a": {
                        "status": "ok",
                        "detail": "models=1 lowest_remaining=90.0%",
                        "checked_at": "2026-03-17T10:00:00+00:00",
                        "email": "a@example.com",
                        "tier": "tier-a",
                        "usage_label": "Auto (Gemini 3)",
                        "models": [
                            {
                                "model": "gemini-3.1-pro-preview",
                                "requests": "-",
                                "usage_remaining": "90.0% resets in 10h",
                                "remaining_percent": 90.0,
                                "reset_in": "10h",
                            }
                        ],
                    },
                    "b": {
                        "status": "ok",
                        "detail": "models=1 lowest_remaining=97.0%",
                        "checked_at": "2026-03-17T10:05:00+00:00",
                        "email": "b@example.com",
                        "tier": "tier-b",
                        "usage_label": "Auto (Gemini 3)",
                        "models": [
                            {
                                "model": "gemini-3.1-pro-preview",
                                "requests": "-",
                                "usage_remaining": "97.0% resets in 11h",
                                "remaining_percent": 97.0,
                                "reset_in": "11h",
                            }
                        ],
                    },
                },
                "updated_at": "2026-03-17T10:05:00+00:00",
            },
        )
        write_json(
            self.paths.check_state_file,
            {
                "profiles": {
                    "a": {"status": "ok", "checked_at": "2026-03-17T09:00:00+00:00"},
                    "b": {"status": "ok", "checked_at": "2026-03-17T09:05:00+00:00"},
                },
                "updated_at": "2026-03-17T09:05:00+00:00",
            },
        )

        output = io.StringIO()
        with patch("gemini_auth_switch.cli.GeminiPaths.from_home", return_value=self.paths):
            with redirect_stdout(output):
                exit_code = run(["pick", "--match", "3.1-pro"])

        rendered = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("picked profile=b", rendered)
        self.assertIn("model=gemini-3.1-pro-preview", rendered)
        self.assertIn("remaining=97.0% resets in 11h", rendered)
        self.assertIn("filters match=3.1-pro", rendered)

    def test_pick_profile_supports_match_any_and_exclude_terms(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")

        write_json(
            self.paths.quota_state_file,
            {
                "profiles": {
                    "a": {
                        "status": "ok",
                        "detail": "models=2 lowest_remaining=96.0%",
                        "checked_at": "2026-03-17T10:00:00+00:00",
                        "email": "a@example.com",
                        "source": QUOTA_SOURCE_API,
                        "models": [
                            {
                                "model": "gemini-3.1-flash-lite-preview",
                                "requests": "-",
                                "usage_remaining": "99.0% resets in 11h",
                                "remaining_percent": 99.0,
                                "reset_in": "11h",
                            },
                            {
                                "model": "gemini-2.5-pro",
                                "requests": "-",
                                "usage_remaining": "96.0% resets in 11h",
                                "remaining_percent": 96.0,
                                "reset_in": "11h",
                            },
                        ],
                    },
                    "b": {
                        "status": "ok",
                        "detail": "models=2 lowest_remaining=72.0%",
                        "checked_at": "2026-03-17T10:05:00+00:00",
                        "email": "b@example.com",
                        "source": QUOTA_SOURCE_API,
                        "models": [
                            {
                                "model": "gemini-3.1-flash-lite-preview",
                                "requests": "-",
                                "usage_remaining": "98.0% resets in 11h",
                                "remaining_percent": 98.0,
                                "reset_in": "11h",
                            },
                            {
                                "model": "gemini-3.1-pro-preview",
                                "requests": "-",
                                "usage_remaining": "72.0% resets in 11h",
                                "remaining_percent": 72.0,
                                "reset_in": "11h",
                            },
                        ],
                    },
                },
                "updated_at": "2026-03-17T10:05:00+00:00",
            },
        )
        write_json(
            self.paths.check_state_file,
            {
                "profiles": {
                    "a": {"status": "ok", "checked_at": "2026-03-17T09:00:00+00:00"},
                    "b": {"status": "ok", "checked_at": "2026-03-17T09:05:00+00:00"},
                },
                "updated_at": "2026-03-17T09:05:00+00:00",
            },
        )

        result = self.pool.pick_profile(
            match_any_terms=["gemini-3"],
            exclude_terms=["lite"],
        )

        self.assertEqual(result.name, "b")
        self.assertEqual(result.matched_model, "gemini-3.1-pro-preview")
        self.assertEqual(result.match_any_terms, ["gemini-3"])
        self.assertEqual(result.exclude_match_terms, ["lite"])

    def test_pick_command_prints_match_any_and_exclude_filters(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")

        write_json(
            self.paths.quota_state_file,
            {
                "profiles": {
                    "a": {
                        "status": "ok",
                        "detail": "models=1 lowest_remaining=73.0%",
                        "checked_at": "2026-03-17T10:00:00+00:00",
                        "email": "a@example.com",
                        "source": QUOTA_SOURCE_API,
                        "models": [
                            {
                                "model": "gemini-3.1-pro-preview",
                                "requests": "-",
                                "usage_remaining": "73.0% resets in 11h",
                                "remaining_percent": 73.0,
                                "reset_in": "11h",
                            }
                        ],
                    },
                },
                "updated_at": "2026-03-17T10:00:00+00:00",
            },
        )

        output = io.StringIO()
        with patch("gemini_auth_switch.cli.GeminiPaths.from_home", return_value=self.paths):
            with redirect_stdout(output):
                exit_code = run(["pick", "--match-any", "gemini-3", "--exclude-match", "lite"])

        rendered = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("filters match=- match_any=gemini-3 exclude=lite", rendered)

    def test_quota_result_is_not_stale_during_failure_cooldown(self) -> None:
        result = self.pool.load_quota_result("missing")
        self.assertIsNone(result)

        cached = self.pool.make_stats_result(
            name="a",
            email="a@example.com",
            status="ok",
            detail="models=1 lowest_remaining=77.0%",
            source=QUOTA_SOURCE_API,
            models=[
                ModelUsageStat(
                    model="gemini-3.1-pro-preview",
                    requests="-",
                    usage_remaining="77.0% resets in 10h",
                    remaining_percent=77.0,
                    reset_in="10h",
                    reset_at=iso_now(3600),
                )
            ],
            last_refresh_attempt_at=iso_now(-10),
            last_refresh_status="rate_limited",
            last_refresh_detail="Too many requests",
            blocked_until=iso_now(600),
            blocked_reason="refresh rate limited",
            failure_streak=2,
        )

        self.assertFalse(self.pool.quota_result_is_stale(cached, ["3.1-pro"], 300.0))

    def test_quota_result_is_not_stale_when_matched_model_waits_for_reset(self) -> None:
        cached = self.pool.make_stats_result(
            name="a",
            email="a@example.com",
            status="ok",
            detail="models=1 lowest_remaining=0.0%",
            source=QUOTA_SOURCE_API,
            models=[
                ModelUsageStat(
                    model="gemini-3.1-pro-preview",
                    requests="-",
                    usage_remaining="0.0% resets in 1h",
                    remaining_percent=0.0,
                    reset_in="1h",
                    reset_at=iso_now(3600),
                )
            ],
        )

        self.assertFalse(self.pool.quota_result_is_stale(cached, ["3.1-pro"], 0.0))

    def test_auto_use_profile_refreshes_stale_current_and_keeps_it(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")
        self.pool.use_profile("a")

        write_json(
            self.paths.quota_state_file,
            {
                "profiles": {
                    "a": make_quota_state_entry(
                        "a@example.com",
                        4.0,
                        checked_at=iso_now(-30),
                        source=QUOTA_SOURCE_STATS,
                    ),
                    "b": make_quota_state_entry(
                        "b@example.com",
                        99.0,
                        checked_at=iso_now(-30),
                        source=QUOTA_SOURCE_API,
                    ),
                },
                "updated_at": iso_now(-30),
            },
        )
        write_json(
            self.paths.check_state_file,
            {
                "profiles": {
                    "a": {"status": "ok", "checked_at": iso_now(-120)},
                    "b": {"status": "ok", "checked_at": iso_now(-120)},
                },
                "updated_at": iso_now(-120),
            },
        )

        refreshed: list[str] = []

        def fake_refresh(pool: GeminiAuthPool, name: str, gemini_bin: str = "gemini"):
            refreshed.append(name)
            result = pool.make_stats_result(
                name=name,
                email=f"{name}@example.com",
                status="ok",
                detail="models=1 lowest_remaining=82.0%",
                tier="Google One AI Pro",
                tier_id="google_one_ai_premium",
                source=QUOTA_SOURCE_API,
                project_id="quota-project",
                models=[
                    ModelUsageStat(
                        model="gemini-3.1-pro-preview",
                        requests="-",
                        usage_remaining="82.0% resets in 10h",
                        remaining_percent=82.0,
                        reset_in="10h",
                    )
                ],
            )
            result.checked_at = iso_now()
            pool.write_quota_result(result)
            return result

        with patch.object(GeminiAuthPool, "refresh_profile_quota", fake_refresh):
            decision = self.pool.auto_use_profile(
                match_terms=["3.1-pro"],
                min_remaining_percent=15.0,
            )

        self.assertEqual(decision.action, "keep")
        self.assertEqual(decision.selected.name, "a")
        self.assertEqual(refreshed, ["a"])
        self.assertEqual(self.pool.current_profile_name(), "a")

    def test_auto_use_profile_refreshes_candidate_when_current_is_below_threshold(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")
        self.pool.use_profile("a")

        write_json(
            self.paths.quota_state_file,
            {
                "profiles": {
                    "a": make_quota_state_entry(
                        "a@example.com",
                        4.0,
                        checked_at=iso_now(-10),
                        source=QUOTA_SOURCE_API,
                    ),
                },
                "updated_at": iso_now(-10),
            },
        )
        write_json(
            self.paths.check_state_file,
            {
                "profiles": {
                    "a": {"status": "ok", "checked_at": iso_now(-120)},
                    "b": {"status": "ok", "checked_at": iso_now(-120)},
                },
                "updated_at": iso_now(-120),
            },
        )

        refreshed: list[str] = []

        def fake_refresh(pool: GeminiAuthPool, name: str, gemini_bin: str = "gemini"):
            refreshed.append(name)
            result = pool.make_stats_result(
                name=name,
                email=f"{name}@example.com",
                status="ok",
                detail="models=1 lowest_remaining=96.0%",
                tier="Google One AI Pro",
                tier_id="google_one_ai_premium",
                source=QUOTA_SOURCE_API,
                project_id="quota-project",
                models=[
                    ModelUsageStat(
                        model="gemini-3.1-pro-preview",
                        requests="-",
                        usage_remaining="96.0% resets in 11h",
                        remaining_percent=96.0,
                        reset_in="11h",
                    )
                ],
            )
            result.checked_at = iso_now()
            pool.write_quota_result(result)
            return result

        with patch.object(GeminiAuthPool, "refresh_profile_quota", fake_refresh):
            decision = self.pool.auto_use_profile(
                match_terms=["3.1-pro"],
                min_remaining_percent=15.0,
                candidate_refresh_limit=1,
            )

        self.assertEqual(decision.action, "switch")
        self.assertEqual(decision.current_profile, "a")
        self.assertEqual(decision.selected.name, "b")
        self.assertEqual(refreshed, ["b"])
        self.assertEqual(self.pool.current_profile_name(), "b")

    def test_auto_use_profile_avoids_requested_profile(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")
        self.pool.use_profile("a")

        write_json(
            self.paths.quota_state_file,
            {
                "profiles": {
                    "a": make_quota_state_entry(
                        "a@example.com",
                        96.0,
                        checked_at=iso_now(-10),
                        source=QUOTA_SOURCE_API,
                    ),
                    "b": make_quota_state_entry(
                        "b@example.com",
                        82.0,
                        checked_at=iso_now(-10),
                        source=QUOTA_SOURCE_API,
                    ),
                },
                "updated_at": iso_now(-10),
            },
        )
        write_json(
            self.paths.check_state_file,
            {
                "profiles": {
                    "a": {"status": "ok", "checked_at": iso_now(-120)},
                    "b": {"status": "ok", "checked_at": iso_now(-120)},
                },
                "updated_at": iso_now(-120),
            },
        )

        decision = self.pool.auto_use_profile(
            match_terms=["3.1-pro"],
            min_remaining_percent=15.0,
            candidate_refresh_limit=0,
            avoid_profiles=["a"],
        )

        self.assertEqual(decision.action, "switch")
        self.assertEqual(decision.current_profile, "a")
        self.assertEqual(decision.selected.name, "b")
        self.assertEqual(self.pool.current_profile_name(), "b")

    def test_auto_use_profile_excludes_lite_models_from_threshold_decision(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")
        self.pool.use_profile("a")

        write_json(
            self.paths.quota_state_file,
            {
                "profiles": {
                    "a": {
                        "status": "ok",
                        "detail": "models=2 lowest_remaining=4.0%",
                        "checked_at": iso_now(-10),
                        "email": "a@example.com",
                        "source": QUOTA_SOURCE_API,
                        "models": [
                            {
                                "model": "gemini-3.1-flash-lite-preview",
                                "requests": "-",
                                "usage_remaining": "99.0% resets in 11h",
                                "remaining_percent": 99.0,
                                "reset_in": "11h",
                            },
                            {
                                "model": "gemini-3.1-pro-preview",
                                "requests": "-",
                                "usage_remaining": "4.0% resets in 11h",
                                "remaining_percent": 4.0,
                                "reset_in": "11h",
                            },
                        ],
                    },
                    "b": {
                        "status": "ok",
                        "detail": "models=2 lowest_remaining=82.0%",
                        "checked_at": iso_now(-10),
                        "email": "b@example.com",
                        "source": QUOTA_SOURCE_API,
                        "models": [
                            {
                                "model": "gemini-3.1-flash-lite-preview",
                                "requests": "-",
                                "usage_remaining": "98.0% resets in 11h",
                                "remaining_percent": 98.0,
                                "reset_in": "11h",
                            },
                            {
                                "model": "gemini-3.1-pro-preview",
                                "requests": "-",
                                "usage_remaining": "82.0% resets in 11h",
                                "remaining_percent": 82.0,
                                "reset_in": "11h",
                            },
                        ],
                    },
                },
                "updated_at": iso_now(-10),
            },
        )
        write_json(
            self.paths.check_state_file,
            {
                "profiles": {
                    "a": {"status": "ok", "checked_at": iso_now(-120)},
                    "b": {"status": "ok", "checked_at": iso_now(-120)},
                },
                "updated_at": iso_now(-120),
            },
        )

        decision = self.pool.auto_use_profile(
            match_any_terms=["gemini-3"],
            exclude_terms=["lite"],
            min_remaining_percent=15.0,
            candidate_refresh_limit=0,
        )

        self.assertEqual(decision.action, "switch")
        self.assertEqual(decision.current_profile, "a")
        self.assertEqual(decision.selected.name, "b")
        self.assertEqual(self.pool.current_profile_name(), "b")

    def test_auto_use_profile_respects_candidate_refresh_limit(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")
        self.seed_live_auth("rt-c", email="c@example.com")
        self.pool.save_current("c")
        self.pool.use_profile("a")

        write_json(
            self.paths.quota_state_file,
            {
                "profiles": {
                    "a": make_quota_state_entry(
                        "a@example.com",
                        5.0,
                        checked_at=iso_now(-10),
                        source=QUOTA_SOURCE_API,
                    ),
                },
                "updated_at": iso_now(-10),
            },
        )
        write_json(
            self.paths.check_state_file,
            {
                "profiles": {
                    "a": {"status": "ok", "checked_at": iso_now(-120)},
                    "b": {"status": "ok", "checked_at": iso_now(-120)},
                    "c": {"status": "ok", "checked_at": iso_now(-120)},
                },
                "updated_at": iso_now(-120),
            },
        )

        refreshed: list[str] = []

        def fake_refresh(pool: GeminiAuthPool, name: str, gemini_bin: str = "gemini"):
            refreshed.append(name)
            remaining_percent = 80.0 if name == "b" else 99.0
            result = pool.make_stats_result(
                name=name,
                email=f"{name}@example.com",
                status="ok",
                detail=f"models=1 lowest_remaining={remaining_percent:.1f}%",
                tier="Google One AI Pro",
                tier_id="google_one_ai_premium",
                source=QUOTA_SOURCE_API,
                project_id="quota-project",
                models=[
                    ModelUsageStat(
                        model="gemini-3.1-pro-preview",
                        requests="-",
                        usage_remaining=f"{remaining_percent:.1f}% resets in 10h",
                        remaining_percent=remaining_percent,
                        reset_in="10h",
                    )
                ],
            )
            result.checked_at = iso_now()
            pool.write_quota_result(result)
            return result

        with patch.object(GeminiAuthPool, "refresh_profile_quota", fake_refresh):
            decision = self.pool.auto_use_profile(
                match_terms=["3.1-pro"],
                min_remaining_percent=15.0,
                candidate_refresh_limit=1,
            )

        self.assertEqual(decision.action, "switch")
        self.assertEqual(decision.selected.name, "b")
        self.assertEqual(refreshed, ["b"])

    def test_auto_use_profile_skips_candidate_refresh_during_cooldown(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")
        self.seed_live_auth("rt-c", email="c@example.com")
        self.pool.save_current("c")
        self.pool.use_profile("a")

        write_json(
            self.paths.quota_state_file,
            {
                "profiles": {
                    "a": make_quota_state_entry(
                        "a@example.com",
                        4.0,
                        checked_at=iso_now(-10),
                        source=QUOTA_SOURCE_API,
                    ),
                    "b": make_quota_state_entry(
                        "b@example.com",
                        95.0,
                        checked_at=iso_now(-120),
                        source=QUOTA_SOURCE_API,
                        last_refresh_attempt_at=iso_now(-10),
                        last_refresh_status="rate_limited",
                        last_refresh_detail="Too many requests",
                        blocked_until=iso_now(600),
                        blocked_reason="refresh rate limited",
                        failure_streak=1,
                    ),
                    "c": make_quota_state_entry(
                        "c@example.com",
                        80.0,
                        checked_at=iso_now(-120),
                        source=QUOTA_SOURCE_STATS,
                    ),
                },
                "updated_at": iso_now(-10),
            },
        )
        write_json(
            self.paths.check_state_file,
            {
                "profiles": {
                    "a": {"status": "ok", "checked_at": iso_now(-120)},
                    "b": {"status": "ok", "checked_at": iso_now(-120)},
                    "c": {"status": "ok", "checked_at": iso_now(-120)},
                },
                "updated_at": iso_now(-120),
            },
        )

        refreshed: list[str] = []

        def fake_refresh(pool: GeminiAuthPool, name: str, gemini_bin: str = "gemini"):
            refreshed.append(name)
            remaining_percent = 82.0 if name == "c" else 99.0
            result = pool.make_stats_result(
                name=name,
                email=f"{name}@example.com",
                status="ok",
                detail=f"models=1 lowest_remaining={remaining_percent:.1f}%",
                tier="Google One AI Pro",
                tier_id="google_one_ai_premium",
                source=QUOTA_SOURCE_API,
                project_id="quota-project",
                models=[
                    ModelUsageStat(
                        model="gemini-3.1-pro-preview",
                        requests="-",
                        usage_remaining=f"{remaining_percent:.1f}% resets in 11h",
                        remaining_percent=remaining_percent,
                        reset_in="11h",
                        reset_at=iso_now(39600),
                    )
                ],
            )
            result.checked_at = iso_now()
            pool.write_quota_result(result)
            return result

        with patch.object(GeminiAuthPool, "refresh_profile_quota", fake_refresh):
            decision = self.pool.auto_use_profile(
                match_terms=["3.1-pro"],
                min_remaining_percent=15.0,
                candidate_refresh_limit=2,
            )

        self.assertEqual(decision.action, "switch")
        self.assertEqual(decision.selected.name, "b")
        self.assertEqual(refreshed, ["c"])

    def test_mark_profile_rate_limited_excludes_profile_from_pick(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")

        write_json(
            self.paths.quota_state_file,
            {
                "profiles": {
                    "a": make_quota_state_entry(
                        "a@example.com",
                        91.0,
                        checked_at=iso_now(-10),
                        source=QUOTA_SOURCE_API,
                    ),
                },
                "updated_at": iso_now(-10),
            },
        )

        result = self.pool.mark_profile_rate_limited(
            "a",
            detail="429 Too Many Requests",
            retry_after_seconds=45.0,
        )

        self.assertEqual(result.status, "rate_limited")
        self.assertEqual(result.last_refresh_status, "rate_limited")
        self.assertEqual(result.blocked_reason, "request rate limited")
        self.assertIsNotNone(result.blocked_until)
        self.assertEqual(result.model_count(), 1)

        stored = self.pool.quota_profile("a")
        self.assertEqual(stored.status, "rate_limited")
        self.assertEqual(stored.model_count(), 1)
        with self.assertRaisesRegex(PoolError, "no eligible cached quota match"):
            self.pool.pick_profile(["3.1-pro"])

    def test_auto_use_command_prints_switch_decision(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")
        self.seed_live_auth("rt-b", email="b@example.com")
        self.pool.save_current("b")
        self.pool.use_profile("a")

        write_json(
            self.paths.quota_state_file,
            {
                "profiles": {
                    "a": make_quota_state_entry(
                        "a@example.com",
                        5.0,
                        checked_at=iso_now(-10),
                        source=QUOTA_SOURCE_API,
                    ),
                },
                "updated_at": iso_now(-10),
            },
        )
        write_json(
            self.paths.check_state_file,
            {
                "profiles": {
                    "a": {"status": "ok", "checked_at": iso_now(-120)},
                    "b": {"status": "ok", "checked_at": iso_now(-120)},
                },
                "updated_at": iso_now(-120),
            },
        )

        def fake_refresh(pool: GeminiAuthPool, name: str, gemini_bin: str = "gemini"):
            result = pool.make_stats_result(
                name=name,
                email=f"{name}@example.com",
                status="ok",
                detail="models=1 lowest_remaining=96.0%",
                tier="Google One AI Pro",
                tier_id="google_one_ai_premium",
                source=QUOTA_SOURCE_API,
                project_id="quota-project",
                models=[
                    ModelUsageStat(
                        model="gemini-3.1-pro-preview",
                        requests="-",
                        usage_remaining="96.0% resets in 11h",
                        remaining_percent=96.0,
                        reset_in="11h",
                    )
                ],
            )
            result.checked_at = iso_now()
            pool.write_quota_result(result)
            return result

        output = io.StringIO()
        with patch("gemini_auth_switch.cli.GeminiPaths.from_home", return_value=self.paths):
            with patch.object(GeminiAuthPool, "refresh_profile_quota", fake_refresh):
                with redirect_stdout(output):
                    exit_code = run(
                        [
                            "auto-use",
                            "--match",
                            "3.1-pro",
                            "--min-remaining",
                            "15",
                            "--candidate-refresh-limit",
                            "1",
                        ]
                    )

        rendered = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("switched from=a to=b", rendered)
        self.assertIn("threshold=15.0%", rendered)
        self.assertIn("filters match=3.1-pro", rendered)

    def test_mark_rate_limited_command_updates_quota_state(self) -> None:
        self.seed_live_auth("rt-a", email="a@example.com")
        self.pool.save_current("a")

        output = io.StringIO()
        with patch("gemini_auth_switch.cli.GeminiPaths.from_home", return_value=self.paths):
            with redirect_stdout(output):
                exit_code = run(
                    [
                        "mark-rate-limited",
                        "a",
                        "--detail",
                        "429 Too Many Requests",
                        "--retry-after",
                        "45",
                    ]
                )

        rendered = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("marked profile=a quota=rate_limited", rendered)
        stored = self.pool.quota_profile("a")
        self.assertEqual(stored.status, "rate_limited")
        self.assertIsNotNone(stored.blocked_until)


if __name__ == "__main__":
    unittest.main()
