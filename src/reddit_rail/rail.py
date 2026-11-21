#!/usr/bin/env python3
"""reddit.py - search Reddit and read whole threads. No login, no API key.

WHY THIS EXISTS (2026-11-06)
Every "research Reddit" session used to fail, and for four separate reasons:
  1. www.reddit.com/search.rss (the old reddit_search.py channel) DOES NOT SEARCH.
     It returns r/all trending junk whatever you ask it - verified 2026-11-06.
     That is why old sessions "found" cats and memes when asked about gap years.
  2. www.reddit.com/**.json returns 403 (datacenter IP, no auth).
  3. reddit.com/search.rss 429s after a handful of requests; bursts are punished.
  4. api.pullpush.io (the documented fallback) was returning 526/521 - down.

WHAT WORKS (measured, 2026-11-06)
  arctic-shift.photon-reddit.com - a public Reddit archive. No auth, no key.
  Full post objects and full comment bodies. Its one limit: a keyword query
  MUST be scoped to a subreddit (or an author). There is no global search.
  So discovery here is a fan-out over a curated sub list, which is what
  research actually wants anyway: r/6thForm answers a UniUK question badly
  and r/UniUK answers it well.

DESIGN
  primary  : arctic-shift  posts/search?subreddit=&query=   + posts/ids + comments/search
  fallback : reddit rss    /r/<sub>/search.rss?restrict_sr=1  (only when arctic fails)
  cache    : sqlite - a repeat question costs nothing, and an outage cannot
             lose work that was already fetched.
             default ~/.local/state/reddit-rail/cache.db  ($REDDIT_CACHE_DIR)
  status   : last run, provider, code, reason, timestamp. `reddit probe` reads it.
             default ~/.local/state/reddit-rail/reddit_status.json

USAGE
  reddit search "gap year engineering"              # fan out over the curated subs
  reddit search "bose anc" --subs headphones,bose   # narrow it
  reddit search "x" --window year --limit 40 --json
  reddit thread 1e5p300                             # a whole thread, comments included
  reddit thread https://www.reddit.com/r/6thForm/comments/1e5p300/x/
  reddit sub 6thForm --limit 25                     # newest 25 in a sub
  reddit probe                                      # is the rail alive? writes status
  reddit status                                     # last probe/run as recorded
  reddit subs [--add r/x] [--reset]                 # the curated list

EXIT CODES
  0 ok · 1 nothing found or partial · 2 every provider dead (see `reddit probe`)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

__version__ = "2.0.0"

# ---------------------------------------------------------------- config

HOME = Path.home()
STATE_ROOT = Path(os.environ.get("XDG_STATE_HOME", HOME / ".local" / "state"))
STATE_DIR = Path(os.environ.get("REDDIT_STATE_DIR", STATE_ROOT / "reddit-rail"))
CACHE_DIR = Path(os.environ.get("REDDIT_CACHE_DIR", STATE_DIR / "reddit_cache"))
STATUS_PATH = Path(os.environ.get("REDDIT_STATUS_PATH", STATE_DIR / "reddit_status.json"))
SUBS_PATH = Path(os.environ.get("REDDIT_SUBS_PATH", STATE_DIR / "reddit_subs.json"))
COOLDOWN_PATH = STATE_DIR / "reddit_cooldown.json"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/135.0.0.0 Safari/537.36")
ARCTIC = "https://arctic-shift.photon-reddit.com/api"
RSS = "https://www.reddit.com/r/{sub}/search.rss"
PROBE_POST_ID = "1e5p300"          # a known-good archived post

# Priority tier: the first pass. Keep this SHORT - every sub is one rate-limited
# request, and arctic-shift throttles in a rolling window.
PRIORITY_SUBS = [
    "6thForm", "UniUK", "EngineeringStudents", "AskUK", "UKPersonalFinance",
    "cscareerquestionsEU", "programming", "LocalLLaMA", "smallbusiness", "london",
]

# Full fan-out list (--all-subs): the subs his research questions actually live in.
DEFAULT_SUBS = [
    # study / universities / careers
    "6thForm", "UniUK", "EngineeringStudents", "AskUK", "AskAcademia",
    "apprenticeships", "ukjobs", "gradadmissions",
    # money / business
    "UKPersonalFinance", "ukfinance", "smallbusiness", "Entrepreneur",
    "SideProject", "SaaS", "beermoneyuk", "freelance",
    # software / AI / hardware
    "programming", "cscareerquestions", "cscareerquestionsEU", "MachineLearning",
    "LocalLLaMA", "selfhosted", "androiddev", "raspberry_pi", "esp32", "Arduino",
    "robotics",
    # consumer / local
    "headphones", "bose", "london",
]

ATOM = "{http://www.w3.org/2005/Atom}"

# ---------------------------------------------------------------- small utils


def now_utc() -> int:
    return int(time.time())


def iso(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return "?"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def url_of(r) -> str:
    p = r.get("permalink") or ""
    return p if str(p).startswith("http") else "https://www.reddit.com" + str(p)


def tokens(q: str):
    return [t for t in re.split(r"[^a-z0-9]+", q.lower()) if len(t) > 2]


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ---------------------------------------------------------------- http

class Health:
    """Per-provider outcome ledger for this run; written out as the status artifact."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = {}

    def record(self, provider: str, ok: bool, detail: str, code=0):
        with self.lock:
            d = self.data.setdefault(provider, {"ok": 0, "fail": 0, "last": ""})
            d["ok" if ok else "fail"] += 1
            d["last"] = f"{code} {detail}".strip()
            d["code"] = code
            d["at"] = now_utc()

    def summary(self) -> dict:
        return {k: dict(v) for k, v in self.data.items()}


HEALTH = Health()
DEADLINE = [0.0]          # monotonic timestamp; 0 == no deadline
_PACE_LOCK = threading.Lock()
_LAST_CALL = [0.0]
MIN_INTERVAL = float(os.environ.get("REDDIT_MIN_INTERVAL", "0.45"))


