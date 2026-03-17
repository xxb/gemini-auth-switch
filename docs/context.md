# Context

## Why This Exists

One Gemini account is not enough for the intended workload. The goal is to manage several local Gemini CLI OAuth accounts and switch between them cleanly when quota is exhausted.

## Environment Assumptions

These assumptions were verified on 2026-03-17 on the development host:

- The real Gemini CLI is installed and available as `gemini`.
- The local installed version is `0.33.2`.
- The official CLI still uses `~/.gemini/settings.json`, `~/.gemini/oauth_creds.json`, and `~/.gemini/google_accounts.json` for the normal `oauth-personal` flow on this host.
- `~/.gemini/google_account_id` may still exist from older logins, so keep compatibility with it, but the current upstream CLI code no longer appears to depend on it for the main OAuth path.
- The host also keeps unrelated global files such as `installation_id`, `projects.json`, `state.json`, `trustedFolders.json`, and `user_id`; these are not account-switch files.
- Hook support exists for `BeforeAgent` and `AfterAgent`.
- Custom commands still load from `~/.gemini/commands`.

## Auth Findings

- A successful local switch means the live `oauth_creds.json` fingerprint matches the selected saved profile.
- If a fresh Gemini CLI launch still shows "Verify your account" or suggests changing login, that is usually a Google-side account validation response (`VALIDATION_REQUIRED`), not a failed file switch.
- The reference project `Besty0728/Gemini-CLI-Auth-Manager` uses the same basic pattern: overwrite `oauth_creds.json`, clear cache, restart Gemini CLI, and treat `VALIDATION_REQUIRED` as an account issue.

## Design Direction

Do not fork a large Windows-oriented project just to reuse small parts of its logic.

Instead:

1. Build a small, explicit core around the live `~/.gemini` files.
2. Keep v0.1 standard-library-only.
3. Add hook-driven auto-rotation only after the account pool primitives are solid.

## Current Scope

v0.1 must provide:

- save current live account
- list accounts
- show last known probe status per account
- switch account
- rotate to next account
- probe one or all saved accounts with a fresh Gemini subprocess
- launch a fresh login and capture it as a new profile

## Local State Files

- `auth_pool_state.json` tracks the locally selected live profile.
- `auth_check_state.json` stores the latest probe result per saved profile so later sessions can inspect recent account health without rerunning probes first.

## Deferred Scope

- automatic quota detection and rotation
- slash-command installation
- richer state inspection
- custom rotation strategies
- packaging for PyPI or Homebrew
