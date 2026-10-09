"""Simple in-memory per-IP rate limiter. Stdlib only."""

import threading
import time

WINDOW_SECONDS = 60


class RateLimiter:
    def __init__(self, max_per_min=60):
        self.max_per_min = int(max_per_min)
        self._lock = threading.Lock()
        self._hits = {}

    def allowed(self, ip):
        now = time.time()
        key = str(ip or "unknown")
        with self._lock:
            window_start = now - WINDOW_SECONDS
            lst = [t for t in self._hits.get(key, []) if t > window_start]
            if len(lst) >= self.max_per_min:
                self._hits[key] = lst
                return False
            lst.append(now)
            self._hits[key] = lst
            return True


_default = RateLimiter(60)


def allowed(ip, max_per_min=60):
    if int(max_per_min) == _default.max_per_min:
        return _default.allowed(ip)
    return RateLimiter(int(max_per_min)).allowed(ip)