def set_deadline(seconds) -> None:
    """Set a hard wall for this command. Every sleep inside a fetch respects it."""
    DEADLINE[0] = (time.monotonic() + float(seconds)) if seconds else 0.0


def _left():
    return None if not DEADLINE[0] else DEADLINE[0] - time.monotonic()


def _sleep(seconds: float) -> None:
    left = _left()
    if left is not None:
        if left <= 0:
            raise FetchError(0, "deadline reached")
        seconds = min(seconds, left)
    if seconds > 0:
        time.sleep(seconds)


def _pace():
    with _PACE_LOCK:
        wait = MIN_INTERVAL - (time.time() - _LAST_CALL[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[0] = time.time()


def _note_reset(provider: str, headers, default: int = 0) -> int:
    """arctic-shift sends x-ratelimit-reset (seconds) on 200 and 429. Persist it so a
    later process - another session, a cron job - waits instead of burning the window."""
    secs = default
    try:
        if headers and headers.get("x-ratelimit-reset"):
            secs = int(float(headers.get("x-ratelimit-reset")))
    except Exception:
        secs = default
    if secs and secs > 0:
        cd = load_json(COOLDOWN_PATH, {})
        until = now_utc() + secs
        if until > int(cd.get(provider, 0)):
            cd[provider] = until
            try:
                _atomic_write(COOLDOWN_PATH, json.dumps(cd))
            except Exception:
                pass
    return secs


def _wait_global_cooldown(provider: str) -> None:
    cd = load_json(COOLDOWN_PATH, {})
    until = int(cd.get(provider, 0))
    left = until - now_utc()
    if left > 0:
        _sleep(min(left, 45))


class FetchError(Exception):
    def __init__(self, code: int, body: str):
        self.code = code
        self.body = body
        super().__init__(f"HTTP {code}: {body[:160]}")


def get(url: str, provider: str, tries: int = 3, timeout: int = 25,
        headers: dict | None = None) -> str:
    """Paced GET with backoff. 429s are backed off hard and recorded."""
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    last = FetchError(0, "no attempt")
    for attempt in range(tries):
        _pace()
        req = urllib.request.Request(url, headers=hdrs)
        _wait_global_cooldown(provider)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read().decode("utf-8", "replace")
                _note_reset(provider, r.headers)
                HEALTH.record(provider, True, "ok", r.status)
                return body
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            last = FetchError(e.code, body)
            HEALTH.record(provider, False, body[:120] or e.reason, e.code)
            if e.code == 429:
                reset = _note_reset(provider, e.headers, default=20)
                _sleep(min(reset + 0.5, 45))
            elif e.code in (500, 502, 503, 504, 520, 521, 522, 526, 422):
                # arctic-shift uses 422 for "Timeout. Maybe slow down a bit", and its
                # real lockout is ~75s. A 1.5s retry burns every attempt inside that
                # window and reports 0ok/3fail (measured 2026-11-08), so escalate past
                # the lockout instead of hammering it.
                if provider == "arctic-shift" and e.code == 422:
                    _sleep(min(20 * (attempt + 1), 75))
                else:
                    _sleep(1.5 * (attempt + 1))
            elif e.code in (400, 403, 404):
                raise last
            else:
                _sleep(1.0)
        except Exception as e:  # timeout, DNS, connection reset
            last = FetchError(0, str(e))
            HEALTH.record(provider, False, str(e)[:120], 0)
            _sleep(1.5 * (attempt + 1))
    raise last


# ---------------------------------------------------------------- providers

def arctic_posts(sub: str, query: str = "", limit: int = 100, sort: str = "desc",
                 tries: int = 3):
    """Search one subreddit. Multi-word queries are the expensive case server-side.

    Measured 2026-11-08: `query=unredeemable` answers in ~1s, while
    `query=unredeemable hero` returns `{"error":"Timeout. Maybe slow down a bit"}`
    regardless of how long you wait - so a multi-word query is retried as its most
    distinctive single token before giving up. That keeps live search working
    without paying 75s three times for a query the server will never answer.
    """
    variants = [query] if not query else [query, max(query.split(), key=len)]
    err = "no attempt"
    for q in variants:
        params = {"subreddit": sub, "limit": min(limit, 100), "sort": sort}
        if q:
            params["query"] = q
        url = f"{ARCTIC}/posts/search?{urllib.parse.urlencode(params)}"
        for attempt in range(tries):
            try:
                d = json.loads(get(url, "arctic-shift"))
            except FetchError as e:
                err = f"HTTP {getattr(e, 'code', 0)}"
                break
            err = str(d.get("error") or "")
            if not err:
                if q != query:
                    log(f"reddit: '{query}' timed out; searched '{q}' instead")
                return d.get("data") or []
            if "imeout" not in err:
                break
            time.sleep(1.0 * (attempt + 1))
    raise FetchError(422, err)


def arctic_post(post_id: str):
    body = get(f"{ARCTIC}/posts/ids?ids={urllib.parse.quote(post_id)}", "arctic-shift")
    d = json.loads(body)
    rows = d.get("data") or []
    return rows[0] if rows else None


def arctic_comments(link_id: str, max_pages: int = 10, per_page: int = 100):
    """All comments for a post. arctic-shift caps limit at 100, so page on `before`."""
    out, seen, before = [], set(), None
    for _ in range(max_pages):
        url = f"{ARCTIC}/comments/search?link_id={urllib.parse.quote(link_id)}&limit={per_page}"
        if before:
            url += f"&before={before}"
        try:
            d = json.loads(get(url, "arctic-shift"))
        except FetchError:
            break
        if d.get("error"):
            break
        rows = d.get("data") or []
        fresh = [c for c in rows if c.get("id") not in seen]
        for c in fresh:
            seen.add(c.get("id"))
        out.extend(fresh)
        if len(rows) < per_page or not fresh:
            break
        before = min(int(c.get("created_utc", 0)) for c in rows)
        if before <= 0:
            break
    return out


def rss_search(sub: str, query: str, window: str = "all", limit: int = 100):
    """Fallback only: reddit's own RSS. 429-prone, so it is second class by design."""
    cd = load_json(COOLDOWN_PATH, {})
    if cd.get("rss", 0) > now_utc():
        raise FetchError(429, f"rss cooling down for {cd['rss'] - now_utc()}s")
    url = (RSS.format(sub=urllib.parse.quote(sub)) + "?q=" + urllib.parse.quote(query)
           + f"&restrict_sr=1&sort=relevance&t={window}&limit={min(limit, 100)}")
    try:
        body = get(url, "reddit-rss", tries=2, headers={"Accept": "application/rss+xml"})
    except FetchError as e:
        if e.code == 429:
            _atomic_write(COOLDOWN_PATH, json.dumps({"rss": now_utc() + 900}))
        raise
    root = ET.fromstring(body)
    out = []
    for e in root.findall(f"{ATOM}entry"):
        link = e.find(f"{ATOM}link")
        href = link.get("href") if link is not None else ""
        m = re.search(r"/comments/([a-z0-9]+)/", href)
        if not m:
            continue
        out.append({
            "id": m.group(1),
            "subreddit": sub,
            "title": (e.findtext(f"{ATOM}title") or "").strip(),
            "permalink": href,
            "created_utc": 0,
            "score": 0,
            "num_comments": 0,
            "selftext": "",
            "_via": "rss",
        })
    return out


# ---------------------------------------------------------------- cache

SCHEMA = """
create table if not exists posts(
  id text primary key, sub text, title text, score integer, num_comments integer,
  created_utc integer, permalink text, selftext text, fetched_at integer);
create table if not exists threads(
  id text primary key, md text, fetched_at integer);
create index if not exists posts_query on posts(sub, created_utc);
create table if not exists comments(post_id text primary key, json text, fetched_at integer);
create table if not exists runs(key text primary key, at integer);
create table if not exists subs_seen(sub text primary key, at integer);
"""


def _db():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(CACHE_DIR / "cache.db", timeout=20)
    con.executescript(SCHEMA)
    return con


def cache_posts(rows):
    if not rows:
        return
    con = _db()
    with con:
        con.executemany(
            "insert or replace into posts values(?,?,?,?,?,?,?,?,?)",
            [(r.get("id"), r.get("subreddit") or r.get("sub"), r.get("title"),
              int(r.get("score") or 0), int(r.get("num_comments") or 0),
              int(r.get("created_utc") or 0),
              (r.get("permalink") or ""),
              (r.get("selftext") or "")[:20000], now_utc()) for r in rows])
    con.close()


def mark_run(sub, query):
    con = _db()
    with con:
        con.execute("insert or replace into runs values(?,?)",
                    (f"{sub}|{query.lower()}", now_utc()))
    con.close()


def cached_query(sub, query, fresh_h):
    """Return cached posts for (sub, query) if that pair was fetched recently, else None."""
    try:
        con = _db()
        row = con.execute("select at from runs where key=?",
                          (f"{sub}|{query.lower()}",)).fetchone()
        if not row or now_utc() - row[0] > fresh_h * 3600:
            con.close()
            return None
        toks = tokens(query)
        rows = con.execute(
            "select id, sub, title, score, num_comments, created_utc, permalink, selftext "
            "from posts where sub=?", (sub,)).fetchall()
        con.close()
    except Exception:
        return None
    out = []
    for r in rows:
        blob = f"{r[2]} {r[7]}".lower()
        if any(t in blob for t in toks):
            out.append({"id": r[0], "subreddit": r[1], "title": r[2], "score": r[3],
                        "num_comments": r[4], "created_utc": r[5],
                        "permalink": r[6], "selftext": r[7], "_via": "cache"})
    return out


def mark_seen(sub):
    con = _db()
    with con:
        con.execute("insert or replace into subs_seen values(?,?)", (sub, now_utc()))
    con.close()


def seen_age_h(sub):
    try:
        con = _db()
        row = con.execute("select at from subs_seen where sub=?", (sub,)).fetchone()
        con.close()
        return None if not row else (now_utc() - row[0]) / 3600.0
    except Exception:
        return None


def cached_posts_for(sub, query, window=None):
    """Keyword filter over whatever is already in the cache for this sub. No network."""
    try:
        con = _db()
        rows = con.execute(
            "select id, sub, title, score, num_comments, created_utc, permalink, selftext "
            "from posts where sub=?", (sub,)).fetchall()
        con.close()
    except Exception:
        return []
    toks = tokens(query)
    out = []
    for r in rows:
        blob = f"{r[2]} {r[7]}".lower()
        if toks and not any(t in blob for t in toks):
            continue
        out.append({"id": r[0], "subreddit": r[1], "title": r[2], "score": r[3],
                    "num_comments": r[4], "created_utc": r[5],
                    "permalink": r[6], "selftext": r[7], "_via": "cache"})
    return out


def prune(post_days: int = 180, thread_days: int = 21, max_posts: int = 200_000):
    """Keep the cache bounded: the warmer writes 100 rows per sub every 10 minutes."""
    con = _db()
    removed = 0
    with con:
        removed += con.execute("delete from posts where created_utc < ?",
                               (now_utc() - post_days * 86400,)).rowcount
        removed += con.execute("delete from threads where fetched_at < ?",
                               (now_utc() - thread_days * 86400,)).rowcount
        n = con.execute("select count(*) from posts").fetchone()[0]
        if n > max_posts:
            removed += con.execute(
                "delete from posts where id in (select id from posts order by fetched_at limit ?)",
                (n - max_posts,)).rowcount
    con.close()
    if removed:
        v = _db()
        v.execute("vacuum")
        v.close()
    return removed


def cache_thread(pid, md):
    con = _db()
    with con:
        con.execute("insert or replace into threads values(?,?,?)", (pid, md, now_utc()))
    con.close()


def cached_thread(pid, max_age_h=72):
    try:
        con = _db()
        row = con.execute("select md, fetched_at from threads where id=?", (pid,)).fetchone()
        con.close()
        if row and now_utc() - row[1] < max_age_h * 3600:
            return row[0]
    except Exception:
        pass
    return None


def cache_comments(pid, comments):
    """Raw comments as JSON so `ask` and `thread` never re-fetch the same thread."""
    con = _db()
    with con:
        con.execute("insert or replace into comments values(?,?,?)",
                    (pid, json.dumps(comments), now_utc()))
    con.close()


def cached_comments(pid, max_age_h=72):
    try:
        con = _db()
        row = con.execute("select json, fetched_at from comments where post_id=?",
                          (pid,)).fetchone()
        con.close()
        if row and now_utc() - row[1] < max_age_h * 3600:
            return json.loads(row[0])
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- ranking

def rank(rows, query, window=None, limit=40):
    toks = tokens(query)
    cutoff = 0
    if window and window != "all":
        span = {"hour": 3600, "day": 86400, "week": 604800,
                "month": 2592000, "year": 31536000}.get(window, 0)
        cutoff = now_utc() - span
    scored, seen = [], set()
    for r in rows:
        pid = r.get("id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        created = int(r.get("created_utc") or 0)
        if cutoff and created and created < cutoff:
            continue
        title = (r.get("title") or "").lower()
        body = (r.get("selftext") or "").lower()
        hits = 0
        for t in toks:
            hits += 3 * title.count(t) + body.count(t)
        if toks and hits == 0:
            continue
        s = (2.0 * hits
             + 1.5 * math.log10(1 + int(r.get("num_comments") or 0))
             + 1.0 * math.log10(1 + max(0, int(r.get("score") or 0))))
        scored.append((s, r))
    scored.sort(key=lambda x: (-x[0], -int(x[1].get("created_utc") or 0)))
    return [r for _, r in scored[:limit]]


# ---------------------------------------------------------------- commands

def normalise_sub(raw: str) -> str:
    """Accept 6thForm, r/6thForm, reddit.com/r/6thForm - return just the name."""
    x = raw.strip().rstrip("/")
    m = re.search(r"/r/([A-Za-z0-9_]+)", x)
    if m:
        return m.group(1)
    return x[2:] if x.lower().startswith("r/") else x


def register_subs(raw_subs):
    """Add unknown subs to the persisted list on first use, so a session never has to
    go and read a doc before searching somewhere new.

    Every documented form accepts comma lists ('--subs 6thForm,newsub'), so split
    them here - otherwise the whole string is registered as one bogus sub name.
    """
    expanded = []
    for x in raw_subs:
        expanded.extend(str(x).split(","))
    subs = [normalise_sub(x) for x in expanded if x.strip()]
    known = default_subs()
    added = [s for s in subs if s not in known]
    if added:
        _atomic_write(SUBS_PATH, json.dumps(known + added, indent=1))
        log(f"reddit: added to the fan-out list: {', '.join('r/' + a for a in added)}")
    return subs


def default_subs(priority=False):
    if priority:
        full = default_subs()
        # a bare search covers the priority tier PLUS anything a session has added
        added = [s for s in full if s not in DEFAULT_SUBS]
        return [s for s in full if s in PRIORITY_SUBS] + added or list(PRIORITY_SUBS)
    if SUBS_PATH.exists():
        subs = load_json(SUBS_PATH, None)
        if isinstance(subs, list) and subs:
            return [s if not s.startswith("r/") else s[2:] for s in subs]
    _atomic_write(SUBS_PATH, json.dumps(DEFAULT_SUBS, indent=1))
    return list(DEFAULT_SUBS)


def cmd_warm(args):
    """Spend the rate-limit budget on the stalest subs so search stays local.

    arctic-shift gives ~4 requests, then locks the IP for ~75s (measured 2026-11-06).
    That is enough to keep a corpus warm, and nowhere near enough to search live.
    """
    subs = (register_subs(args.subs.split(",")) if args.subs
            else default_subs(priority=False))
    order = sorted(subs, key=lambda s: (seen_age_h(s) if seen_age_h(s) is not None else 1e9),
                   reverse=True)
    done, failed, spent = [], [], 0
    t0 = time.time()
    for sub in order:
        if spent >= args.budget or time.time() - t0 > args.deadline:
            break
        spent += 1
        try:
            rows = arctic_posts(sub, "", limit=100)
        except FetchError as e:
            failed.append(f"{sub}({getattr(e, 'code', '?')})")
            if getattr(e, "code", 0) in (422, 429):
                break            # window is shut; no point burning the rest
            continue
        cache_posts(rows)
        mark_seen(sub)
        done.append((sub, len(rows)))
    for sub, n in done:
        print(f"  r/{sub:<22} {n:>3} posts")
    if failed:
        print(f"  throttled/failed: {', '.join(failed)}")
    total = sum(n for _, n in done)
    print(f"warmed {len(done)}/{len(order)} subs, {total} posts, {spent} requests, "
          f"{time.time() - t0:.1f}s")
    try:
        removed = prune()
        con = _db()
        n = con.execute("select count(*) from posts").fetchone()[0]
        con.close()
        size = (CACHE_DIR / "cache.db").stat().st_size / 1e6
        print(f"corpus: {n} posts, {size:.1f}MB at {CACHE_DIR}"
              + (f" (pruned {removed})" if removed else ""))
    except Exception as e:
        log(f"reddit: prune/corpus stats failed: {e}")
    write_status("warm", bool(done), f"{len(done)} subs, {total} posts, {spent} requests")
    return 0 if done else 2


def select_subs(args):
    if getattr(args, "subs", None):
        return register_subs(args.subs.split(","))
    if getattr(args, "all_subs", False):
        return default_subs(priority=False)
    return default_subs(priority=True)


def collect_search(subs, queries, *, live=True, budget=12, deadline=60.0,
                   window="all", limit=40, fresh=6.0, quiet=False):
    """One search over the local corpus, optionally topped up live. Shared by
    `search` and `ask` so both commands see exactly the same ranking."""
    t0 = time.time()
    q = " ".join(queries)
    all_rows, fetched, failed, spent = [], 0, [], 0

    # 1. the local corpus answers first: instant, and it cannot rate-limit.
    for sub in subs:
        all_rows.extend(cached_posts_for(sub, q))

    # 2. top up live inside a request budget, skipping subs fetched recently.
    if live:
        for sub in subs:
            if spent >= budget or time.time() - t0 > deadline:
                break
            age = seen_age_h(sub)
            if age is not None and age < fresh:
                continue
            spent += 1
            try:
                rows = arctic_posts(sub, q, limit=100)
                cache_posts(rows)
                mark_seen(sub)
                fetched += 1
                all_rows.extend(rows)
            except FetchError as e:
                try:                      # a different service, a different limit
                    rows = rss_search(sub, q, window)
                    cache_posts(rows)
                    all_rows.extend(rows)
                    fetched += 1
                except FetchError:
                    failed.append(sub)
                    if getattr(e, "code", 0) in (422, 429):
                        break             # the window is shut; stop burning requests
    picked = rank(all_rows, q, window, limit)
    ages = [a for a in (seen_age_h(s) for s in subs) if a is not None]
    if not quiet and failed:
        log(f"reddit: throttled/failed: {', '.join(failed[:8])}")
    return {"query": queries, "subs": subs, "results": picked, "failed": failed,
            "candidates": len(all_rows), "fetched": fetched, "requests": spent,
            "seconds": round(time.time() - t0, 1),
            "corpus_freshness_h": round(min(ages), 1) if ages else None,
            "providers": HEALTH.summary()}


def search_line(s):
    """The one italic line every human output starts with."""
    subs = s["subs"]
    seen = "corpus cold" if s["corpus_freshness_h"] is None else f"corpus {s['corpus_freshness_h']}h"
    prov = ", ".join(f"{k}({v['ok']}ok/{v['fail']}fail)" for k, v in s["providers"].items()) or "local cache"
    line = (f'_{len(s["results"])} threads · {len(subs)} subs · {seen} · '
            f'{s["candidates"]} candidate posts · {s["fetched"]} live fetches · '
            f'{s["seconds"]}s · {prov}_')
    return line


def cmd_search(args):
    set_deadline(args.deadline)
    queries = args.query or []
    if not queries:
        log("reddit search: need a query")
        return 2
    subs = select_subs(args)
    s = collect_search(subs, queries, live=not getattr(args, "offline", False),
                       budget=args.budget, deadline=args.deadline, window=args.window,
                       limit=args.limit, fresh=args.fresh)
    picked = s["results"]
    if args.json:
        out = json.dumps({k: v for k, v in s.items() if k != "providers"} | {"providers": s["providers"]},
                         indent=1)
    else:
        buf = [f'## reddit: {", ".join(queries)}', search_line(s)]
        if s["failed"]:
            buf.append(f'_throttled: {", ".join(s["failed"][:8])}_')
        if not s["candidates"]:
            buf += ["", "_nothing cached for these subs yet - pass `--live` (the default), "
                        "or run `reddit warm` to fill the corpus_",
                    '_other subreddit? just name it: `reddit search "q" --subs newsname` '
                    "- unknown subs register themselves_"]
        buf.append("")
        for r in picked:
            body = re.sub(r"\s+", " ", (r.get("selftext") or "")).strip()[:220]
            # RSS rows carry no score, comments or timestamp. Saying "1970-01-01 · 0pts"
            # reads as real data, so mark those rows instead of inventing numbers.
            rss = r.get("_via") == "rss"
            when = "date n/a" if rss else iso(r.get("created_utc"))
            pts = "?" if rss else r.get("score", 0)
            nc = "?" if rss else r.get("num_comments", 0)
            tag = " · via rss (no metadata)" if rss else ""
            buf.append(f'- **{r.get("title", "").strip()}** — r/{r.get("subreddit")} '
                       f'· {when} · {pts}pts · {nc}c{tag}')
            buf.append(f'  {url_of(r)}')
            if body:
                buf.append(f'  > {body}')
        out = "\n".join(buf)
    print(out)
    write_status("search", len(picked) > 0,
                 f"{len(picked)} hits over {len(subs)} subs, {s['fetched']} live fetches")
    if args.out:
        _atomic_write(Path(args.out), out + "\n")
    return 0 if picked else 1


def parse_ref(ref: str):
    ref = ref.strip()
    m = re.search(r"/comments/([a-zA-Z0-9]+)", ref)
    if m:
        return m.group(1)
    m = re.search(r"\b([a-z0-9]{5,8})\b", ref)
    return m.group(1) if m else None


def render_thread(post, comments, top=60):
    sub = post.get("subreddit", "?")
    title = html.unescape(post.get("title") or "")
    lines = [f'# {title}', "",
             f"r/{sub} · u/{post.get('author', '?')} · {iso(post.get('created_utc'))} "
             f"· {post.get('score', 0)}pts · {post.get('num_comments', 0)} comments",
             url_of(post), ""]
    body = (post.get("selftext") or "").strip()
    if body and body != "[removed]" and body != "[deleted]":
        lines += [body, ""]
    # depth from parent chain
    byid = {c.get("id"): c for c in comments}
    depth_cache = {}

    def depth(c, guard=0):
        cid = c.get("id")
        if cid in depth_cache:
            return depth_cache[cid]
        d, parent, g = 0, c.get("parent_id") or "", 0
        while parent.startswith("t1_") and g < 8:
            pid = parent[3:]
            if pid not in byid:
                break
            d += 1
            parent = byid[pid].get("parent_id") or ""
            g += 1
        depth_cache[cid] = min(d, 4)
        return depth_cache[cid]

    shown = sorted(comments, key=lambda c: -int(c.get("score") or 0))[:top]
    lines.append(f"## comments — top {len(shown)} of {len(comments)}")
    lines.append("")
    for c in shown:
        body = (c.get("body") or "").strip()
        if not body or body in ("[removed]", "[deleted]"):
            continue
        indent = "  " * depth(c)
        clean = re.sub(r"\n{3,}", "\n\n", body)
        quoted = "\n".join(f"{indent}> {ln}" for ln in clean.splitlines())
        lines += [f"{indent}**u/{c.get('author', '?')}** ({c.get('score', 0)}pts) "
                  f"[{iso(c.get('created_utc'))}]",
                  quoted, ""]
    return "\n".join(lines)


def cmd_mcp(args):
    from . import mcp
    return mcp.serve()


def cmd_thread(args):
    pid = parse_ref(args.ref)
    if not pid:
        log(f"reddit thread: cannot parse a thread id from {args.ref!r}")
        return 2
    if not args.refresh:
        cached = cached_thread(pid)
        if cached:
            sys.stdout.write(cached if cached.endswith("\n") else cached + "\n")
            write_status("thread", True, f"{pid} from cache")
            return 0
    try:
        post = arctic_post(pid)
    except FetchError as e:
        log(f"reddit thread: post {pid} failed: {e}")
        write_status("thread", False, f"{pid}: {e}")
        return 2
    if not post:
        log(f"reddit thread: {pid} not in the archive (too new or deleted)")
        write_status("thread", False, f"{pid} not archived")
        return 1
    comments = cached_comments(pid)
    if comments is None:
        comments = arctic_comments(pid)
        cache_comments(pid, comments)
    md = render_thread(post, comments, top=args.limit)
    cache_thread(pid, md)
    if args.json:
        print(json.dumps({"id": pid, "post": post, "comments": comments,
                          "count": len(comments), "markdown": md}, indent=1))
    else:
        print(md)
    write_status("thread", True, f"{pid}: {len(comments)} comments")
    if args.out:
        _atomic_write(Path(args.out), md)
    return 0


def cmd_sub(args):
    sub = args.sub[2:] if args.sub.startswith("r/") else args.sub
    rows = arctic_posts(sub, "", limit=args.limit, sort=args.sort)
    cache_posts(rows)
    if args.json:
        print(json.dumps({"subreddit": sub, "sort": args.sort, "count": len(rows),
                          "posts": rows[:args.limit]}, indent=1))
    else:
        print(f"## r/{sub} — {args.sort}, {len(rows)} posts")
        print()
        for r in rows[:args.limit]:
            print(f'- {iso(r.get("created_utc"))} · {r.get("score", 0)}pts · '
                  f'{r.get("num_comments", 0)}c · **{html.unescape(r.get("title") or "")}**')
            print(f'  {url_of(r)}')
    write_status("sub", bool(rows), f"r/{sub} {len(rows)} posts")
    return 0 if rows else 1



# ---------------------------------------------------------------- ask

NOISE = ("[removed]", "[deleted]")


def clean_comment(body: str) -> str:
    """Comment markdown -> one plain sentence, verbatim words kept."""
    body = html.unescape(body or "")
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)        # images
    body = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", body)   # [text](url) -> text
    body = re.sub(r"https?://\S+", "", body)                 # bare urls
    body = re.sub(r"^>.*$", "", body, flags=re.M)           # nested quote lines
    body = re.sub(r"[*_`#>]+", "", body)                    # markdown punctuation
    return re.sub(r"\s+", " ", body).strip()


BOT_AUTHORS = ("automoderator",)
PROMO_MARKERS = ("you may be interested", "join our", "check out r/", "discord.gg",
                 "follow me", "my youtube", "subscribe")


def _is_bot(author: str) -> bool:
    a = (author or "").lower()
    return a.startswith(BOT_AUTHORS) or a.endswith("bot") or a.startswith("bot")


def pick_quotes(comments, n, min_len=110, max_len=900):
    """Top comments by score, cleaned, length-bounded, bots and promos dropped."""
    out, seen = [], set()
    for c in sorted(comments, key=lambda c: -int(c.get("score") or 0)):
        body = c.get("body") or ""
        if not body or body.strip() in NOISE:
            continue
        if _is_bot(c.get("author") or ""):
            continue
        text = clean_comment(body)
        if not (min_len <= len(text) <= max_len):
            continue
        low = text.lower()
        if any(m in low for m in PROMO_MARKERS):
            continue
        if len(text.split()) < 15:
            continue
        key = text[:90].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"text": text, "author": c.get("author") or "?",
                    "score": int(c.get("score") or 0),
                    "created": iso(c.get("created_utc")), "id": c.get("id")})
        if len(out) >= n:
            break
    return out


