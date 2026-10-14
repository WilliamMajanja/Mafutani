"""RFC-001 api_rate enforcement.

`api_rate` was in ENTITLEMENTS for every tier from the first commit and read
by nothing. `/api/me/entitlements` told a Free user they had 60 requests a
minute; the server applied a flat 600 to every caller regardless of tier.

The shape of the fix matters more than the numbers, so these pin three
properties:

* the **anonymous** cap is untouched at 600 -- this is the abuse control,
  and tier-awareness must not become a way to raise it;
* an authenticated caller is bounded by **their own tier**, so Free is
  *lowered* (600 -> 60) and Institutional is *not clipped* (no longer stuck
  at 600 when entitled to 1200);
* a per-user bucket alone would be unbounded per IP, so a backstop keeps
  one address from multiplying its allowance by opening sessions.

The tier wiring is exercised through `_enforce_rate` with a stubbed session
resolver, because driving a real HTTP request at 60 req/min to observe a
429 would make the suite both slow and flaky.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.rate_limit import RateLimiter, MAX_KEYS  # noqa: E402
from core.tiers import ENTITLEMENTS  # noqa: E402


class TestRateLimiterPerCallLimit(unittest.TestCase):
    def setUp(self):
        self.lim = RateLimiter(600)

    def test_default_limit_applies(self):
        for _ in range(600):
            self.assertTrue(self.lim.allowed("ip"))
        self.assertFalse(self.lim.allowed("ip"))

    def test_per_call_limit_overrides(self):
        for _ in range(60):
            self.assertTrue(self.lim.allowed("u:1", 60))
        self.assertFalse(self.lim.allowed("u:1", 60),
                         "60/min not enforced for this key")

    def test_keys_are_independent(self):
        for _ in range(60):
            self.lim.allowed("u:1", 60)
        self.assertFalse(self.lim.allowed("u:1", 60))
        self.assertTrue(self.lim.allowed("u:2", 60),
                        "one user's limit leaked onto another")

    def test_override_does_not_mutate_the_instance_default(self):
        """A per-user bucket must not permanently retune the shared one."""
        self.lim.allowed("u:1", 60)
        self.assertEqual(self.lim.max_per_min, 600)

    def test_broken_limit_falls_back_rather_than_disabling(self):
        """A caller passing nonsense must not remove the throttle."""
        for _ in range(600):
            self.lim.allowed("u:1", "not-a-number")
        self.assertFalse(self.lim.allowed("u:1", "not-a-number"))

    def test_zero_and_negative_fall_back(self):
        """A broken limit must fall back to the default, not refuse everything.

        Note the direction of the assertion. `len(lst) >= limit` with a
        negative limit is true immediately, so dropping the `cand > 0` guard
        does not remove the throttle -- it makes the limiter reject *every*
        request. An earlier version of this test asserted that a later call
        was refused, which that mutant satisfies perfectly; it has to assert
        that the early calls are allowed.
        """
        for bad in (0, -1, -600):
            self.lim.reset()
            self.assertTrue(self.lim.allowed("u:1", bad),
                            "limit %r refused the very first request" % bad)
            self.assertTrue(self.lim.allowed("u:1", bad))
            allowed = sum(1 for _ in range(600)
                          if self.lim.allowed("u:1", bad))
            self.assertEqual(allowed, 598,
                             "limit %r fell back to something other than the "
                             "600 default (allowed %d)" % (bad, allowed))
            self.assertFalse(self.lim.allowed("u:1", bad))

    def test_none_key_is_safe(self):
        self.assertTrue(self.lim.allowed(None))

    def test_reset_clears_one_key(self):
        for _ in range(600):
            self.lim.allowed("u:1")
        self.lim.reset("u:1")
        self.assertTrue(self.lim.allowed("u:1"))

    def test_reset_all(self):
        for _ in range(600):
            self.lim.allowed("u:1")
        self.lim.reset()
        self.assertTrue(self.lim.allowed("u:1"))


class TestMemoryIsBounded(unittest.TestCase):
    """`self._hits` is now keyed by user id, not just IP.

    With one entry per account that has ever made a request, an unpruned
    dict is a slow leak in a long-lived process. The sweep must actually
    reclaim, not just run.
    """

    def test_expired_keys_are_swept(self):
        lim = RateLimiter(600)
        lim._hits["u:stale"] = [0.0]          # long outside the window
        for i in range(MAX_KEYS + 10):
            lim.allowed("u:%d" % i)
        self.assertNotIn("u:stale", lim._hits,
                         "an expired key survived the sweep")

    def test_active_keys_are_kept(self):
        """Sweeping must not drop keys that are still inside the window,
        or the sweep would hand every over-limit user a fresh allowance."""
        import time
        lim = RateLimiter(600)
        now = time.time()
        for i in range(MAX_KEYS + 10):
            lim.allowed("u:%d" % i)
        lim._hits["u:live"] = [now]
        lim.allowed("u:trigger")
        self.assertIn("u:live", lim._hits)

    def test_stale_bulk_is_reclaimed(self):
        """The table may exceed MAX_KEYS while keys are live -- that is the
        documented behaviour, since the sweep can only reclaim keys that
        have aged out. What must hold is that a burst of expired keys is
        actually released rather than accumulating forever."""
        lim = RateLimiter(600)
        for i in range(MAX_KEYS + 500):
            lim._hits["u:%d" % i] = [0.0]        # all long expired
        self.assertGreater(len(lim._hits), MAX_KEYS)
        lim.allowed("u:trigger")                  # pushes past MAX_KEYS, sweeps
        self.assertLessEqual(len(lim._hits), 2,
                             "expired keys were not reclaimed: %d left"
                             % len(lim._hits))


class TestAnonymousCapUnchanged(unittest.TestCase):
    def test_anonymous_limiter_is_still_600(self):
        """The abuse control must not move as a side effect of tier work.

        Anchored to the start of a line on purpose. A bare
        `assertIn("_limiter = RateLimiter(600)")` also matches
        `_auth_user_limiter = RateLimiter(600)` and
        `_auth_ip_limiter = RateLimiter(600)`, so it stayed green when the
        anonymous cap was raised to 6000 -- the assertion was matching the
        two lines added by the very change it was meant to police. Same
        shape as the `SELECT[^"]*FROM` regex that matched nothing.
        """
        import re
        with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
            src = f.read()
        anon = re.findall(r"(?m)^\s*_limiter = RateLimiter\((\d+)\)", src)
        self.assertEqual(anon, ["600"],
                         "anonymous cap is %s, expected exactly one 600" % anon)

    def test_the_authenticated_buckets_are_separate_objects(self):
        """Three buckets sharing one object would let the anonymous cap and
        a tier allowance overwrite each other's counts."""
        import app as appmod
        self.assertIsNot(appmod._limiter, appmod._auth_user_limiter)
        self.assertIsNot(appmod._limiter, appmod._auth_ip_limiter)
        self.assertIsNot(appmod._auth_user_limiter, appmod._auth_ip_limiter)

    def test_authenticated_path_does_not_use_the_anonymous_bucket(self):
        import re
        with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
            src = f.read()
        body = src[src.index("def _enforce_rate"):src.index("def _dispatch")]
        # `_auth_user_limiter.allowed` contains "_limiter.allowed" as a
        # substring, so this needs a boundary -- an earlier version of this
        # test matched the authenticated branch and failed for that reason.
        bare = re.compile(r"(?<![\w])_limiter\.allowed")
        after = body[body.index("# Authenticated"):]
        self.assertIsNone(bare.search(after),
                          "the anonymous bucket is reachable when authenticated")
        self.assertTrue(bare.search(body[:body.index("# Authenticated")]),
                        "anonymous traffic no longer uses the 600 cap")


