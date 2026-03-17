from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from gemini_auth_switch.paths import GeminiPaths
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

    def test_save_and_list_profile(self) -> None:
        self.seed_live_auth("rt-1")
        summary = self.pool.save_current()
        self.assertEqual(summary.name, "user@example.com")
        profiles = self.pool.list_profiles()
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].email, "user@example.com")

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


if __name__ == "__main__":
    unittest.main()