def cmd_ask(args):
    """A question in, real answers with links out. The command the README shows."""
    set_deadline(args.deadline)
    subs = register_subs(args.subs.split(",")) if args.subs else default_subs(priority=True)
    t0 = time.time()
    s = collect_search(subs, args.question, live=not args.offline, budget=args.budget,
                       deadline=args.deadline, window=args.window,
                       limit=max(args.threads * 3, args.threads), fresh=args.fresh)
    threads, failed = [], list(s["failed"])
    for r in s["results"][:args.threads]:
        pid = r.get("id")
        if not pid:
            continue
        if time.time() - t0 > args.deadline:
            failed.append("deadline reached")
            break
        comments = cached_comments(pid, max_age_h=args.thread_age_h)
        if comments is None:
            if args.offline:
                failed.append(f"{pid}(not cached)")
                continue
            log(f"reddit ask: opening {pid} ...")
            try:
                comments = arctic_comments(pid, max_pages=1)
            except FetchError as e:
                failed.append(f"{pid}({getattr(e, 'code', '?')})")
                continue
            cache_comments(pid, comments)
        post = {"id": pid, "title": r.get("title"), "subreddit": r.get("subreddit"),
                "score": r.get("score"), "num_comments": r.get("num_comments"),
                "created_utc": r.get("created_utc"), "permalink": r.get("permalink")}
        quotes = pick_quotes(comments, args.quotes)
        if not quotes:
            continue
        threads.append({
            "title": html.unescape(post.get("title") or ""),
            "url": url_of(post),
            "subreddit": post.get("subreddit") or r.get("subreddit"),
            "created": iso(post.get("created_utc")),
            "score": int(post.get("score") or 0),
            "num_comments": int(post.get("num_comments") or 0),
            "quotes": quotes,
        })
    total_comments = sum(int(t["num_comments"]) for t in threads)
    n_quotes = sum(len(t["quotes"]) for t in threads)
    if args.json:
        out = json.dumps({"question": " ".join(args.question), "subs": subs,
                          "threads": threads, "failed": failed,
                          "seconds": round(time.time() - t0, 1), "fetched": s["fetched"],
                          "coverage": {"threads": len(threads), "comments": total_comments,
                                       "quotes": n_quotes}}, indent=1)
    else:
        buf = [f'## ask: {" ".join(args.question)}',
               f'_{len(threads)} threads · {total_comments} comments · {n_quotes} quotes · '
               f'{len(subs)} subs · {s["fetched"]} live fetches · '
               f'{round(time.time() - t0, 1)}s_']
        if failed:
            buf.append(f'_skipped: {", ".join(failed[:8])}_')
        if not threads:
            buf += ["", "_no quotes yet - the archive window may be shut. "
                        "Try again in a minute, or run `reddit warm` first._"]
        for i, t in enumerate(threads, 1):
            buf += ["", f'### {i}. {t["title"]} - r/{t["subreddit"]} · {t["created"]} '
                        f'· {t["score"]}pts · {t["num_comments"]} comments', t["url"]]
            for q in t["quotes"]:
                buf += ["", f'> [{q["score"]}pts] u/{q["author"]}: {q["text"]}']
        out = "\n".join(buf)
    print(out)
    write_status("ask", bool(threads), f"{len(threads)} threads, {n_quotes} quotes")
    if args.out:
        _atomic_write(Path(args.out), out + "\n")
    return 0 if threads else 1


