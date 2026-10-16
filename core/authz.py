"""Login security: brute-force throttling + TOTP (RFC 6238) 2FA.

Zero-dependency (hmac/hashlib/base64/struct/time). Pure stdlib.

Two independent controls:

1. `Throttle` — per-identity + per-IP sliding-window rate limiting with
   exponential lockout. State lives in a `login_attempts` table so it
   survives restarts and is shared across worker processes.

2. TOTP — RFC 6238 time-based one-time passwords (SHA1, 6 digits, 30s
   step, +/-1 window) for real 2FA, plus a recovery-code set hashed at
   rest. Secrets never leave the server in plaintext: the provisioning
   flow returns the secret exactly once, over the same channel as the
   password.
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time

# ------------------------------------------------------------------ throttle
MAX_ATTEMPTS = 5            # failures tolerated inside the window
WINDOW_SECONDS = 300        # 5-minute sliding window
BASE_LOCKOUT = 30           # seconds; doubles per extra breach
MAX_LOCKOUT = 900           # cap the backoff at 15 minutes
MAX_FAILURES_WINDOW = 20    # keep the ledger small


class Throttle:
    """Sliding-window throttle with exponential lockout.

    Each `fail()` records a failure and returns the lockout seconds in
    force (0 when still allowed). `check()` returns the remaining
    lockout without recording anything. `ok(identity, ip)` is the happy
    path: clears state on success.
    """

    def __init__(self, conn, key, max_attempts=MAX_ATTEMPTS,
                 window=WINDOW_SECONDS, base=BASE_LOCKOUT,
                 max_lock=MAX_LOCKOUT):
        self.conn = conn
        self.key = key
        self.max_attempts = int(max_attempts)
        self.window = int(window)
        self.base = int(base)
        self.max_lock = int(max_lock)

    def _now(self):
        return int(time.time())

    def _prune(self, now):
        self.conn.execute(
            "DELETE FROM login_attempts WHERE key = ? AND ts < ?",
            (self.key, now - self.window))

    def _rows(self, now):
        self._prune(now)
        return self.conn.execute(
            "SELECT ts FROM login_attempts WHERE key = ? ORDER BY ts",
            (self.key,)).fetchall()

    def _lockout_for(self, rows, now):
        # `max_attempts` failures are tolerated; the next one locks out.
        if len(rows) <= self.max_attempts:
            return 0
        over = len(rows) - self.max_attempts
        # attempts beyond the allowance escalate: 30s, 60s, 120s ...
        secs = min(self.base * (2 ** over), self.max_lock)
        # The lock is measured from the oldest surviving failure, so it
        # cannot outlive the evidence that caused it.
        age = now - int(rows[0][0])
        if secs < age:
            return 0
        return secs - age

    def check(self):
        """Seconds of lockout remaining (0 if the caller may try)."""
        return self._lockout_for(self._rows(self._now()), self._now())

    def blocked(self):
        return self.check() > 0

    def fail(self):
        now = self._now()
        self.conn.execute(
            "INSERT INTO login_attempts (key, ts) VALUES (?, ?)",
            (self.key, now))
        # Cap history growth per key. Rank by rowid (unique) rather than ts:
        # several failures inside the same second share a ts, which makes a
        # `ts NOT IN (SELECT ts ...)` prune either delete everything or
        # nothing.
        self.conn.execute(
            "DELETE FROM login_attempts WHERE key = ? AND rowid NOT IN"
            " (SELECT rowid FROM login_attempts WHERE key = ?"
            "  ORDER BY ts DESC, rowid DESC LIMIT ?)",
            (self.key, self.key, MAX_FAILURES_WINDOW))
        self.conn.commit()
        return self._lockout_for(self._rows(now), now)

    def ok(self):
        self.conn.execute("DELETE FROM login_attempts WHERE key = ?",
                          (self.key,))
        self.conn.commit()


def throttle_keys(email, ip):
    """Identity-scoped and IP-scoped keys for a login attempt."""
    e = (email or "").strip().lower() or "-"
    i = (ip or "-")[:64]
    return ["email:{}".format(e), "ip:{}".format(i)]


# ---------------------------------------------------------------------- TOTP
TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1          # accept one step either side for clock drift
TOTP_PERIOD = 90         # seconds a generated code stays valid in-app


def _b32_secret(nbytes=20):
    raw = secrets.token_bytes(nbytes)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def new_totp_secret():
    """Base32 secret for provisioning (returned once, never stored raw)."""
    return _b32_secret()


def _decode_secret(secret):
    s = str(secret or "").strip().replace(" ", "").upper()
    s += "=" * ((8 - len(s) % 8) % 8)
    try:
        return base64.b32decode(s, casefold=True)
    except Exception:
        return b""


def totp_code(secret, when=None, step=TOTP_STEP, digits=TOTP_DIGITS):
    """RFC 6238 code for `secret` at time `when` (default now)."""
    key = _decode_secret(secret)
    if not key:
        return ""
    counter = int((time.time() if when is None else when) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def verify_totp(secret, provided, when=None, window=TOTP_WINDOW):
    """Constant-time check of a submitted code within +/- `window` steps."""
    want = str(provided or "").strip().replace(" ", "")
    if not want or not _decode_secret(secret):
        return False
    when = time.time() if when is None else when
    for drift in range(-int(window), int(window) + 1):
        # compare all candidates to avoid early-exit timing leaks
        if hmac.compare_digest(totp_code(secret, when + drift * TOTP_STEP),
                               want):
            return True
    return False


def new_recovery_codes(n=8):
    """Human-typable single-use recovery codes (returned once)."""
    return ["{}-{}".format(secrets.token_hex(2), secrets.token_hex(2)).upper()
            for _ in range(n)]


def hash_recovery_code(code):
    return hashlib.sha256(str(code or "").strip().upper().encode()).hexdigest()


def verify_recovery_code(stored_json, provided):
    """Constant-time lookup of `provided` in a JSON list of hashes.

    Returns ``(matched, remaining_json)`` on every path, including
    malformed input -- callers unpack the result unconditionally, so a
    bare bool here would raise TypeError inside the login handler.
    """
    import json
    try:
        hashes = json.loads(stored_json or "[]")
    except Exception:
        return False, stored_json or "[]"
    if not isinstance(hashes, list):
        return False, stored_json or "[]"
    want = hash_recovery_code(provided)
    ok = False
    for h in hashes:  # no early exit
        if hmac.compare_digest(str(h), want):
            ok = True
    if not ok:
        return False, stored_json or "[]"
    try:
        remaining = [h for h in hashes
                     if not hmac.compare_digest(str(h), want)]
        return True, json.dumps(remaining)
    except Exception:
        return True, "[]"


def otpauth_url(secret, email, issuer="Mafutani CTA"):
    """otpauth:// URI for authenticator apps (shown once at setup)."""
    import urllib.parse
    label = urllib.parse.quote("{}:{}".format(issuer, email))
    return "otpauth://totp/{}?secret={}&issuer={}&algorithm=SHA1&digits={}&period={}".format(
        label, secret, urllib.parse.quote(issuer), TOTP_DIGITS, TOTP_STEP)
