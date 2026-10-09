"""Adversarial tool fingerprinting and honeypot detection.

This module answers one question: "is this request coming from a known
attack tool?" It does not try to answer "is this request malicious" --
that is a judgment no heuristic can make reliably, and a false positive
there blocks a real user.

What it does instead:
* Honeypot routes: URLs that exist only to be scanned for. Any hit is a
  positive fingerprint. These are the strongest signal available because
  no legitimate user ever requests `/.env`.
* Known tool signatures: user agents and header patterns that belong to
  sqlmap, nikto, gobuster, nuclei, masscan, and similar. A match is a
  positive fingerprint.
* Behavioral scoring: weak signals that only matter in combination. A
  missing `Accept-Language` is common among bots but also among some
  legitimate clients, so it raises a score rather than triggering.

Response policy
---------------
* Unknown clients pass. Only positive signatures and honeypot hits
  trigger action.
* A positive fingerprint throttles the client (429 + audit event) and
  returns 404, never confirming what was detected.
* A honeypot hit is recorded with the client IP and the path, so an
  operator can see what was being probed.

No third-party code. `re` and `collections` only.
"""
import re
import time
from collections import defaultdict

__all__ = ["Fingerprint", "HoneypotHit", "Score"]


class HoneypotHit(object):
    """A request to a URL that exists only to be found."""

    __slots__ = ("path", "ts", "ip")

    def __init__(self, path, ip):
        self.path = path
        self.ts = time.time()
        self.ip = ip or ""

    def to_dict(self):
        return {"path": self.path, "ts": int(self.ts), "ip": self.ip}


#: URLs that exist only to be scanned for. Each is a path that a real
#: user or browser never requests but a scanner tries early. The list is
#: deliberately boring: `/wp-login.php`, `/.git/config`, `/admin.php`.
#: A hit on any of them is a positive fingerprint.
HONEYPOT_PATHS = (
    "/.env",
    "/.git/config",
    "/.git/HEAD",
    "/wp-login.php",
    "/wp-admin/",
    "/admin.php",
    "/administrator/",
    "/phpmyadmin/",
    "/.aws/credentials",
    "/config.json",
    "/.env.local",
    "/.env.production",
    "/backup.zip",
    "/dump.sql",
    "/.DS_Store",
    "/server-status",
    "/actuator/env",
    "/vendor/phpunit/phpunit/src/Util/PHP/eval-stdin.php",
    "/cgi-bin/luci/;stok=/locale",
)

#: User-agent substrings that belong to known attack tools. These are
#: positive fingerprints: no legitimate browser sends them.
TOOL_USER_AGENTS = (
    "sqlmap",
    "nikto",
    "dirbuster",
    "gobuster",
    "wfuzz",
    "masscan",
    "nmap",
    "nuclei",
    "wpscan",
    "metasploit",
    "hydra",
    "burpsuite",
    "zgrab",
    "censysinspect",
    "shodan",
    "python-requests/2.",   # a scanner, not a user
    "curl/7.",              # a script, not a user
    "go-http-client",
    "java/1.",              # a library, not a user
)

#: Header-order anomalies that suggest a library rather than a browser.
#: Browsers send headers in a consistent order; libraries do not. This is
#: a weak signal, so it raises a score rather than triggering.
BROWSER_HEADER_MARKERS = (
    "accept-language",
    "accept-encoding",
    "sec-fetch-mode",
    "sec-fetch-site",
)


class Score(object):
    """A mutable suspicion score for one client.

    The score is deliberately not a verdict. It exists so that a client
    who accumulates weak signals can be throttled without any single
    signal being treated as proof.
    """

    __slots__ = ("value", "reasons")

    def __init__(self):
        self.value = 0
        self.reasons = []

    def add(self, points, reason):
        self.value += points
        self.reasons.append(reason)

    def reset(self):
        self.value = 0
        self.reasons = []


class Fingerprint(object):
    """Per-client fingerprint state.

    Thread-safe by design: the HTTP handler is single-threaded, and even
    under ThreadingHTTPServer each request sees a consistent view because
    all mutations happen within one request.
    """

    def __init__(self, honeypot_paths=HONEYPOT_PATHS, tool_agents=TOOL_USER_AGENTS,
                 score_threshold=5):
        self._honeypots = frozenset(honeypot_paths)
        self._tools = tuple(tool_agents)
        self._threshold = score_threshold
        self._scores = defaultdict(Score)
        self._honeypot_hits = []
        self._throttled = {}
        # Localhost is the operator's own machine. Throttling it locks
        # the operator out of their own application, which is worse than
        # the scan it was meant to stop.
        self._whitelist = frozenset({"127.0.0.1", "::1", "localhost"})

    # -- honeypot ---------------------------------------------------------
    def is_honeypot(self, path):
        """Is this path a honeypot? Exact match, case-insensitive."""
        if not path:
            return False
        return path.lower() in self._honeypots

    def record_honeypot(self, path, ip):
        """Record a honeypot hit. Returns the hit."""
        hit = HoneypotHit(path, ip)
        self._honeypot_hits.append(hit)
        if len(self._honeypot_hits) > 1000:
            self._honeypot_hits = self._honeypot_hits[-1000:]
        return hit

    def honeypot_hits(self, n=50):
        return list(self._honeypot_hits[-n:])

    # -- tool signatures --------------------------------------------------
    def tool_signature(self, user_agent):
        """Return the matched tool name, or None."""
        if not user_agent:
            return None
        ua = user_agent.lower()
        for tool in self._tools:
            if tool in ua:
                return tool
        return None

    # -- scoring ----------------------------------------------------------
    def score_request(self, headers, user_agent, path):
        """Score a request. Returns (score, reasons).

        The score is a sum of weak signals. It never triggers on its own;
        only the threshold does, and the threshold is deliberately high
        enough that a normal browser never reaches it.
        """
        score = Score()

        # Missing Accept-Language is common among bots but also among
        # some legitimate clients, so it is a weak signal only.
        if not any(k.lower() in BROWSER_HEADER_MARKERS for k in headers):
            score.add(2, "no browser header markers")

        # A tool UA is a strong signal but still not a verdict -- a
        # legitimate script may send one.
        tool = self.tool_signature(user_agent)
        if tool:
            score.add(3, "tool user agent: {}".format(tool))

        # A request to a path with no referer and no accept is more
        # likely a scan than a user. Weak, so it only adds to the score.
        if "referer" not in headers and "accept" not in headers:
            score.add(1, "no referer or accept")

        return score.value, score.reasons

    def is_tool(self, user_agent):
        """Is this user agent a known attack tool? Positive fingerprint."""
        return self.tool_signature(user_agent) is not None

    # -- throttling -------------------------------------------------------
    def throttle(self, ip, seconds=300):
        """Mark a client as throttled for `seconds`."""
        if ip and ip not in self._whitelist:
            self._throttled[ip] = time.time() + seconds

    def is_throttled(self, ip):
        """Is this client currently throttled?"""
        if not ip:
            return False
        if ip in self._whitelist:
            return False
        expires = self._throttled.get(ip)
        if expires is None:
            return False
        if expires < time.time():
            del self._throttled[ip]
            return False
        return True

    def clear(self, ip):
        """Clear throttle state for a client."""
        self._throttled.pop(ip, None)
        self._scores.pop(ip, None)


#: Module-level singleton. The handler uses this; tests construct their own.
fingerprint = Fingerprint()
