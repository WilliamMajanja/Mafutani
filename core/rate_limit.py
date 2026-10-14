"""Simple in-memory rate limiter. Stdlib only.

Keyed by an opaque string, so the same class serves per-IP throttling and
per-user tier throttling. RFC-001 gives every tier an `api_rate`
(Free 60 / Pro 300 / Institutional 1200 / Admin 3600); that number was
advertised by `/api/me/entitlements` and read by nothing until the caller
started passing it to :meth:`RateLimiter.allowed`.

Why the limit can be per-call
-----------------------------
`max_per_min` is a *default*, not a constant. An authenticated request is
throttled against the limit its own tier grants, which differs per user, so
one shared bucket cannot express it. Anonymous traffic keeps a single fixed
limit and never reaches this path.

Memory
------
`self._hits` is keyed by whatever the caller passes. With IP keys that is
bounded by the number of distinct clients; with user ids it is one entry per
account that has ever made a request, and a long-lived process would grow
without limit. Keys whose timestamps have all fallen out of the window are
swept once the table passes :data:`MAX_KEYS`, so the working set is bounded
by the number of *active* keys rather than all keys ever seen.
"""

import threading
import time

WINDOW_SECONDS = 60

# Sweep threshold. Above this many keys, drop the ones with nothing left in
# the current window. Sized well above any plausible concurrent-user count
# so the sweep is rare, and well below "memory grows all day".
MAX_KEYS = 10000


class RateLimiter:
    def __init__(self, max_per_min=60):
        self.max_per_min = int(max_per_min)
        self._lock = threading.Lock()
        self._hits = {}

    def _sweep(self, now, window_start):
        """Drop keys with no hits inside the window. Caller holds the lock."""
        dead = [k for k, ts in self._hits.items()
                if not ts or ts[-1] <= window_start]
        for k in dead:
            del self._hits[k]

    def allowed(self, key, max_per_min=None):
        """Record a hit against `key`; False when over the limit.

        `max_per_min` overrides the instance default for this call only.
        A non-positive or unparseable limit falls back to the default rather
        than disabling the limiter -- a caller that passes a broken number
        must not accidentally remove the throttle.
        """
        limit = self.max_per_min
        if max_per_min is not None:
            try:
                cand = int(max_per_min)
                if cand > 0:
                    limit = cand
            except (TypeError, ValueError):
                pass
        now = time.time()
        k = str(key if key is not None else "unknown")
        with self._lock:
            window_start = now - WINDOW_SECONDS
            lst = [t for t in self._hits.get(k, []) if t > window_start]
            if len(lst) >= limit:
                self._hits[k] = lst
                return False
            lst.append(now)
            self._hits[k] = lst
            if len(self._hits) > MAX_KEYS:
                self._sweep(now, window_start)
            return True

    def reset(self, key=None):
        """Forget one key, or everything. Used by tests and by the
        admin 'disable this user' path, which should stop counting against
        a user immediately rather than at the end of the window."""
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(str(key), None)


_default = RateLimiter(60)


def allowed(ip, max_per_min=60):
    """Module-level convenience.

    Note: the `max_per_min` argument used to construct a *fresh* limiter on
    every call, which meant a caller asking for any limit other than 60 got
    an empty bucket and therefore no limiting at all. Nothing called it with
    a non-default value, so it was latent rather than live -- but it is the
    kind of bug that turns live the moment someone trusts it.
    """
    if int(max_per_min) == _default.max_per_min:
        return _default.allowed(ip)
    _default.max_per_min = int(max_per_min)
    return _default.allowed(ip)
