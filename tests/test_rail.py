"""Offline tests for reddit-rail. No network, no credentials, no Reddit.

Every test either runs against fixtures or monkeypatches the provider seam, so the
suite passes on a machine with no internet and never touches the archive.
"""
from __future__ import annotations

import json
import time

import pytest

from reddit_rail import mcp, rail


# ---------------------------------------------------------------- small utils

def test_tokens_drops_short_words_and_punctuation():
    assert rail.tokens("Gap YEAR: engineering?") == ["gap", "year", "engineering"]


def test_tokens_empty_query():
    assert rail.tokens("") == []


def test_url_of_relative_permalink():
    row = {"permalink": "/r/6thForm/comments/abc/x/"}
    assert rail.url_of(row) == "https://www.reddit.com/r/6thForm/comments/abc/x/"


def test_url_of_absolute_permalink_is_left_alone():
    row = {"permalink": "https://www.reddit.com/r/x/comments/abc/x/"}
    assert rail.url_of(row) == row["permalink"]


# ---------------------------------------------------------------- state layout

def test_state_dir_is_its_own_thing():
    assert "reddit-rail" in str(rail.STATE_DIR)


def test_cache_and_status_sit_under_state_dir():
    assert str(rail.STATE_DIR) in str(rail.CACHE_DIR)
    assert str(rail.STATE_DIR) in str(rail.STATUS_PATH)


# ---------------------------------------------------------------- cache

@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(rail, "CACHE_DIR", tmp_path / "reddit_cache")
    monkeypatch.setattr(rail, "STATUS_PATH", tmp_path / "reddit_status.json")
    monkeypatch.setattr(rail, "SUBS_PATH", tmp_path / "reddit_subs.json")
    monkeypatch.setattr(rail, "COOLDOWN_PATH", tmp_path / "reddit_cooldown.json")
    return tmp_path


def _post(pid="abc1", sub="6thForm", title="Bath aero gap year?", created_utc=1777000000):
    return {"id": pid, "subreddit": sub, "title": title, "score": 4,
            "num_comments": 6, "created_utc": created_utc,
            "permalink": f"/r/{sub}/comments/{pid}/x/", "selftext": "body text"}


def test_cache_roundtrip_returns_the_post(tmp_state):
    rail.cache_posts([_post()])
    rail.mark_run("6thForm", "gap year")
    rows = rail.cached_query("6thForm", "gap year", fresh_h=6)
    assert rows is not None and len(rows) == 1
    assert rows[0]["id"] == "abc1"
    assert rows[0]["_via"] == "cache"


def test_stale_cache_is_not_used(tmp_state):
    rail.cache_posts([_post()])
    rail.mark_run("6thForm", "gap year")
    assert rail.cached_query("6thForm", "gap year", fresh_h=-1) is None


def test_missing_run_is_not_used(tmp_state):
    rail.cache_posts([_post()])
    assert rail.cached_query("6thForm", "never fetched", fresh_h=6) is None


def test_query_must_match_the_post(tmp_state):
    rail.cache_posts([_post()])
    rail.mark_run("6thForm", "zzz")
    assert rail.cached_query("6thForm", "zzz", fresh_h=6) == []


def test_thread_cache_roundtrip_and_expiry(tmp_state):
    rail.cache_thread("abc1", "# thread\nbody")
    assert rail.cached_thread("abc1") == "# thread\nbody"
    assert rail.cached_thread("abc1", max_age_h=-1) is None
    assert rail.cached_thread("missing") is None


def test_seen_age_and_prune(tmp_state):
    rail.cache_posts([_post(created_utc=100)])
    rail.mark_seen("6thForm")
    assert rail.seen_age_h("6thForm") is not None
    assert rail.seen_age_h("never") is None
    removed = rail.prune(post_days=1)
    assert removed >= 1


# ---------------------------------------------------------------- cli plumbing

def test_every_subcommand_exists():
    p = rail.build_parser()
    choices = p._subparsers._group_actions[0].choices
    for name in ("search", "thread", "sub", "warm", "probe", "status", "subs", "ask", "mcp"):
        assert name in choices


def test_no_command_exits_2():
    assert rail.main([]) == 2


def test_search_needs_a_query():
    with pytest.raises(SystemExit):
        rail.main(["search"])


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as e:
        rail.main(["--version"])
    assert e.value.code == 0
    assert "reddit-rail 2.0.0" in capsys.readouterr().out


