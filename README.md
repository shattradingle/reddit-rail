# reddit-rail

Search Reddit and read whole threads from the command line. No login, no API key, no
account, no per-request billing.

```console
$ reddit search "gap year engineering" --subs 6thForm,UniUK --limit 20
$ reddit thread 1e5p300
$ reddit sub 6thForm --limit 25
```

## Why this exists

Scraping Reddit without credentials is full of traps, and every "Reddit search" attempt
written from scratch walks into the same four:

1. `www.reddit.com/search.rss` **does not search**. Whatever you ask it, it returns
   r/all trending junk. This is the single most common cause of a tool that "works" and
   quietly returns cats when you asked about gap years.
2. `www.reddit.com/**.json` returns **403** from any datacenter IP without auth.
3. `search.rss` **429s** after a handful of requests, and bursts get you punished harder.
4. The documented fallbacks (`api.pullpush.io` and friends) have been **down** for a while.

## What actually works

`arctic-shift.photon-reddit.com` — a public Reddit archive, no auth, full post objects and
full comment bodies. Its one limit: a keyword query must be scoped to a subreddit or an
author. There is no global search.

So `search` is a fan-out over a list of subreddits, which is what research wants anyway:
r/6thForm answers a university question badly and r/UniUK answers it well. Naming a sub you
have never used is enough — it is registered permanently on first use.

```
arctic-shift ──posts/search?subreddit=&query=──┐
              ──posts/ids──────────────────────┤──► sqlite cache ──► ranked output
              ──comments/search────────────────┘         ▲
                                                        │
                reddit rss (fallback, only when arctic fails)
```

## Install

Python 3.9+, standard library only — there is nothing to `pip install`.

```console
git clone https://github.com/shattradingle/reddit-rail
cd reddit-rail
install -m755 reddit ~/.local/bin/reddit
```

## Commands

| command | what it does |
| --- | --- |
| `reddit search "q"` | keyword search fanned out over the curated sub list |
| `reddit thread <url\|id>` | one thread: post plus every comment, nested with scores |
| `reddit sub <name>` | the newest posts in a single sub |
| `reddit warm` | fetch the stalest subs into the local corpus |
| `reddit probe` | health-check every provider and write the status artifact |
| `reddit status` | the last run, as recorded |
| `reddit subs` | show, add to, or reset the fan-out list |

Useful flags:

```
--subs a,b,c        override the sub list; unknown subs are added to it
--window year       hour|day|week|month|year|all
--limit 40          results
--live              fetch fresh posts instead of answering from cache
--json / --out f    machine-readable output
```

## The cache is the point

Every fetch lands in sqlite. A repeat question costs **~0.07s and zero network**, so you can
ask the same thing twenty times while narrowing a question, and an outage cannot lose
anything already fetched. `reddit warm` on a timer keeps the corpus fresh; `--live` is for
when you want right now.

## Limits, stated plainly

- **Arctic Shift is rate limited to roughly 4 requests per window**, with a lockout of about
  75s if you push it. `search` therefore caps itself (`--budget`, default 24) and paces
  calls; `warm` defaults to 4 and expects to be run on a timer.
- Multi-word queries sometimes 422 with `"Timeout. Maybe slow down a bit"`. The tool
  degrades to the longest token and says so in one line (`searched '<token>' instead`) —
  that line is normal, not a failure.
- It is an unofficial archive. Posts deleted on Reddit may still be readable there;
  moderator-removed threads stay visible in the archive and are often the most honest
  evidence in a thread.
- One subreddit per query means recall depends on your sub list. The shipped list is
  opinionated; replace it.

## Exit codes

`0` ok · `1` nothing found or partial · `2` every provider dead (see `reddit probe`)

## Licence

MIT. See `LICENSE`.

## Tests

Offline, no network, no credentials:

```console
python -m unittest -v     # 13 cases
```
