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
gswitch current
```

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
```

## Restart Behavior

Gemini CLI sessions typically cache auth state after startup. After switching profiles, restart any already-running Gemini CLI session before expecting the new account to take effect.

## Roadmap

See [docs/roadmap.md](docs/roadmap.md).
