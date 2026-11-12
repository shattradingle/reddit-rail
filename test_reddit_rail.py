"""Offline tests for reddit-rail. No network, no credentials, no Reddit.

Run: python -m unittest -v
"""
import unittest
import tempfile
from pathlib import Path

import reddit


class Tokens(unittest.TestCase):
    def test_drops_short_words_and_punctuation(self):
        self.assertEqual(reddit.tokens("Gap YEAR: engineering?"),
                         ["gap", "year", "engineering"])

    def test_empty_query(self):
        self.assertEqual(reddit.tokens(""), [])


class Urls(unittest.TestCase):
    def test_relative_permalink_becomes_absolute(self):
        row = {"permalink": "/r/6thForm/comments/abc/x/"}
        self.assertEqual(reddit.url_of(row),
                         "https://www.reddit.com/r/6thForm/comments/abc/x/")

    def test_absolute_permalink_is_left_alone(self):
        row = {"permalink": "https://www.reddit.com/r/x/comments/abc/x/"}
        self.assertEqual(reddit.url_of(row), row["permalink"])


class StateLocations(unittest.TestCase):
    def test_state_dir_is_its_own_thing(self):
        # regression guard: this must never default into a private dot-dir
        self.assertIn("reddit-rail", str(reddit.STATE_DIR))

    def test_cache_and_status_sit_under_state_dir(self):
        self.assertIn(str(reddit.STATE_DIR), str(reddit.CACHE_DIR))
        self.assertIn(str(reddit.STATE_DIR), str(reddit.STATUS_PATH))


class Cache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = reddit.CACHE_DIR
        reddit.CACHE_DIR = Path(self.tmp.name) / "reddit_cache"

    def tearDown(self):
        reddit.CACHE_DIR = self._orig
        self.tmp.cleanup()

    def _post(self, pid="abc1", sub="6thForm", title="Bath aero gap year?"):
        return {"id": pid, "subreddit": sub, "title": title, "score": 4,
                "num_comments": 6, "created_utc": 1777000000,
                "permalink": f"/r/{sub}/comments/{pid}/x/", "selftext": "body text"}

    def test_roundtrip_returns_the_post(self):
        reddit.cache_posts([self._post()])
        reddit.mark_run("6thForm", "gap year")
        rows = reddit.cached_query("6thForm", "gap year", fresh_h=6)
        self.assertIsNotNone(rows)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "abc1")
        self.assertEqual(rows[0]["_via"], "cache")

    def test_stale_cache_is_not_used(self):
        reddit.cache_posts([self._post()])
        reddit.mark_run("6thForm", "gap year")
        # a negative window puts every recorded run in the past by definition
        self.assertIsNone(reddit.cached_query("6thForm", "gap year", fresh_h=-1))

    def test_missing_run_is_not_used(self):
        reddit.cache_posts([self._post()])
        self.assertIsNone(reddit.cached_query("6thForm", "never fetched", fresh_h=6))

    def test_query_must_match_the_post(self):
        reddit.cache_posts([self._post()])
        reddit.mark_run("6thForm", "zzz")
        self.assertEqual(reddit.cached_query("6thForm", "zzz", fresh_h=6), [])


class Arcade(unittest.TestCase):
    def test_every_subcommand_exists(self):
        p = reddit.build_parser()
        for name in ("search", "thread", "sub", "warm", "probe", "status", "subs"):
            self.assertIn(name, p._subparsers._group_actions[0].choices)

    def test_no_command_exits_2(self):
        self.assertEqual(reddit.main([]), 2)

    def test_search_needs_a_query(self):
        with self.assertRaises(SystemExit):
            reddit.main(["search"])


if __name__ == "__main__":
    unittest.main()
