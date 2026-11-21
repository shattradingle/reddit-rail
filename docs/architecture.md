# How reddit-rail works

One page, so you can judge the code without reading all of it.

## The constraint everything follows from

Reddit has no free, unauthenticated, server-side search. The endpoints people reach for
first do not do what they look like they do. These were measured on 2026-11-06, from a
datacenter IP, and they are the reason this tool is shaped like it is:

| endpoint | what people expect | what it does |
| --- | --- | --- |
| `www.reddit.com/search.rss` | search results | returns r/all trending junk for every query |
| `www.reddit.com/**.json` | search results | `403` without auth from a datacenter IP |
| `www.reddit.com/r/<sub>/search.rss` | search within a sub | works, but 429s after a handful of requests |
| `api.pullpush.io` | the documented fallback | 526/521, down for a while |
| `arctic-shift.photon-reddit.com` | an archive | **works**: full posts and comment bodies, no auth |

So the primary provider is the archive. It has one hard limit: a keyword query must be
scoped to a subreddit or an author. That is why `search` is a fan-out over a sub list
instead of one request.

## The flow

```
argv ──► argparse ──► cmd_* ──► collect_search ──► providers ──► sqlite ──► rank ──► output
                          │                                          ▲
                          └──► render_thread ◄── arctic comments ───┘
```

- `src/reddit_rail/rail.py` is the whole tool: config, HTTP, providers, cache, ranking,
  commands, CLI. It is sectioned with `# ----` headers.
- `src/reddit_rail/mcp.py` is the stdio MCP server. It imports the command functions and
  captures their stdout, so the MCP output and the CLI output can never drift apart.
- `tests/` is offline. Provider seams are monkeypatched; parsing is fed fixtures.

## Three rules the code enforces

1. **Nothing is fetched twice while it is fresh.** Every post and thread lands in SQLite.
   `cached_*` functions take a freshness window; `search` answers from the corpus first
   and only then tops up. A repeat question costs no network.
2. **The archive's window is respected, not fought.** Requests are paced
   (`MIN_INTERVAL`), the reset header is persisted to `reddit_cooldown.json` so a second
   process waits instead of burning the window, 422/429 backs off past the lockout, and
   every command carries a request budget and a hard deadline.
3. **Missing data is labelled, not invented.** RSS rows have no score or date, so they
   print `date n/a` and `?` rather than `1970-01-01` and `0`. A skipped thread is listed
   in one `_skipped:_` line. A degraded multi-word query says so.

## Where the state lives

| path | what |
| --- | --- |
| `$XDG_STATE_HOME/reddit-rail/reddit_cache/cache.db` | posts, threads, comments, runs, subs seen |
| `$XDG_STATE_HOME/reddit-rail/reddit_status.json` | the last run, as recorded by `reddit status` |
| `$XDG_STATE_HOME/reddit-rail/reddit_subs.json` | the per-machine fan-out list |
| `$XDG_STATE_HOME/reddit-rail/reddit_cooldown.json` | provider reset times |

All four are overridable by environment variable. Nothing is written outside state dirs.

## What would make this fail

- The archive goes away. The fallback is Reddit's RSS, which does not really search, so
  the tool degrades to "recent posts in a sub" and says so.
- Reddit changes the archive's API shape. `arctic_posts` / `arctic_comments` are the only
  two functions that know the schema; everything else takes dicts.
- The sub list is wrong for the question. Recall is the list. That is a user-editable
  file, not a model.
