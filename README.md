# gemini-auth-switch

[![MIT License](https://img.shields.io/github/license/xxb/gemini-auth-switch)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](pyproject.toml)
[![GitHub tag](https://img.shields.io/github/v/tag/xxb/gemini-auth-switch?label=version)](https://github.com/xxb/gemini-auth-switch/releases)

Small, Linux-first account pooling for the official Gemini CLI.

`gemini-auth-switch` keeps multiple OAuth profiles under `~/.gemini/auth_profiles/`, lets you switch between them quickly, and prepares the ground for automatic account rotation when one Gemini account runs out of quota.

## Goals

- Save multiple Gemini CLI OAuth accounts on one machine
- Switch accounts without manual file juggling
- Keep the implementation compatible with the real `~/.gemini` layout
- Stay simple enough to audit and open source

## Non-Goals For v0.1

- Full TUI or menu system
- Windows-first install flow
- Heavy dependencies
- Deep integration with third-party auth manager projects

## Features In This Version

- Save the current live Gemini auth into a named profile
- List saved profiles
- Show the current live profile match
- Show auth diagnostics and common failure hints
- Probe all saved profiles and classify which ones still work
- Persist the latest probe result for each profile and show it in `gswitch list`
- Collect Gemini `/stats` output for one or all saved profiles
- Persist the latest quota snapshot per profile and inspect it locally
- Pick the best saved profile for matching models from cached quota data
- Automatically keep or switch profiles with on-demand quota refresh
- Switch to a saved profile
- Rotate to the next saved profile
- Remove a saved profile
- Launch a fresh Gemini login flow and capture the resulting account
- Clear both known Gemini OAuth cache file variants when switching

## Installation

```bash
./scripts/install-user.sh
```

The helper creates a dedicated user-level virtual environment under an OS-appropriate data directory, such as `~/.local/share/gemini-auth-switch/venv` on Linux or `~/Library/Application Support/gemini-auth-switch/venv` on macOS. It then links the `gswitch` launcher into the current Python user scripts directory.

This avoids distro-managed `pip --user` issues on Linux hosts that enforce PEP 668, while still giving launchers a portable path. For launcher or wrapper integration, point at the installed command from `command -v gswitch` or from the path printed by the installer. Do not hardcode a repository-local virtualenv path, another user's home path, or a Linux-only launcher path assumption.

`gswitch` discovers the Gemini Code Assist OAuth client constants from the installed official `gemini` CLI package on the local machine. If your local package layout differs, you can override discovery with `GSWITCH_OAUTH_CLIENT_ID` and `GSWITCH_OAUTH_CLIENT_SECRET`.

## Usage

Save the currently logged-in account:

```bash
gswitch save
gswitch save work@example.com
gswitch save trading-burner
```

Inspect profiles:

```bash
gswitch list
gswitch list --verbose
gswitch current
gswitch doctor
gswitch check work@example.com
gswitch check-all
gswitch check-all --delay 15
gswitch stats work@example.com
gswitch stats-all --delay 15
gswitch quota
gswitch quota-all
gswitch quota-all --verbose
gswitch pick --match 3.1-pro
gswitch pick --match 3.1 --match pro
gswitch pick --match-any gemini-3 --exclude-match lite
gswitch auto-use --match 3.1-pro
gswitch auto-use --match-any gemini-3 --exclude-match lite
gswitch auto-use --match 3.1-pro --min-remaining 15
gswitch auto-use --match 3.1-pro --stale-seconds 300 --candidate-refresh-limit 2
```

`gswitch check` and `gswitch check-all` both save the latest probe result. By default `gswitch list` stays compact and shows the last known status and timestamp; use `gswitch list --verbose` when you also want saved-at time and short probe detail.

`gswitch stats` and `gswitch stats-all` launch a fresh Gemini TTY session and run `/stats`, so you can compare remaining quota across saved accounts without reopening browser login. Each run also refreshes the local quota cache in `auth_quota_state.json`.

`gswitch quota` and `gswitch quota-all` are read-only local views over the last saved quota snapshot, so they return immediately and do not start Gemini again.

`gswitch pick` is also read-only. It looks at cached quota plus the last known health status from `check`, filters models with repeated `--match` keywords, optional `--match-any` OR keywords, and optional `--exclude-match` exclusions, then prints the best current candidate without switching accounts for you.

`gswitch auto-use` starts from the same local cache, but it is no longer cache-only. It refreshes the current profile when its quota snapshot is missing, stale, unmatched, or still only `/stats`-derived, then keeps the current account if the refreshed matched quota is still at or above `--min-remaining`.

If the current account is still not good enough, `gswitch auto-use` refreshes up to `--candidate-refresh-limit` stale or missing alternative profiles through the official Gemini Code Assist quota API, updates `auth_quota_state.json`, and switches to the best eligible candidate. `--stale-seconds` controls when an API quota snapshot is considered old enough to refresh again.

When a refresh attempt fails, `auto-use` now records a short local cooldown for that profile instead of hammering the same candidate again on the next run. For successful API snapshots it also stores each model's absolute `reset_at`, so a fully exhausted matched model can be skipped until its quota window resets.

`gswitch` itself does not depend on `.cc-connect` or any other launcher. For runtime rotation, the intended integration pattern is: run `gswitch auto-use` before a non-interactive `gemini -p --output-format stream-json` request, and if the very first streamed event is still a 429-style result error, mark that profile as rate-limited, exclude it from the next decision with `gswitch auto-use --avoid-profile ...`, switch once, and retry the request exactly one time. Any local wrapper or launcher can implement that pattern.

Switch accounts:

```bash
gswitch use work@example.com
gswitch next
```

Login a brand new account and save it:

```bash
gswitch login burner-02
```

By default this launches `gemini` and captures the live auth files after the command exits.

## Storage Layout

The tool writes only under `~/.gemini`:

```text
~/.gemini/
  auth_profiles/
    work@example.com/
      oauth_creds.json
      google_account_id
      profile.json
  auth_pool_state.json
  auth_check_state.json
  auth_quota_state.json
  auth_switch.lock
```

`auth_quota_state.json` may contain either legacy `/stats` snapshots or newer Code Assist API snapshots. It also stores refresh cooldown metadata plus per-model `reset_at` when the upstream API provides it. `pick` only reads that file; `auto-use` now refreshes it incrementally when needed.

`auth_switch.lock` is a local process lock that serializes commands which temporarily rewrite live Gemini auth files.

## Example Output

Compact list view:

```text
  primary@example.com   email=primary@example.com   status=ok                  checked=2026-03-17T05:31:38+00:00
* research@example.com  email=research@example.com  status=validation_required checked=2026-03-17T05:33:02+00:00
  backup@example.com    email=backup@example.com    status=timeout             checked=2026-03-17T05:34:11+00:00
```

Verbose list view:

```text
* research@example.com  email=research@example.com  status=validation_required checked=2026-03-17T05:33:02+00:00 updated=2026-03-17T03:44:22+00:00 detail=Verify your account to continue.
```

Stats view:

```text
ok profile=research@example.com email=research@example.com checked=2026-03-17T05:40:43+00:00 usage=Auto (Gemini 3) detail=models=3 lowest_remaining=93.3%
  model=gemini-2.5-flash requests=- remaining=96.4% resets in 22h 25m
  model=gemini-2.5-flash-lite requests=- remaining=98.3% resets in 22h 25m
  model=gemini-2.5-pro requests=- remaining=93.3% resets in 22h 24m
```

Pick view:

```text
  picked profile=research@example.com email=research@example.com model=gemini-3.1-pro-preview remaining=97.0% resets in 11h quota_checked=2026-03-17T10:05:00+00:00 health=ok usage=Auto (Gemini 3)
filters match=3.1-pro match_any=- exclude=-
```

Auto-use view:

```text
switched from=primary@example.com to=research@example.com email=research@example.com model=gemini-3.1-pro-preview remaining=97.0% resets in 11h threshold=15.0% quota_checked=2026-03-17T10:05:00+00:00 health=ok usage=Auto (Gemini 3) reason=switched after refreshing the current profile and the top cached candidates because the current profile was missing, unhealthy, unmatched, or below threshold 15.0%
filters match=3.1-pro match_any=- exclude=-
restart any running Gemini CLI session to apply the new account
```

## Restart Behavior

Gemini CLI sessions typically cache auth state after startup. After switching profiles, restart any already-running Gemini CLI session before expecting the new account to take effect.

`gemini-auth-switch` only rewrites Gemini auth files and clears Gemini OAuth token caches. It does not delete unrelated Gemini host files such as `projects.json`, `state.json`, `trustedFolders.json`, or command definitions. That means old saved workspace/session metadata is not wiped by `gswitch` itself.

The practical limitation is process state: if you switch accounts and then restart Gemini to load the new auth, the already-open in-process conversation context is interrupted because the old Gemini process is gone. In short: saved host metadata stays, but a live running chat session is not seamlessly carried across an auth switch.

## Troubleshooting

If `gswitch use ...` changes the live profile but a fresh `gemini` launch still says "Verify your account" or offers "change login", that is usually not a local switch failure. The official Gemini CLI can return `VALIDATION_REQUIRED` for a specific Google account, and the reference `Gemini-CLI-Auth-Manager` project documents the same behavior.

Use `gswitch doctor` to confirm the active profile, auth type, and cache-file state. This project manages local OAuth files for `oauth-personal`; it does not bypass Google-side account eligibility or verification checks.

`gswitch check-all` does not reopen browser login for every account. It reuses the saved local credentials, starts a fresh `gemini -p` subprocess per profile, streams progress as each profile finishes, prints a final results summary, stores the latest result in `auth_check_state.json`, and restores the original live auth after the probe run. That is still a burst of real authenticated Gemini requests, so use a non-zero `--delay` if you want to reduce rapid multi-account probing.

If you only want to verify one account, use `gswitch check <profile>`. It performs the same kind of fresh-process probe as `check-all`, but only for the selected saved profile.

`gswitch stats` uses Gemini's interactive `/stats` command under a fresh TTY session. In practice that means quota inspection and prompt execution are related but not identical checks. An account may still return quota information even if a normal prompt run later hits validation or model-level limits, so treat `stats` as quota visibility, not a full end-to-end health check.

Use `gswitch quota` or `gswitch quota-all` when you only need the last recorded local snapshot. That is the right base for incremental refresh and auto-switch logic because it avoids re-polling every saved account on every decision.

If `quota` shows `refresh=rate_limited blocked_until=...`, `auto-use` will skip refreshing that profile again until the cooldown expires. If a matched model row shows `reset_at=...` and its remaining quota is already `0.0%`, that profile is also skipped until the earliest relevant reset time.

Use `gswitch pick --match ...` when you want a fast local answer to "which saved profile currently looks best for this model family?" without starting Gemini again.

Use `gswitch pick --match-any ... --exclude-match ...` when you want a broader preferred family such as "Gemini 3, but never lite".

Use `gswitch auto-use --match ...` before launching a new Gemini CLI session when you want the tool to refresh only what it needs, keep the current account if it still looks healthy enough, and otherwise switch to the best refreshed alternative.

## Roadmap

See [docs/roadmap.md](docs/roadmap.md).
