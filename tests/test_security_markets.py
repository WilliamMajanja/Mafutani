"""Tests for the four carryover features.

Covers:
  * core.authz  — Throttle (brute-force lockout) + TOTP 2FA + recovery codes
  * core.markets — global market region config, session hours, venue mapping
  * core.provision — idempotent per-user provisioning (risk limits,
    watchlist, starter strategies)
  * core.devaccounts — the dev credential ledger (managed vs external)

The throttle/provision tests run against a real throwaway sqlite DB built
from data/schema.sql, so they exercise the same DDL the server uses.
"""
import json
import os
import sys
import tempfile
import time
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from core import authz, markets, provision  # noqa: E402


# ----------------------------------------------------------------- helpers
def fresh_db():
    """Build a throwaway DB from the real schema.

    Returns (conn, path) — sqlite3.Connection cannot carry attributes, so
    the path is returned separately for cleanup.
    """
    import sqlite3
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    with open(os.path.join(WORKDIR, "data", "schema.sql"), "r") as fh:
        conn.executescript(fh.read())
    conn.commit()
    return conn, path


def add_user(conn, email, tier="Free", pw_hash="x"):
    # users.tier references tiers(name), so register the tier first —
    # mirrors what an admin tier change would do.
    conn.execute("INSERT OR IGNORE INTO tiers (name) VALUES (?)", (tier,))
    cur = conn.execute(
        "INSERT INTO users (email, password_hash, tier) VALUES (?, ?, ?)",
        (email, pw_hash, tier))
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------- throttle
class TestThrottle(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            os.unlink(self.path)
        except Exception:
            pass

    def test_allows_up_to_max_attempts(self):
        t = authz.Throttle(self.conn, "email:a@b", max_attempts=3)
        self.assertEqual(t.check(), 0, "fresh key must not be locked")
        self.assertEqual(t.fail(), 0, "1st failure within allowance")
        self.assertEqual(t.fail(), 0, "2nd failure within allowance")

    def test_locks_out_after_allowance(self):
        t = authz.Throttle(self.conn, "email:a@b", max_attempts=3, base=30)
        for _ in range(3):
            t.fail()
        left = t.fail()          # 4th failure trips the lock
        self.assertGreater(left, 0, "must lock after exceeding allowance")
        self.assertTrue(t.blocked())

    def test_backoff_escalates_and_caps(self):
        t = authz.Throttle(self.conn, "email:a@b", max_attempts=2, base=30,
                           max_lock=120)
        t.fail(); t.fail()
        first = t.fail()
        second = t.fail()
        self.assertGreaterEqual(second, first, "backoff must escalate")
        self.assertLessEqual(second, 120, "backoff must respect max_lock")

    def test_success_clears_state(self):
        t = authz.Throttle(self.conn, "email:a@b", max_attempts=5)
        t.fail(); t.fail()
        self.assertEqual(len(self.conn.execute(
            "SELECT 1 FROM login_attempts WHERE key = ?",
            ("email:a@b",)).fetchall()), 2)
        t.ok()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM login_attempts").fetchone()[0], 0)
        self.assertFalse(t.blocked())

    def test_keys_are_scoped_per_email_and_ip(self):
        keys = authz.throttle_keys("A@B.com", "1.2.3.4")
        self.assertEqual(keys, ["email:a@b.com", "ip:1.2.3.4"])
        # Same ip, different identity: the ip key is shared, the email key
        # is not. Use the same policy on both Throttle objects, since the
        # policy is the caller's, not the persisted state's.
        def t(key):
            return authz.Throttle(self.conn, key, max_attempts=1, base=60)
        t("ip:1.2.3.4").fail()
        t("ip:1.2.3.4").fail()
        self.assertTrue(t("ip:1.2.3.4").blocked(),
                        "shared ip key must carry the lockout")
        self.assertFalse(t("ip:9.9.9.9").blocked(),
                         "a different ip is unaffected")
        self.assertFalse(t("email:other@x.com").blocked(),
                         "a different identity is unaffected")

    def test_history_is_capped(self):
        t = authz.Throttle(self.conn, "email:c@d", max_attempts=1000)
        for _ in range(authz.MAX_FAILURES_WINDOW + 15):
            t.fail()
        n = self.conn.execute(
            "SELECT COUNT(*) FROM login_attempts WHERE key = ?",
            ("email:c@d",)).fetchone()[0]
        self.assertLessEqual(n, authz.MAX_FAILURES_WINDOW,
                             "failure ledger must stay bounded")

    def test_stale_failures_are_pruned(self):
        t = authz.Throttle(self.conn, "email:e@f", max_attempts=2, base=60)
        t.fail(); t.fail(); t.fail()
        self.assertTrue(t.blocked())
        # age every row out of the window
        self.conn.execute(
            "UPDATE login_attempts SET ts = ? WHERE key = ?",
            (int(time.time()) - authz.WINDOW_SECONDS - 10, "email:e@f"))
        self.conn.commit()
        self.assertFalse(t.blocked(), "old failures must age out")


# -------------------------------------------------------------------- TOTP
class TestTotp(unittest.TestCase):
    def setUp(self):
        self.secret = authz.new_totp_secret()

    def test_secret_is_base32_and_distinct(self):
        self.assertTrue(all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
                            for c in self.secret))
        self.assertNotEqual(self.secret, authz.new_totp_secret())

    def test_code_is_six_digits_and_verifies(self):
        code = authz.totp_code(self.secret)
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())
        self.assertTrue(authz.verify_totp(self.secret, code))

    def test_rejects_wrong_code(self):
        code = authz.totp_code(self.secret)
        wrong = "000000" if code != "000000" else "111111"
        self.assertFalse(authz.verify_totp(self.secret, wrong))

    def test_tolerates_one_step_of_clock_drift(self):
        t = int(time.time())
        prev = authz.totp_code(self.secret, t - authz.TOTP_STEP)
        nxt = authz.totp_code(self.secret, t + authz.TOTP_STEP)
        self.assertTrue(authz.verify_totp(self.secret, prev), "prev step")
        self.assertTrue(authz.verify_totp(self.secret, nxt), "next step")

    def test_rejects_far_future_code(self):
        far = authz.totp_code(self.secret, time.time() + 3600)
        self.assertFalse(authz.verify_totp(self.secret, far))

    def test_empty_inputs_rejected(self):
        self.assertFalse(authz.verify_totp("", "123456"))
        self.assertFalse(authz.verify_totp(self.secret, ""))
        self.assertFalse(authz.verify_totp(self.secret, None))
        self.assertFalse(authz.verify_totp("not-base32!!!", "123456"))

    def test_otpauth_uri_wellformed(self):
        uri = authz.otpauth_url(self.secret, "a@b.com")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn("secret=" + self.secret, uri)
        self.assertIn("issuer=", uri)
        self.assertIn("period=" + str(authz.TOTP_STEP), uri)

    # -- recovery codes -------------------------------------------------
    def test_recovery_codes_single_use(self):
        codes = authz.new_recovery_codes(5)
        self.assertEqual(len(codes), 5)
        self.assertEqual(len(set(codes)), 5)
        stored = json.dumps([authz.hash_recovery_code(c) for c in codes])
        used = codes[0]
        matched, remaining = authz.verify_recovery_code(stored, used)
        self.assertTrue(matched)
        self.assertEqual(len(json.loads(remaining)), 4)
        # replaying the same code must now fail
        again, _ = authz.verify_recovery_code(remaining, used)
        self.assertFalse(again, "recovery codes must be single-use")

    def test_recovery_plaintext_not_stored(self):
        codes = authz.new_recovery_codes(3)
        stored = json.dumps([authz.hash_recovery_code(c) for c in codes])
        for c in codes:
            self.assertNotIn(c, stored, "plaintext recovery code leaked")


