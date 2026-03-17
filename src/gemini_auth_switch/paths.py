from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GeminiPaths:
    gemini_dir: Path

    @classmethod
    def from_home(cls, home: Path | None = None) -> "GeminiPaths":
        base_home = home or Path.home()
        return cls(base_home / ".gemini")

    @property
    def live_creds_file(self) -> Path:
        return self.gemini_dir / "oauth_creds.json"

    @property
    def live_account_id_file(self) -> Path:
        return self.gemini_dir / "google_account_id"

    @property
    def google_accounts_file(self) -> Path:
        return self.gemini_dir / "google_accounts.json"

    @property
    def settings_file(self) -> Path:
        return self.gemini_dir / "settings.json"

    @property
    def profiles_dir(self) -> Path:
        return self.gemini_dir / "auth_profiles"

    @property
    def state_file(self) -> Path:
        return self.gemini_dir / "auth_pool_state.json"

    @property
    def check_state_file(self) -> Path:
        return self.gemini_dir / "auth_check_state.json"

    @property
    def quota_state_file(self) -> Path:
        return self.gemini_dir / "auth_quota_state.json"

    @property
    def operation_lock_file(self) -> Path:
        return self.gemini_dir / "auth_switch.lock"

    @property
    def token_cache_v1(self) -> Path:
        return self.gemini_dir / "mcp-oauth-tokens.json"

    @property
    def token_cache_v2(self) -> Path:
        return self.gemini_dir / "mcp-oauth-tokens-v2.json"

    def profile_dir(self, profile_name: str) -> Path:
        return self.profiles_dir / profile_name

    def profile_meta_file(self, profile_name: str) -> Path:
        return self.profile_dir(profile_name) / "profile.json"

    def profile_creds_file(self, profile_name: str) -> Path:
        return self.profile_dir(profile_name) / "oauth_creds.json"

    def profile_account_id_file(self, profile_name: str) -> Path:
        return self.profile_dir(profile_name) / "google_account_id"