def write_status(cmd, ok, detail, providers=None):
    payload = {
        "at": now_utc(), "at_iso": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "cmd": cmd, "ok": bool(ok), "code": 0 if ok else 1, "reason": detail,
        "providers": providers if providers is not None else HEALTH.summary(),
    }
    try:
        _atomic_write(STATUS_PATH, json.dumps(payload, indent=1))
    except Exception as e:
        log(f"reddit: could not write status: {e}")


def probe_one(name, fn):
    t0 = time.time()
    try:
        fn()
        return name, True, f"{(time.time() - t0) * 1000:.0f}ms"
    except FetchError as e:
        return name, False, f"{e.code or 'ERR'} {e.body[:90]}"
    except Exception as e:
        return name, False, str(e)[:90]


def cmd_probe(args):
    checks = [
        ("arctic-shift", lambda: arctic_post(PROBE_POST_ID)),
        ("reddit-rss", lambda: rss_search("6thForm", "test", "week")),
        ("pullpush", lambda: get("https://api.pullpush.io/reddit/search/submission/"
                                "?q=test&size=1", "pullpush", tries=1, timeout=12)),
    ]
    results = []
    for name, fn in checks:
        results.append(probe_one(name, fn))
    print(f"{'provider':<14} {'status':<6} detail")
    for name, ok, detail in results:
        print(f"{name:<14} {'UP' if ok else 'DOWN':<6} {detail}")
    core = results[0][1]
    subs_n = len(default_subs())
    print(f"\nrail: {'VIABLE' if core else 'BROKEN'} — {subs_n} subs in the fan-out list, "
          f"cache at {CACHE_DIR}")
    if not core:
        print("arctic-shift is down: fallback is reddit-rss (429-prone). "
              "Try again in a few minutes before telling a session Reddit is unavailable.")
    write_status("probe", core, "; ".join(f"{n}:{'up' if ok else 'down'}" for n, ok, _ in results))
    return 0 if core else 2


