"""Auth + tier-gate tests. Imports real core.auth / core.tiers (verified present)."""
import os
import sqlite3
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from core import auth, tiers  # noqa: E402


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    with open(os.path.join(WORKDIR, "data", "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    return conn


def signup(conn, email="u@example.com", password="s3cret-pw", tier="Free"):
    pw_hash = auth.pbkdf2_hash(password)
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, tier) VALUES (?, ?, ?)",
        (email, pw_hash, tier),
    )
    conn.commit()
    return cur.lastrowid


class TestAuth(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()

    def tearDown(self):
        self.conn.close()

    def test_signup_defaults_free_tier(self):
        uid = signup(self.conn)
        row = self.conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        self.assertEqual(row["tier"], "Free")

    def test_login_verify_ok_and_bad_password(self):
        signup(self.conn, password="correct-horse")
        row = self.conn.execute(
            "SELECT * FROM users WHERE email = ?", ("u@example.com",)
        ).fetchone()
        self.assertTrue(auth.verify("correct-horse", row["password_hash"]))
        self.assertFalse(auth.verify("wrong-pw", row["password_hash"]))

    def test_tier_gate_free_cannot_use_pro_strategy(self):
        self.assertTrue(tiers.can_use_strategy("Free", "ema_trend"))
        self.assertFalse(tiers.can_use_strategy("Free", "vwap_pullback"))
        with self.assertRaises(PermissionError):
            tiers.require_strategy("Free", "vwap_pullback")

    def test_tier_gate_free_cannot_go_live(self):
        with self.assertRaises(PermissionError):
            tiers.require_live("Free")
        self.assertTrue(tiers.can_use_live("Pro"))

    def test_session_lifecycle_and_expiry(self):
        uid = signup(self.conn)
        token, csrf, _exp = auth.create_session(self.conn, uid)
        self.assertIsNotNone(token)
        user = auth.get_session_user(self.conn, token)
        self.assertIsNotNone(user)
        self.assertEqual(user["id"], uid)
        # Expired session must not resolve.
        tok2, _, _ = auth.create_session(self.conn, uid, ttl_seconds=-1)
        self.assertIsNone(auth.get_session_user(self.conn, tok2))
        # Logout destroys session.
        auth.destroy_session(self.conn, token)
        self.assertIsNone(auth.get_session_user(self.conn, token))

    def test_csrf_verify(self):
        self.assertTrue(auth.verify_csrf("abc", "abc"))
        self.assertFalse(auth.verify_csrf("abc", "xyz"))
        self.assertFalse(auth.verify_csrf("", "xyz"))


if __name__ == "__main__":
    unittest.main()
