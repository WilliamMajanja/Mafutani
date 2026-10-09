"""Tests for `core.logsafe` -- the log redaction choke point.

This module exists because of a real leak: the stdlib logged the entire
request line, query string included, so any credential that reached a URL
was persisted to disk in cleartext. These tests pin the behaviour that
closes it, including the nested-URL case that defeated the first
implementation.
"""
import os
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from core import logsafe  # noqa: E402


class TestRedactValueText(unittest.TestCase):
    def test_masks_known_sensitive_keys(self):
        for key in ("password", "passwd", "pwd", "token", "secret",
                    "api_key", "csrf", "code", "authorization", "signature"):
            out = logsafe.redact_value_text("{}=LEAKME".format(key))
            self.assertNotIn("LEAKME", out, key)
            self.assertIn("***", out, key)

    def test_preserves_non_sensitive_keys(self):
        # `session` is deliberately absent: a session id is credential
        # material, so it is masked (see test_credential_shaped_names_are_covered).
        for key in ("symbol", "limit", "min_vol", "offset", "interval",
                    "max_spread_bps", "timeframe", "venue"):
            out = logsafe.redact_value_text("{}={}".format(key, "AAPL"))
            self.assertIn("AAPL", out, key)

    def test_nested_url_does_not_defeat_redaction(self):
        """The regression that mattered.

        A naive `re.sub` matches `https` first (name) with the whole rest of
        the URL as its value, then resumes scanning *after* it -- so the
        sensitive key inside the URL is never seen.
        """
        out = logsafe.redact_value_text(
            "GET https://u:pw@host/v1?api_key=TOPSECRET -> 200")
        self.assertNotIn("TOPSECRET", out)

    def test_multiple_sensitive_params_in_one_url(self):
        out = logsafe.redact_value_text(
            "https://x/o?secret=s1&symbol=BTC&api_key=k1&password=p1")
        for leak in ("s1", "k1", "p1"):
            self.assertNotIn(leak, out, leak)
        self.assertIn("symbol=BTC", out, "non-sensitive must survive")

    def test_case_insensitive(self):
        for variant in ("PassWord", "PASSWORD", "pAsSwOrD", "Token", "API_KEY"):
            out = logsafe.redact_value_text("{}=LEAKME".format(variant))
            self.assertNotIn("LEAKME", out, variant)

    def test_word_boundary_respected(self):
        """`passwordless` is not `password`."""
        out = logsafe.redact_value_text("passwordless=true&mypassword=no")
        self.assertIn("passwordless=true", out)
        self.assertNotIn("mypassword=no", out)

    def test_colon_separator(self):
        out = logsafe.redact_value_text("password: LEAKME")
        self.assertNotIn("LEAKME", out)

    def test_quoted_values_keep_quotes(self):
        out = logsafe.redact_value_text('password="LEAK ME"')
        self.assertNotIn("LEAK ME", out)
        self.assertTrue(out.startswith('password="'))

    def test_empty_and_none(self):
        self.assertEqual(logsafe.redact_value_text(""), "")
        self.assertEqual(logsafe.redact_value_text(None), None)


class TestRedactUrl(unittest.TestCase):
    def test_userinfo_password_masked(self):
        out = logsafe.redact_url("https://user:hunter2@api.x.com/v1")
        self.assertNotIn("hunter2", out)
        self.assertIn("user", out, "username is not a secret")

    def test_query_masked(self):
        out = logsafe.redact_url("https://x/o?token=abc123")
        self.assertNotIn("abc123", out)

    def test_otpauth_secret_masked(self):
        out = logsafe.redact_url(
            "otpauth://totp/Acme:me?secret=JBSWY3DPEHPK3PXP&issuer=Acme")
        self.assertNotIn("JBSWY3DPEHPK3PXP", out)
        self.assertIn("issuer=Acme", out)

    def test_empty(self):
        self.assertEqual(logsafe.redact_url(""), "")
        self.assertEqual(logsafe.redact_url(None), "")


class TestRedactPath(unittest.TestCase):
    def test_requestline_shape(self):
        out = logsafe.redact_path(
            "GET /api/symbols?password=SECRET HTTP/1.1")
        self.assertNotIn("SECRET", out)
        self.assertIn("/api/symbols", out)
        self.assertIn("HTTP/1.1", out, "protocol must survive for debugging")

    def test_debuggability_preserved(self):
        out = logsafe.redact_path("/api/screener?min_vol=0&max_spread_bps=100")
        self.assertIn("min_vol=0", out)
        self.assertIn("max_spread_bps=100", out)

    def test_free_text_is_also_scrubbed(self):
        """Not only well-formed paths -- this is the log-line sink."""
        out = logsafe.redact_path("password: supersecret")
        self.assertNotIn("supersecret", out)

    def test_url_in_text(self):
        out = logsafe.redact_path("fetch https://x/o?api_key=TOPSECRET failed")
        self.assertNotIn("TOPSECRET", out)

    def test_no_query(self):
        self.assertEqual(logsafe.redact_path("/api/status"), "/api/status")

    def test_empty(self):
        self.assertEqual(logsafe.redact_path(""), "")


class TestScrub(unittest.TestCase):
    def test_bearer_token(self):
        out = logsafe.scrub("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload")
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", out)

    def test_basic_token(self):
        out = logsafe.scrub("Authorization: Basic dXNlcjpwYXNzd29yZA==")
        self.assertNotIn("dXNlcjpwYXNzd29yZA", out)

    def test_userinfo(self):
        out = logsafe.scrub("connecting to https://u:pw@x.com/v1")
        self.assertNotIn("u:pw@", out)

    def test_otpauth(self):
        out = logsafe.scrub("otpauth://totp/A:b?secret=JBSWY3DPEHPK3PXP")
        self.assertNotIn("JBSWY3DPEHPK3PXP", out)

    def test_idempotent(self):
        once = logsafe.scrub("GET https://u:pw@x/o?api_key=SECRET&sym=AAPL")
        self.assertEqual(logsafe.scrub(once), once)

    def test_never_raises(self):
        for weird in (None, "", 123, object(), b"bytes", ["a"]):
            logsafe.scrub(weird)  # must not raise

    def test_harmless_text_unchanged(self):
        text = "GET /api/screener?min_vol=0 200 15ms"
        self.assertEqual(logsafe.scrub(text), text)


class TestScrubHeaders(unittest.TestCase):
    def test_sensitive_headers_masked(self):
        out = logsafe.scrub_headers({
            "Authorization": "Bearer SECRET", "Cookie": "session=abc",
            "Accept": "application/json"})
        self.assertNotIn("SECRET", str(out))
        self.assertNotIn("abc", str(out))
        self.assertEqual(out["Accept"], "application/json")

    def test_empty(self):
        self.assertEqual(logsafe.scrub_headers({}), {})
        self.assertEqual(logsafe.scrub_headers(None), {})


class TestSensitiveList(unittest.TestCase):
    def test_is_sensitive(self):
        self.assertTrue(logsafe.is_sensitive("Password"))
        self.assertTrue(logsafe.is_sensitive("API_KEY"))
        self.assertFalse(logsafe.is_sensitive("symbol"))
        self.assertFalse(logsafe.is_sensitive(""))

    def test_credential_shaped_names_are_covered(self):
        """Guard against a future param being added without masking."""
        for name in ("password", "token", "secret", "api_key", "code",
                     "authorization", "signature", "nonce", "session"):
            self.assertIn(name, logsafe.SENSITIVE_PARAMS, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