# ----------------------------------------------------------------- markets
class TestMarkets(unittest.TestCase):
    def test_regions_cover_requested_geography(self):
        self.assertEqual(set(markets.REGIONS), {"AMER", "EMEA", "APAC"})

    def test_aliases_resolve(self):
        for alias, want in (("europe", "EMEA"), ("eu", "EMEA"),
                            ("africa", "EMEA"), ("uk", "EMEA"),
                            ("asia", "APAC"), ("apac", "APAC"),
                            ("america", "AMER"), ("us", "AMER"),
                            ("jse", "EMEA"), ("middle-east", "EMEA")):
            self.assertEqual(markets.normalize_region(alias), want, alias)

    def test_normalize_rejects_garbage(self):
        for bad in ("", None, "  ", "atlantis", "123"):
            self.assertIsNone(markets.normalize_region(bad))

    def test_venue_mapping(self):
        self.assertEqual(markets.region_for_venue("NASDAQ"), "AMER")
        self.assertEqual(markets.region_for_venue("LME"), "EMEA")
        self.assertEqual(markets.region_for_venue("BINANCE"), "APAC")
        self.assertEqual(markets.region_for_venue("COINBASE"), "APAC")
        self.assertIsNone(markets.region_for_venue("NOWHERE", ""))

    def test_venue_falls_back_to_db_region(self):
        self.assertEqual(markets.region_for_venue("XYZ", "UK"), "EMEA")
        self.assertEqual(markets.region_for_venue("XYZ", "APAC"), "APAC")

    def test_every_configured_venue_maps_to_a_region(self):
        for code, r in markets.REGIONS.items():
            for v in r["venues"]:
                self.assertEqual(markets.VENUE_REGION[v], code, v)

    def test_session_hours_cover_expected_windows(self):
        # Americas: 13:30-20:00 UTC
        self.assertFalse(markets.is_open("AMER", 5))
        self.assertTrue(markets.is_open("AMER", 14))
        self.assertFalse(markets.is_open("AMER", 21))
        # EMEA: 08:00-16:30 UTC
        self.assertTrue(markets.is_open("EMEA", 9))
        self.assertFalse(markets.is_open("EMEA", 17))
        # APAC: 01:00-09:00 UTC (inside a single UTC day, no wrap)
        self.assertTrue(markets.is_open("APAC", 2))
        self.assertFalse(markets.is_open("APAC", 12))
        self.assertFalse(markets.is_open("APAC", 23.5))

    def test_session_that_wraps_past_utc_midnight(self):
        # A region whose window crosses midnight (e.g. 22:00-06:00 UTC)
        # must be open on both sides of the UTC day boundary.
        saved = markets.REGIONS["APAC"]
        markets.REGIONS["APAC"] = dict(saved, open_utc=22.0, close_utc=6.0)
        try:
            self.assertTrue(markets.is_open("APAC", 23), "before midnight")
            self.assertTrue(markets.is_open("APAC", 2), "after midnight")
            self.assertFalse(markets.is_open("APAC", 12), "midday closed")
        finally:
            markets.REGIONS["APAC"] = saved

    def test_is_open_unknown_region(self):
        self.assertIsNone(markets.is_open("ATLANTIS", 12))

    def test_market_hours_payload(self):
        h = markets.market_hours("AMER", 14)
        self.assertEqual(h["region"], "AMER")
        self.assertIs(h["open"], True)
        self.assertIn("tz", h)
        self.assertIsNone(markets.market_hours("ATLANTIS")["region"])

    def test_all_markets_payload_shape(self):
        ms = markets.all_markets()
        self.assertEqual(len(ms), 3)
        for m in ms:
            for k in ("code", "name", "tz", "open_utc", "close_utc",
                      "venues", "asset_classes"):
                self.assertIn(k, m)
            self.assertTrue(m["venues"], m["code"])


