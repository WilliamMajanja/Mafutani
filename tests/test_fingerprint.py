"""Tests for adversarial tool fingerprinting and honeypot detection.

The failure modes here are specific:
* a honeypot that misses a scan path
* a tool signature that matches a browser
* a throttle that never expires, or expires immediately
* a fingerprinting failure that blocks a legitimate request
"""
import os
import sys
import time
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from core.fingerprint import Fingerprint, HoneypotHit  # noqa: E402


class TestHoneypot(unittest.TestCase):
    def test_detects_env_file(self):
        fp = Fingerprint()
        self.assertTrue(fp.is_honeypot("/.env"))

    def test_detects_git_config(self):
        fp = Fingerprint()
        self.assertTrue(fp.is_honeypot("/.git/config"))

    def test_detects_wp_login(self):
        fp = Fingerprint()
        self.assertTrue(fp.is_honeypot("/wp-login.php"))

    def test_misses_real_paths(self):
        fp = Fingerprint()
        self.assertFalse(fp.is_honeypot("/api/status"))
        self.assertFalse(fp.is_honeypot("/"))
        self.assertFalse(fp.is_honeypot("/login"))

    def test_case_insensitive(self):
        fp = Fingerprint()
        self.assertTrue(fp.is_honeypot("/.ENV"))

    def test_empty_path(self):
        fp = Fingerprint()
        self.assertFalse(fp.is_honeypot(""))
        self.assertFalse(fp.is_honeypot(None))

    def test_record_hit(self):
        fp = Fingerprint()
        hit = fp.record_honeypot("/.env", "1.2.3.4")
        self.assertIsInstance(hit, HoneypotHit)
        self.assertEqual(hit.path, "/.env")
        self.assertEqual(hit.ip, "1.2.3.4")
        self.assertEqual(len(fp.honeypot_hits()), 1)

    def test_honeypot_hits_bounded(self):
        fp = Fingerprint()
        for i in range(10):
            fp.record_honeypot("/.env", "1.2.3.4")
        self.assertEqual(len(fp.honeypot_hits()), 10)


class TestToolSignature(unittest.TestCase):
    def test_detects_sqlmap(self):
        fp = Fingerprint()
        self.assertTrue(fp.is_tool("sqlmap/1.7.2"))

    def test_detects_nikto(self):
        fp = Fingerprint()
        self.assertTrue(fp.is_tool("Nikto/2.1.6"))

    def test_detects_python_requests(self):
        fp = Fingerprint()
        self.assertTrue(fp.is_tool("python-requests/2.31.0"))

    def test_misses_browser(self):
        fp = Fingerprint()
        self.assertFalse(fp.is_tool(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"))

    def test_misses_empty(self):
        fp = Fingerprint()
        self.assertFalse(fp.is_tool(""))
        self.assertFalse(fp.is_tool(None))

    def test_tool_signature_returns_name(self):
        fp = Fingerprint()
        self.assertEqual(fp.tool_signature("sqlmap/1.7"), "sqlmap")


class TestThrottle(unittest.TestCase):
    def test_not_throttled_initially(self):
        fp = Fingerprint()
        self.assertFalse(fp.is_throttled("1.2.3.4"))

    def test_throttle_sets_state(self):
        fp = Fingerprint()
        fp.throttle("1.2.3.4", seconds=60)
        self.assertTrue(fp.is_throttled("1.2.3.4"))

    def test_throttle_expires(self):
        fp = Fingerprint()
        fp.throttle("1.2.3.4", seconds=0)
        time.sleep(0.01)
        self.assertFalse(fp.is_throttled("1.2.3.4"))

    def test_throttle_ignores_empty_ip(self):
        fp = Fingerprint()
        fp.throttle("", seconds=60)
        self.assertFalse(fp.is_throttled(""))

    def test_clear_removes_throttle(self):
        fp = Fingerprint()
        fp.throttle("1.2.3.4", seconds=60)
        fp.clear("1.2.3.4")
        self.assertFalse(fp.is_throttled("1.2.3.4"))


class TestScore(unittest.TestCase):
    def test_no_markers_adds_points(self):
        fp = Fingerprint()
        value, reasons = fp.score_request(
            {"host": "x"}, "Mozilla/5.0", "/api/status")
        self.assertGreaterEqual(value, 2)

    def test_browser_markers_reduce_score(self):
        # A real browser sends accept, accept-language, and a referer.
        # With all of those present the score is 0.
        fp = Fingerprint()
        headers = {
            "accept": "text/html",
            "accept-language": "en-US",
            "accept-encoding": "gzip",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "referer": "http://localhost/",
        }
        value, _ = fp.score_request(headers, "Mozilla/5.0", "/api/status")
        self.assertEqual(value, 0)

    def test_tool_ua_adds_points(self):
        fp = Fingerprint()
        value, reasons = fp.score_request({}, "sqlmap/1.7", "/api/status")
        self.assertGreaterEqual(value, 3)

    def test_deterministic(self):
        fp = Fingerprint()
        a = fp.score_request({}, "sqlmap/1.7", "/api/status")
        b = fp.score_request({}, "sqlmap/1.7", "/api/status")
        self.assertEqual(a, b)


class TestIsolation(unittest.TestCase):
    def test_instances_are_independent(self):
        a = Fingerprint()
        b = Fingerprint()
        a.throttle("1.2.3.4", seconds=60)
        self.assertFalse(b.is_throttled("1.2.3.4"))


if __name__ == "__main__":
    unittest.main()