def corpus_stats():
    try:
        con = _db()
        n, newest = con.execute("select count(*), max(created_utc) from posts").fetchone()
        subs = con.execute("select count(*) from subs_seen").fetchone()[0]
        con.close()
        return {"posts": n or 0, "subs_warm": subs, "newest_post": newest or 0,
                "path": str(CACHE_DIR / "cache.db")}
    except Exception as e:
        return {"error": str(e)}


def cmd_status(args):
    c = corpus_stats()
    print(f"corpus: {c.get('posts', 0)} posts · {c.get('subs_warm', 0)} subs warm · "
          f"newest {iso(c.get('newest_post'))} · {c.get('path', '')}")
    d = load_json(STATUS_PATH, None)
    if not d:
        print("no status artifact yet — run `reddit probe`")
        return 1
    print(f"{d.get('at_iso')} · {d.get('cmd')} · {'OK' if d.get('ok') else 'FAIL'} · {d.get('reason')}")
    for k, v in (d.get("providers") or {}).items():
        print(f"  {k:<14} ok={v.get('ok', 0)} fail={v.get('fail', 0)} last={v.get('last', '')}")
    return 0


def cmd_subs(args):
    if args.reset:
        _atomic_write(SUBS_PATH, json.dumps(list(DEFAULT_SUBS), indent=1))
    if args.add:
        register_subs(args.add)
    subs = default_subs()
    if not args.add and not args.reset:
        print(f"# {len(subs)} subs in the fan-out list — every `reddit search` queries these")
        print("# add one:  reddit subs --add 6thForm   (or just pass it: "
              "reddit search \"q\" --subs 6thForm,newsub — unknown subs register themselves)")
        print()
    for s in subs:
        print(f"r/{s}")
    return 0


