# gemini-auth-switch

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
- Switch to a saved profile
- Rotate to the next saved profile
- Remove a saved profile
- Launch a fresh Gemini login flow and capture the resulting account
- Clear both known Gemini OAuth cache file variants when switching

## Installation

```bash
python3 -m pip install -e .
```

This installs the `gswitch` command.

## Usage

Save the currently logged-in account:

```bash
gswitch save
gswitch save work@gmail.com
gswitch save trading-burner
```

Inspect profiles:

```bash
gswitch list
gswitch list --verbose
gswitch current
gswitch doctor
gswitch check work@gmail.com
gswitch check-all
gswitch check-all --delay 15
```

`gswitch check` and `gswitch check-all` both save the latest probe result. By default `gswitch list` stays compact and shows the last known status and timestamp; use `gswitch list --verbose` when you also want saved-at time and short probe detail.

Switch accounts:

```bash
gswitch use work@gmail.com
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
    work@gmail.com/
      oauth_creds.json
      google_account_id
      profile.json
  auth_pool_state.json
  auth_check_state.json
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

## Roadmap

See [docs/roadmap.md](docs/roadmap.md).