class TestTierWiring(unittest.TestCase):
    """_enforce_rate, with the session resolver stubbed."""

    def setUp(self):
        import app as appmod
        self.appmod = appmod
        self.Handler = appmod.Handler

    def _handler(self, ip="10.0.0.1"):
        h = self.Handler.__new__(self.Handler)
        h.headers = {}
        h.path = "/api/status"
        h.command = "GET"
        h._fake_ip = ip
        return h

    def _run(self, tier, times, ip="10.0.0.1"):
        """Fire `times` requests for a user on `tier`; return how many passed."""
        appmod = self.appmod
        rate = int(ENTITLEMENTS[tier]["api_rate"])
        appmod._auth_user_limiter.reset()
        appmod._auth_ip_limiter.reset()
        allowed = 0
        for _ in range(times):
            if self.Handler._enforce_rate(self._handler(ip), 7, rate):
                allowed += 1
        return allowed

    def test_free_is_held_to_sixty(self):
        """The bug this fixes: Free was getting the anonymous 600."""
        self.assertEqual(ENTITLEMENTS["Free"]["api_rate"], 60)
        self.assertEqual(self._run("Free", 80), 60)

    def test_pro_gets_three_hundred(self):
        self.assertEqual(ENTITLEMENTS["Pro"]["api_rate"], 300)
        self.assertEqual(self._run("Pro", 320), 300)

    def test_free_is_stricter_than_before(self):
        self.assertLess(ENTITLEMENTS["Free"]["api_rate"], 600)

    def test_institutional_is_not_clipped_at_six_hundred(self):
        """Was pinned to 600 by the anonymous cap despite 1200 entitled."""
        self.assertGreater(ENTITLEMENTS["Institutional"]["api_rate"], 600)
        allowed = self._run("Institutional", 700)
        self.assertGreater(allowed, 600,
                           "Institutional is still clipped at the old 600")

    def test_tiers_are_ordered_by_allowance(self):
        rates = [ENTITLEMENTS[t]["api_rate"]
                 for t in ("Free", "Pro", "Institutional", "Admin")]
        self.assertEqual(rates, sorted(rates))


