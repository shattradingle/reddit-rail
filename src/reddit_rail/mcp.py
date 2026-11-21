"""Stdio MCP server for reddit-rail.

Three tools, no auth, no server process of its own: the client spawns this file,
speaks line-delimited JSON-RPC over stdin/stdout, and the same cache the CLI uses
is what it reads.

    "mcpServers": {"reddit": {"command": "reddit", "args": ["mcp"]}}
"""
from __future__ import annotations

import contextlib
import io
import json
import sys

from . import rail

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "reddit_ask",
        "description": (
            "Ask Reddit a question. Searches the curated subreddits, opens the top "
            "threads, and returns the highest-scoring comments verbatim with their "
            "score and permalink. Use this when you want opinions or first-hand "
            "experience rather than links."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "What you want to know."},
                "subs": {"type": "string",
                         "description": "Optional comma list, e.g. '6thForm,UniUK'."},
                "threads": {"type": "integer", "default": 3},
                "quotes": {"type": "integer", "default": 5},
            },
            "required": ["question"],
        },
    },
    {
        "name": "reddit_search",
        "description": (
            "Search Reddit threads by keyword across the curated subreddits "
            "(plus any named in `subs`). Returns title, subreddit, date, score, "
            "comment count and permalink."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "subs": {"type": "string", "description": "Optional comma list."},
                "limit": {"type": "integer", "default": 15},
                "window": {"type": "string", "enum": ["hour", "day", "week", "month", "year", "all"],
                           "default": "all"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "reddit_thread",
        "description": (
            "Read a whole Reddit thread: the post plus every comment, rendered as "
            "markdown with score and nesting. Accepts a permalink or a post id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Permalink or post id."},
                "limit": {"type": "integer", "default": 60},
            },
            "required": ["ref"],
        },
    },
]


class _Args:
    """Attribute bag shaped like the argparse namespace the cmd_* functions take."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _capture(fn, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = fn(_Args(**kw))
    return buf.getvalue().strip(), code


def tool_ask(question: str, subs: str | None = None, threads: int = 3, quotes: int = 5) -> str:
    text, _ = _capture(rail.cmd_ask, question=str(question).split(), subs=subs,
                       threads=int(threads), quotes=int(quotes), comment_pool=120,
                       window="all", budget=12, deadline=90.0, fresh=6.0,
                       offline=False, json=False, out=None)
    return text or "No quotes found. The archive window may be shut; try again in a minute."


def tool_search(query: str, subs: str | None = None, limit: int = 15,
                window: str = "all") -> str:
    subs_ = (rail.register_subs(str(subs).split(",")) if subs
             else rail.default_subs(priority=True))
    s = rail.collect_search(subs_, [str(query)], live=True, budget=8, deadline=60.0,
                            window=window, limit=int(limit))
    lines = [f"## reddit: {query}", rail.search_line(s), ""]
    if not s["results"]:
        return "\n".join(lines + ["No threads found. Try broader words, or name the subreddit."])
    for r in s["results"]:
        lines.append(f'- **{r.get("title", "").strip()}** — r/{r.get("subreddit")} · '
                     f'{rail.iso(r.get("created_utc"))} · {r.get("score", 0)}pts · '
                     f'{r.get("num_comments", 0)}c')
        lines.append(f'  {rail.url_of(r)}')
    return "\n".join(lines)


def tool_thread(ref: str, limit: int = 60) -> str:
    pid = rail.parse_ref(str(ref))
    if not pid:
        return f"Cannot parse a Reddit post id from {ref!r}."
    cached = rail.cached_thread(pid)
    if cached:
        return cached
    try:
        post = rail.arctic_post(pid)
    except rail.FetchError as e:
        return f"Fetch failed for {pid}: {e}"
    if not post:
        return f"{pid} is not in the archive (too new or deleted)."
    comments = rail.arctic_comments(pid)
    md = rail.render_thread(post, comments, top=int(limit))
    rail.cache_thread(pid, md)
    return md


HANDLERS = {
    "reddit_ask": lambda a: tool_ask(a.get("question", ""), a.get("subs"),
                                     a.get("threads", 3), a.get("quotes", 5)),
    "reddit_search": lambda a: tool_search(a.get("query", ""), a.get("subs"),
                                           a.get("limit", 15), a.get("window", "all")),
    "reddit_thread": lambda a: tool_thread(a.get("ref", ""), a.get("limit", 60)),
}


def handle(req: dict):
    """One JSON-RPC request in, one response dict (or None for a notification) out."""
    method = req.get("method")
    rid = req.get("id")
    if not isinstance(req, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "invalid request"}}
    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "reddit-rail", "version": rail.__version__},
        }
    elif method in ("notifications/initialized", "notifications/cancelled"):
        return None
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32602, "message": f"unknown tool: {name}"}}
        try:
            text = fn(args)
            result = {"content": [{"type": "text", "text": text}], "isError": False}
        except Exception as e:  # a broken provider must not kill the server
            result = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
                      "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def serve(stdin=None, stdout=None) -> int:
    fin = stdin or sys.stdin
    fout = stdout or sys.stdout
    for line in fin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            fout.write(json.dumps({"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32700, "message": "parse error"}}) + "\n")
            fout.flush()
            continue
        resp = handle(req)
        if resp is not None:
            fout.write(json.dumps(resp) + "\n")
            fout.flush()
    return 0
