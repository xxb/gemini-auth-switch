# Roadmap

## v0.1

- profile storage under `~/.gemini/auth_profiles`
- `save`, `list`, `current`, `doctor`, `check`, `check-all`, `stats`, `stats-all`, `quota`, `quota-all`, `pick`, `auto-use`, `use`, `next`, `remove`, `login`
- cache clearing on switch
- incremental quota refresh for `auto-use` via the official Code Assist API
- local quota cache entries tagged by source (`stats` or `api`)
- per-model `reset_at` tracking plus failed-refresh cooldowns to avoid repeated 429s
- process lock for live auth rewrites
- repo docs that preserve requirements across sessions

## v0.2

- installable Gemini CLI hooks for automatic rotation
- build hook decisions on top of cached `pick` and incremental `auto-use` logic
- use API-refreshed quota cache as the primary rotation signal, with `/stats` as a manual fallback
- quota-error detection in `AfterAgent`
- optional preflight rotation in `BeforeAgent`
- configurable rotation order

## v0.3

- slash-command helpers under `~/.gemini/commands`
- JSON output mode
- dry-run support