# ---------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(
        prog="reddit", description="Reddit research rail (no login, no API key).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""searching somewhere new - no registration step, no doc to read:
  reddit search "gap year" --subs 6thForm,newsub      # unknown subs are added automatically
  reddit warm --subs newsub                           # or just warm it directly
  reddit subs --add newsub                            # explicit add, same list
  reddit subs                                         # what is in the fan-out list
Every form accepts 6thForm, r/6thForm or https://reddit.com/r/6thForm.
The list persists at ~/.local/state/reddit-rail/reddit_subs.json and is what a bare `reddit search` uses.""")
    p.add_argument("--version", action="version", version=f"reddit-rail {__version__}")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser(
        "search", help="keyword search, fanned out over the curated subs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Searches the local corpus (instant, no network). --live adds fetches.",
        epilog="""examples:
  reddit search "gap year engineering"
  reddit search "anc earbuds" --subs headphones,bose          # adds them if unknown
  reddit search "bose firmware" --live --limit 10
  reddit search "x" --window year --json --out /tmp/x.json""")
    s.add_argument("query", nargs="+")
    s.add_argument("--subs", help="comma list, overrides the curated list")
    s.add_argument("--window", default="all", choices=["hour", "day", "week", "month", "year", "all"])
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--workers", type=int, default=1)
    s.add_argument("--all-subs", action="store_true", help="the full 30-sub list")
    s.add_argument("--budget", type=int, default=24, help="max network requests this run")
    s.add_argument("--offline", action="store_true",
                   help="answer from the local corpus only, never touch the network")
    s.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--fresh", type=float, default=6.0,
                   help="do not refetch a sub whose cache is younger than N hours")
    s.add_argument("--deadline", type=float, default=150.0)
    s.add_argument("--json", action="store_true")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_search)

    t = sub.add_parser("thread", help="a whole thread: post + comments")
    t.add_argument("ref", help="url or post id")
    t.add_argument("--limit", type=int, default=60, help="top N comments (default 60)")
    t.add_argument("--refresh", action="store_true", help="ignore the cache")
    t.add_argument("--json", action="store_true", help="post + comments as one JSON object")
    t.add_argument("--out")
    t.set_defaults(fn=cmd_thread)

    u = sub.add_parser("sub", help="recent posts in one subreddit")
    u.add_argument("sub")
    u.add_argument("--limit", type=int, default=25)
    u.add_argument("--sort", default="desc", choices=["desc", "asc"])
    u.add_argument("--json", action="store_true")
    u.set_defaults(fn=cmd_sub)

    w = sub.add_parser("warm", help="fetch the stalest subs into the local corpus")
    w.add_argument("--subs", help="comma list (default: the full list)")
    w.add_argument("--budget", type=int, default=4, help="max requests (window is ~4)")
    w.add_argument("--deadline", type=float, default=180.0)
    w.set_defaults(fn=cmd_warm)

    a = sub.add_parser(
        "ask", help="a question in, real answers with links out",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Searches, opens the top threads, and prints the highest-scoring "
                    "comments verbatim with their score and permalink.",
        epilog="""examples:
  reddit ask "is a gap year worth it" --subs 6thForm,UniUK
  reddit ask "bose anc hiss" --threads 3 --quotes 8 --json""")
    a.add_argument("question", nargs="+")
    a.add_argument("--subs", help="comma list, overrides the curated list")
    a.add_argument("--threads", type=int, default=3, help="how many threads to open (default 3)")
    a.add_argument("--quotes", type=int, default=5, help="quotes per thread (default 5)")
    a.add_argument("--comment-pool", type=int, default=120,
                   help="comments paged per thread before picking (default 120)")
    a.add_argument("--window", default="all", choices=["hour", "day", "week", "month", "year", "all"])
    a.add_argument("--budget", type=int, default=12, help="max search requests this run")
    a.add_argument("--deadline", type=float, default=90.0)
    a.add_argument("--fresh", type=float, default=6.0)
    a.add_argument("--thread-age-h", type=float, default=72.0,
                   help="reuse an opened thread younger than N hours (default 72)")
    a.add_argument("--offline", action="store_true")
    a.add_argument("--json", action="store_true")
    a.add_argument("--out")
    a.set_defaults(fn=cmd_ask)

    m = sub.add_parser("mcp", help="stdio MCP server: reddit_search, reddit_thread, reddit_ask")
    m.set_defaults(fn=cmd_mcp)

    pr = sub.add_parser("probe", help="health check every provider, write the status artifact")
    pr.set_defaults(fn=cmd_probe)

    st = sub.add_parser("status", help="last run as recorded")
    st.set_defaults(fn=cmd_status)

    sb = sub.add_parser(
        "subs", help="show/extend the fan-out list",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="The subs every `reddit search` queries. Adding one is optional - "
                    "passing an unknown sub to `search` or `warm` registers it for you.",
        epilog="""examples:
  reddit subs                                  # list them
  reddit subs --add 6thForm,uklaw              # add one or several
  reddit subs --add https://reddit.com/r/uklaw # a url works too
  reddit subs --reset                          # back to the built-in 30""")
    sb.add_argument("--add", action="append")
    sb.add_argument("--reset", action="store_true")
    sb.set_defaults(fn=cmd_subs)
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return 2
    try:
        return args.fn(args)
    except FetchError as e:
        log(f"reddit: provider failure: {e}")
        write_status(args.cmd, False, str(e))
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        DEADLINE[0] = 0.0


if __name__ == "__main__":
    sys.exit(main())
