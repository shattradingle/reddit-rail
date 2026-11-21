# Changelog

All notable changes to reddit-rail. Dates are UTC.

## 2.0.0 — 2026-11-21

The version that became a package instead of a file in someone's `~/.local/bin`.

- **MCP server.** `reddit mcp` exposes `reddit_ask`, `reddit_search` and `reddit_thread`
  over stdio, sharing the CLI's cache.
- **`reddit ask`.** Search, open the top threads, return the highest-scoring comments
  verbatim with score and permalink. Bots and promo replies are filtered out.
- **Live by default.** `reddit search` now tops the corpus up from the archive unless
  `--offline` is passed. The old opt-in `--live` flag is gone (still accepted, ignored).
- **Comment cache.** Threads are stored as JSON as well as rendered markdown, so `ask`
  and `thread` never fetch the same thread twice.
- **Hard deadlines.** `--deadline` is now a real wall: every sleep inside the HTTP layer
  is clamped to it, so a throttled archive cannot hang a command.
- **JSON everywhere.** `thread` and `sub` gained `--json`; `search` and `ask` already had it.
- **41 offline tests** across the CLI, cache, ranking, quote selection and MCP protocol.
- Packaging: `pip install .`, `pipx install git+…`, `uvx --from git+…`, console script
  `reddit` (plus `reddit-rail`). No runtime dependencies, Python 3.10+.
- CI on Linux, macOS and Windows, Python 3.10–3.13.

## 1.0.0 — 2026-11-06

First working version, for one machine.

- `search`, `thread`, `sub`, `warm`, `probe`, `status`, `subs`.
- SQLite corpus, provider health ledger, rate-limit cooldown persisted across processes.
- The four traps documented in the README were each measured, not guessed.