class TestIpBackstop(unittest.TestCase):
    """Per-user buckets must not become unbounded per-IP."""

    def setUp(self):
        import app as appmod
        self.appmod = appmod
        self.Handler = appmod.Handler

    def test_backstop_constants_exist(self):
        self.assertGreaterEqual(self.appmod.AUTH_IP_BACKSTOP_MULTIPLE, 2)
        self.assertGreaterEqual(self.appmod.AUTH_IP_BACKSTOP_FLOOR, 600)

    def test_backstop_is_the_tighter_of_floor_and_multiple(self):
        for tier, ent in ENTITLEMENTS.items():
            rate = ent["api_rate"]
            backstop = max(self.appmod.AUTH_IP_BACKSTOP_FLOOR,
                           rate * self.appmod.AUTH_IP_BACKSTOP_MULTIPLE)
            self.assertGreaterEqual(backstop, rate,
                                    "%s backstop is below its own tier rate" % tier)

    def test_many_users_from_one_ip_still_hit_a_ceiling(self):
        appmod = self.appmod
        appmod._auth_user_limiter.reset()
        appmod._auth_ip_limiter.reset()
        h = self.Handler.__new__(self.Handler)
        h.headers = {}
        h.path = "/api/status"
        h.command = "GET"
        h._fake_ip = "10.0.0.9"
        rate = 60
        backstop = max(appmod.AUTH_IP_BACKSTOP_FLOOR,
                       rate * appmod.AUTH_IP_BACKSTOP_MULTIPLE)
        allowed = 0
        # 60 distinct users, each well within their own 60/min allowance.
        for uid in range(60):
            for _ in range(60):
                if self.Handler._enforce_rate(h, uid, rate):
                    allowed += 1
        self.assertLessEqual(allowed, backstop,
                             "one IP exceeded its backstop via many users")
        self.assertGreater(allowed, 600,
                           "backstop binds before the old 600 cap even for 60 users")