# -------------------------------------------------------------- provision
class TestProvision(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = fresh_db()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            os.unlink(self.path)
        except Exception:
            pass

    def test_creates_risk_limits_and_watchlist(self):
        uid = add_user(self.conn, "new@local")
        res = provision.provision(self.conn, uid, "Free")
        self.assertTrue(res["risk_limits"])
        self.assertTrue(res["watchlist"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM risk_limits WHERE user_id = ?",
            (uid,)).fetchone()[0], 1)
        wl = self.conn.execute(
            "SELECT symbols_json FROM watchlists WHERE user_id = ?",
            (uid,)).fetchone()[0]
        self.assertTrue(wl, "watchlist must not be empty")
        # every symbol in the starter basket must exist in the universe
        for s in json.loads(wl):
            self.assertIsNotNone(self.conn.execute(
                "SELECT 1 FROM symbols WHERE symbol = ?", (s,)).fetchone(), s)

    def test_idempotent(self):
        uid = add_user(self.conn, "twice@local")
        first = provision.provision(self.conn, uid, "Free")
        second = provision.provision(self.conn, uid, "Free")
        self.assertTrue(first["watchlist"])
        self.assertFalse(second["watchlist"], "second run must be a no-op")
        self.assertFalse(second["risk_limits"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM watchlists WHERE user_id = ?",
            (uid,)).fetchone()[0], 1, "must not duplicate the watchlist")

    def test_starter_strategies_exist_for_each_tier(self):
        for tier in ("Free", "Pro", "Institutional", "Admin"):
            wanted = provision.STARTER_STRATEGIES[tier]
            have = {r[0] for r in self.conn.execute(
                "SELECT name FROM strategies").fetchall()}
            missing = [w for w in wanted if w not in have]
            self.assertEqual(missing, [], "{} tier missing {}".format(
                tier, missing))

    def test_tier_above_free_gets_more_strategies(self):
        self.assertLess(len(provision.STARTER_STRATEGIES["Free"]),
                        len(provision.STARTER_STRATEGIES["Pro"]))
        self.assertLess(len(provision.STARTER_STRATEGIES["Pro"]),
                        len(provision.STARTER_STRATEGIES["Institutional"]))

    def test_unknown_tier_falls_back_to_free(self):
        uid = add_user(self.conn, "odd@local", tier="Platinum")
        res = provision.provision(self.conn, uid, "Platinum")
        self.assertIsNone(res.get("skipped"), res)
        self.assertTrue(res["watchlist"])

    def test_provision_all_backfills_everyone(self):
        for e in ("a@local", "b@local", "c@gmail.com"):
            add_user(self.conn, e)
        done = provision.provision_all(self.conn)
        self.assertEqual(len(done), 3, "all 3 accounts need provisioning")
        for uid in (1, 2, 3):
            self.assertIsNotNone(self.conn.execute(
                "SELECT 1 FROM risk_limits WHERE user_id = ?",
                (uid,)).fetchone())
            self.assertIsNotNone(self.conn.execute(
                "SELECT 1 FROM watchlists WHERE user_id = ?",
                (uid,)).fetchone())
        # second sweep is empty
        self.assertEqual(provision.provision_all(self.conn), [])

    def test_custom_watchlist_respected(self):
        uid = add_user(self.conn, "cust@local")
        provision.provision(self.conn, uid, "Free", watchlist=["AAPL", "BTCUSD"])
        wl = self.conn.execute(
            "SELECT symbols_json FROM watchlists WHERE user_id = ?",
            (uid,)).fetchone()[0]
        self.assertEqual(sorted(json.loads(wl)),
                         ["AAPL", "BTCUSD"])

    def test_unknown_symbols_dropped_from_watchlist(self):
        uid = add_user(self.conn, "ghost@local")
        provision.provision(self.conn, uid, "Free",
                            watchlist=["AAPL", "NOTREAL"])
        wl = json.loads(self.conn.execute(
            "SELECT symbols_json FROM watchlists WHERE user_id = ?",
            (uid,)).fetchone()[0])
        self.assertEqual(wl, ["AAPL"], "dead symbols must be filtered out")


# ------------------------------------------------------------ devaccounts
class TestDevAccounts(unittest.TestCase):
    def setUp(self):
        from core import devaccounts
        self.da = devaccounts

    def test_only_local_suffix_is_managed(self):
        self.assertTrue(self.da.is_managed("a@local"))
        self.assertTrue(self.da.is_managed("A@LOCAL"))
        self.assertFalse(self.da.is_managed("a@gmail.com"))
        self.assertFalse(self.da.is_managed("a@local.evil.com"))
        self.assertFalse(self.da.is_managed(""))

    def test_canonical_accounts_have_tier_and_password(self):
        for email, pw, tier in self.da.TEST_USERS:
            self.assertEqual(self.da.tier_for(email), tier, email)
            self.assertEqual(self.da.password_for(email), pw, email)

    def test_signup_accounts_get_deterministic_password(self):
        self.assertEqual(self.da.password_for("bob@local"), "dev-bob")
        self.assertIsNone(self.da.tier_for("bob@local"))

    def test_external_password_never_exposed(self):
        self.assertIsNone(self.da.password_for("real@gmail.com"),
                          "external accounts must have no known password")

    def test_ledger_marks_external_accounts(self):
        conn, path = fresh_db()
        try:
            add_user(conn, "admin@local", "Admin")
            add_user(conn, "real@gmail.com", "Free")
            text = "\n".join(self.da.format_ledger(conn))
            self.assertIn("admin@local", text)
            self.assertIn("donttrytoplaythefoolwithmeniggesh", text)
            self.assertIn("real@gmail.com", text)
            self.assertIn("not managed", text)
            self.assertNotIn("real@gmail.com:1234", text)
        finally:
            conn.close()
            os.unlink(path)

    def test_ledger_survives_broken_db(self):
        class Broken:
            def execute(self, *a, **k):
                raise RuntimeError("no db")
        lines = self.da.format_ledger(Broken())
        self.assertEqual(len(lines), 1)
        self.assertIn("unavailable", lines[0])

    def test_print_accounts_gate(self):
        os.environ.pop("PRINT_ACCOUNTS", None)
        os.environ.pop("APP_ENV", None)
        self.assertTrue(self.da.ledger_enabled())
        os.environ["PRINT_ACCOUNTS"] = "0"
        self.assertFalse(self.da.ledger_enabled())
        os.environ.pop("PRINT_ACCOUNTS")
        os.environ["APP_ENV"] = "production"
        self.assertFalse(self.da.ledger_enabled(),
                         "must not print credentials in production")
        os.environ.pop("APP_ENV")


if __name__ == "__main__":
    unittest.main(verbosity=2)