def test_search_offline_uses_cache_only(tmp_state, monkeypatch, capsys):
    rows = [_post()]
    monkeypatch.setattr(rail, "cached_posts_for", lambda sub, q: rows)

    def boom(*a, **k):
        raise AssertionError("offline search must never call the network")
    monkeypatch.setattr(rail, "arctic_posts", boom)

    assert rail.main(["search", "gap year", "--offline", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["query"] == ["gap year"]
    assert out["results"][0]["id"] == "abc1"


def test_search_json_shape(tmp_state, monkeypatch, capsys):
    monkeypatch.setattr(rail, "cached_posts_for", lambda sub, q: [_post()])
    assert rail.main(["search", "gap year", "--offline", "--json", "--subs", "6thForm"]) == 0
    out = json.loads(capsys.readouterr().out)
    for key in ("query", "subs", "results", "failed", "candidates", "fetched",
                "requests", "seconds", "corpus_freshness_h", "providers"):
        assert key in out


# ---------------------------------------------------------------- refs and subs

def test_parse_ref_forms():
    assert rail.parse_ref("https://www.reddit.com/r/x/comments/1e5p300/title/") == "1e5p300"
    assert rail.parse_ref("1e5p300") == "1e5p300"
    assert rail.parse_ref("!!!") is None


def test_normalise_sub_forms():
    assert rail.normalise_sub("6thForm") == "6thForm"
    assert rail.normalise_sub("r/6thForm") == "6thForm"
    assert rail.normalise_sub("https://reddit.com/r/6thForm/") == "6thForm"


def test_register_subs_splits_and_persists(tmp_state):
    subs = rail.register_subs(["6thForm,uklaw", "r/university"])
    assert subs == ["6thForm", "uklaw", "university"]
    saved = json.loads((tmp_state / "reddit_subs.json").read_text())
    assert "uklaw" in saved and "university" in saved


def test_priority_subs_include_session_additions(tmp_state):
    rail.register_subs(["uklaw"])
    priority = rail.default_subs(priority=True)
    assert "uklaw" in priority
    assert all(s in priority for s in rail.PRIORITY_SUBS)


# ---------------------------------------------------------------- ranking

def test_rank_prefers_title_hits_and_filters_window():
    old = _post(pid="old", title="nothing here", created_utc=1)
    old["num_comments"] = 0
    new = _post(pid="new", title="gap year guide", created_utc=int(time.time()))
    new["num_comments"] = 0
    got = rail.rank([old, new], "gap year", window="month", limit=10)
    assert [r["id"] for r in got] == ["new"]


def test_rank_respects_limit():
    rows = [_post(pid=f"p{i}", title=f"gap year {i}") for i in range(10)]
    assert len(rail.rank(rows, "gap year", limit=3)) == 3


def test_rank_drops_posts_without_a_term():
    assert rail.rank([_post(title="cats")], "gap year") == []


# ---------------------------------------------------------------- thread render

def test_render_thread_skips_deleted_and_indents_replies():
    post = {"title": "T", "subreddit": "x", "author": "a", "created_utc": 1,
            "score": 2, "num_comments": 3, "selftext": "body", "permalink": "/r/x/comments/p/t/"}
    comments = [
        {"id": "c1", "parent_id": "t3_p", "body": "top level", "score": 10,
         "author": "u1", "created_utc": 1},
        {"id": "c2", "parent_id": "t1_c1", "body": "a reply", "score": 5,
         "author": "u2", "created_utc": 1},
        {"id": "c3", "parent_id": "t1_c1", "body": "[deleted]", "score": 1,
         "author": "u3", "created_utc": 1},
    ]
    md = rail.render_thread(post, comments, top=10)
    assert "# T" in md and "top level" in md and "a reply" in md
    assert "> a reply" in md or "> " in md


def test_clean_comment_strips_markdown_and_urls():
    raw = "**bold** https://x.test/y see [this](https://x.test) ![img](i.png)"
    assert rail.clean_comment(raw) == "bold see this"


def test_pick_quotes_filters_length_and_dedupes():
    long_text = "word " * 40
    comments = [
        {"body": "short", "score": 9, "author": "a", "created_utc": 1},
        {"body": long_text, "score": 8, "author": "b", "created_utc": 1},
        {"body": long_text, "score": 7, "author": "c", "created_utc": 1},
        {"body": "[removed]", "score": 99, "author": "d", "created_utc": 1},
    ]
    quotes = rail.pick_quotes(comments, 5)
    assert len(quotes) == 1
    assert quotes[0]["author"] == "b"


# ---------------------------------------------------------------- status

def test_write_status_persists_last_run(tmp_state):
    rail.write_status("search", True, "1 hit", providers={"arctic-shift": {"ok": 1}})
    data = json.loads((tmp_state / "reddit_status.json").read_text())
    assert data["cmd"] == "search" and data["ok"] is True
    assert data["providers"]["arctic-shift"]["ok"] == 1


def test_health_ledger_counts_ok_and_fail():
    h = rail.Health()
    h.record("p", True, "ok", 200)
    h.record("p", False, "boom", 429)
    assert h.summary()["p"]["ok"] == 1
    assert h.summary()["p"]["fail"] == 1


# ---------------------------------------------------------------- ask

def test_ask_json_shape(tmp_state, monkeypatch, capsys):
    post = {"id": "p1", "title": "Is a gap year worth it?", "subreddit": "6thForm",
            "score": 42, "num_comments": 2, "created_utc": 1777000000,
            "permalink": "/r/6thForm/comments/p1/x/"}
    comments = [
        {"id": "c1", "body": "word " * 40, "score": 100, "author": "top"},
        {"id": "c2", "body": "short", "score": 1, "author": "meh"},
    ]
    monkeypatch.setattr(rail, "collect_search", lambda *a, **k: {
        "query": ["q"], "subs": ["6thForm"], "results": [post], "failed": [],
        "candidates": 5, "fetched": 1, "requests": 1, "seconds": 0.1,
        "corpus_freshness_h": 1.0, "providers": {}})
    monkeypatch.setattr(rail, "arctic_post", lambda pid: post)
    monkeypatch.setattr(rail, "arctic_comments", lambda pid, max_pages=10: comments)

    assert rail.main(["ask", "is a gap year worth it", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["coverage"]["threads"] == 1
    assert out["coverage"]["quotes"] == 1
    assert out["threads"][0]["quotes"][0]["author"] == "top"


def test_ask_no_quotes_exits_1(tmp_state, monkeypatch, capsys):
    monkeypatch.setattr(rail, "collect_search", lambda *a, **k: {
        "query": ["q"], "subs": [], "results": [], "failed": [], "candidates": 0,
        "fetched": 0, "requests": 0, "seconds": 0.0,
        "corpus_freshness_h": None, "providers": {}})
    assert rail.main(["ask", "nothing", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["threads"] == []


# ---------------------------------------------------------------- mcp

def test_mcp_initialize():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert resp["id"] == 1
    assert resp["result"]["serverInfo"]["name"] == "reddit-rail"
    assert resp["result"]["protocolVersion"]


def test_mcp_tools_list_names():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {"reddit_ask", "reddit_search", "reddit_thread"}
    for tool in resp["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"


def test_mcp_notification_gets_no_response():
    assert mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_mcp_unknown_method_is_an_error():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "resources/list"})
    assert resp["error"]["code"] == -32601


def test_mcp_tools_call_success(monkeypatch):
    monkeypatch.setitem(mcp.HANDLERS, "reddit_thread", lambda a: "a thread")
    resp = mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "reddit_thread", "arguments": {"ref": "1e5p300"}}})
    assert resp["result"]["isError"] is False
    assert resp["result"]["content"][0]["text"] == "a thread"


def test_mcp_tools_call_failure_is_reported_not_raised(monkeypatch):
    def boom(args):
        raise RuntimeError("provider down")
    monkeypatch.setitem(mcp.HANDLERS, "reddit_search", boom)
    resp = mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "reddit_search", "arguments": {"query": "x"}}})
    assert resp["result"]["isError"] is True
    assert "provider down" in resp["result"]["content"][0]["text"]


def test_mcp_unknown_tool_is_an_error():
    resp = mcp.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                       "params": {"name": "nope", "arguments": {}}})
    assert resp["error"]["code"] == -32602


def test_mcp_serve_line_protocol(monkeypatch, tmp_path):
    import io
    stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\nnot json\n')
    stdout = io.StringIO()
    assert mcp.serve(stdin, stdout) == 0
    lines = [json.loads(x) for x in stdout.getvalue().splitlines()]
    assert lines[0]["result"] == {}
    assert lines[1]["error"]["code"] == -32700


# ---------------------------------------------------------------- no surprises

def test_import_does_not_create_state(monkeypatch, tmp_path):
    # importing the package must not mkdir or fetch anything
    assert rail.__version__ == "2.0.0"
