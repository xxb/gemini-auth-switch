# Context

## Why This Exists

One Gemini account is not enough for the intended workload. The goal is to manage several local Gemini CLI OAuth accounts and switch between them cleanly when quota is exhausted.

## Environment Assumptions

These assumptions were verified on 2026-03-17 on the development host:

- The real Gemini CLI is installed and available as `gemini`.
- The local installed version is `0.33.2`.
- The official CLI still uses `~/.gemini/settings.json`, `~/.gemini/oauth_creds.json`, `~/.gemini/google_account_id`, and `~/.gemini/google_accounts.json`.
- Hook support exists for `BeforeAgent` and `AfterAgent`.
- Custom commands still load from `~/.gemini/commands`.

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
- switch account
- rotate to next account
- launch a fresh login and capture it as a new profile

## Deferred Scope

- automatic quota detection and rotation
- slash-command installation
- richer state inspection
- custom rotation strategies
- packaging for PyPI or Homebrew