class TestSessionTierResolution(unittest.TestCase):
    """The resolver must degrade to anonymous, never raise.

    It runs ahead of every request. An exception escaping here would turn a
    database hiccup into a 500 on every endpoint, and 'fail open to the
    anonymous cap' is the only safe direction -- the alternative is failing
    open to *no* limit at all.
    """

    def _bare_handler(self):
        import app as appmod
        h = appmod.Handler.__new__(appmod.Handler)
        h.headers = {}
        h.path = "/api/status"
        h.command = "GET"
        return h

    def test_missing_request_context_degrades(self):
        # No cookies, no rfile, no valid handler state: every lookup inside
        # must fail softly and return the anonymous pair.
        self.assertEqual(self._bare_handler()._session_tier(), (None, None))

    def test_repeated_calls_are_stable(self):
        h = self._bare_handler()
        for _ in range(3):
            self.assertEqual(h._session_tier(), (None, None))


class TestDispatchWiring(unittest.TestCase):
    """_dispatch must actually resolve the tier and apply the limit.

    Every other test here calls _enforce_rate directly, which is exactly why
    breaking the *call site* in _dispatch went unnoticed: stub the resolver
    to always return anonymous and every tier test still passed. These drive
    _dispatch itself.
    """

    def _handler(self):
        import app as appmod
        h = appmod.Handler.__new__(appmod.Handler)
        h.headers = {}
        h.path = "/api/status"
        h.command = "GET"
        h._fake_ip = "10.0.0.1"
        self.sent = []
        h._send_json = lambda payload, status=200: self.sent.append(
            (status, payload))
        return h

    def test_dispatch_resolves_the_session_tier(self):
        h = self._handler()
        seen = []
        h._session_tier = lambda: (7, 60)
        h._enforce_rate = lambda uid, rate: seen.append((uid, rate)) or True
        h._dispatch("GET")
        self.assertIn((7, 60), seen,
                      "_dispatch did not pass the resolved tier through")

    def test_dispatch_blocks_when_the_limit_is_exceeded(self):
        h = self._handler()
        h._session_tier = lambda: (7, 60)
        h._enforce_rate = lambda uid, rate: False
        h._dispatch("GET")
        self.assertTrue(self.sent, "an over-limit request was not blocked")
        status, payload = self.sent[0]
        self.assertEqual(status, 429)
        self.assertEqual(payload.get("error"), "rate limited")

    def test_blocked_response_names_the_applied_limit(self):
        """A client told only 'rate limited' has to guess whether it hit its
        tier allowance or the anonymous cap -- the two need different fixes."""
        h = self._handler()
        h._session_tier = lambda: (7, 60)
        h._enforce_rate = lambda uid, rate: False
        h._dispatch("GET")
        self.assertEqual(self.sent[0][1].get("limit_per_min"), 60)

    def test_anonymous_block_omits_the_tier_limit(self):
        """There is no tier to name, so the key must be absent rather than
        null -- a UI rendering `null` as a limit is worse than omitting it."""
        h = self._handler()
        h._session_tier = lambda: (None, None)
        h._enforce_rate = lambda uid, rate: False
        h._dispatch("GET")
        self.assertNotIn("limit_per_min", self.sent[0][1])

    def test_allowed_requests_are_not_blocked(self):
        h = self._handler()
        h._session_tier = lambda: (7, 60)
        h._enforce_rate = lambda uid, rate: True
        h._dispatch("GET")
        self.assertFalse(self.sent and self.sent[0][0] == 429,
                         "an allowed request was rate limited")


class TestAdvertisedMatchesEnforced(unittest.TestCase):
    """RFC-001 acceptance: one map, so the UI cannot start lying again."""

    def test_entitlements_api_rate_matches_table(self):
        from core.tiers import get_entitlements
        for tier in ENTITLEMENTS:
            self.assertEqual(get_entitlements(tier)["api_rate"],
                             ENTITLEMENTS[tier]["api_rate"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
