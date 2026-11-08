"""News provider + status tests. No network: fixtures + cache seeding."""
import os
import sqlite3
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from data import news  # noqa: E402

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Apple beats earnings!!!</title><link>https://x.test/a1</link>
<pubDate>Mon, 21 Sep 2026 12:00:00 GMT</pubDate><source>Yahoo</source></item>
<item><title>Apple beats earnings</title><link>https://x.test/a2</link>
<pubDate>Mon, 21 Sep 2026 12:05:00 GMT</pubDate><source>Google</source></item>
<item><title>Fed holds rates steady</title><link>https://x.test/b1</link>
<pubDate>Mon, 21 Sep 2026 11:00:00 GMT</pubDate></item>
</channel></rss>"""


class TestNews(unittest.TestCase):
    def test_parse_rss(self):
        items = news._parse_rss(RSS.encode(), "Test")
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["title"], "Apple beats earnings!!!")
        self.assertTrue(items[0]["ts"] > 0)
        self.assertEqual(items[2]["source"], "Test")

    def test_dedupe(self):
        items = news._parse_rss(RSS.encode(), "Test")
        out = news.dedupe(items)
        self.assertEqual(len(out), 2)  # apple dupes collapse (punctuation-insensitive)

    def test_parse_bad_xml(self):
        self.assertEqual(news._parse_rss(b"not xml", "T"), [])

    def test_cache_serves_without_network(self):
        news._CACHE["sym:TEST"] = (9999999999, [{"title": "cached", "link": "u",
                                                 "ts": 1, "published": "",
                                                 "source": "cache"}])
        try:
            res = news.get_news("TEST", limit=5)
            self.assertFalse(res["stale"])
            self.assertEqual(res["items"][0]["title"], "cached")
        finally:
            news._CACHE.pop("sym:TEST", None)


class TestStatus(unittest.TestCase):
    def test_status_payload_shape(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql"), encoding="utf-8") as f:
            conn.executescript(f.read())
        try:
            from api.routes import _status_payload
            st = _status_payload(conn)
            self.assertTrue(st["ok"])
            self.assertEqual(st["service"], "mafutani")
            self.assertIn("users", st["tables"])
            self.assertIn("feeds", st)
            self.assertGreaterEqual(st["uptime_s"], 0)
        finally:
            conn.close()

    def test_terminal_status_verb(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        with open(os.path.join(WORKDIR, "data", "schema.sql"), encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.execute("INSERT INTO users (email, password_hash, tier) VALUES (?, ?, ?)",
                     ("s@example.com", "x", "Free"))
        try:
            from core.terminal import execute
            ok, out, data = execute(conn, {"id": 1, "tier": "Free"}, "status")
            self.assertTrue(ok, out)
            self.assertIn("tables", data)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
