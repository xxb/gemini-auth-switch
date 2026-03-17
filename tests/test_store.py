from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from gemini_auth_switch.paths import GeminiPaths
from gemini_auth_switch.cli import run
from gemini_auth_switch.store import GeminiAuthPool


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


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


if __name__ == "__main__":
    unittest.main()
