# Roadmap

## v0.1

- profile storage under `~/.gemini/auth_profiles`
- `save`, `list`, `current`, `doctor`, `check`, `check-all`, `stats`, `stats-all`, `quota`, `quota-all`, `pick`, `auto-use`, `use`, `next`, `remove`, `login`
- cache clearing on switch
- incremental quota refresh for `auto-use` via the official Code Assist API
- AND/OR/exclusion model filters for `pick` and `auto-use`, so integrations can prefer a model family and ignore low-priority variants such as `lite`
- local quota cache entries tagged by source (`stats` or `api`)
- per-model `reset_at` tracking plus failed-refresh cooldowns to avoid repeated 429s
- process lock for live auth rewrites
- user-level install path documentation and helper script for a stable `gswitch` command
- repo docs that preserve requirements across sessions

## v0.2

- launcher- or wrapper-driven runtime 429 detection and one-shot retry on top of cached `pick` and incremental `auto-use`
- installable Gemini CLI hooks for observability or optional policy wiring after the wrapper path is stable
- use API-refreshed quota cache as the primary rotation signal, with `/stats` as a manual fallback
- quota-error reporting in `AfterAgent`
- optional preflight rotation in `BeforeAgent`
- configurable rotation order

## v0.3

- slash-command helpers under `~/.gemini/commands`
- JSON output mode
- dry-run support
