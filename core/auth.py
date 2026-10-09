"""Auth helpers: pbkdf2 hashing, sessions, CSRF. Stdlib only, import-safe.

All DB functions take a sqlite3.Connection (Row factory) so this module
never imports server or db layers.
"""
import hashlib
import hmac
import secrets
import time

HASH_ITERATIONS = 200_000
SESSION_TTL_SECONDS = 86400  # 24h


def pbkdf2_hash(password):
    """Hash password -> 'pbkdf2_sha256$iterations$salt_hex$hash_hex'."""
    if not isinstance(password, str):
        raise ValueError("password must be str")
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, HASH_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(HASH_ITERATIONS, salt.hex(), dk.hex())


def verify(password, stored):
    """Constant-time verify of password against stored hash string."""
    try:
        if not isinstance(password, str) or not isinstance(stored, str):
            return False
        parts = stored.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            return False
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def new_csrf_token():
    return secrets.token_urlsafe(32)


def create_session(conn, user_id, ttl_seconds=SESSION_TTL_SECONDS):
    """Insert session row, return (token, csrf_token, expires_at)."""
    token = secrets.token_urlsafe(32)
    csrf = new_csrf_token()
    now = int(time.time())
    expires_at = now + int(ttl_seconds)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at, csrf_token)"
        " VALUES (?, ?, ?, ?, ?)",
        (token, int(user_id), now, expires_at, csrf),
    )
    conn.commit()
    return token, csrf, expires_at


def get_session_user(conn, token):
    """Return user Row for valid non-expired session token, else None."""
    if not token:
        return None
    now = int(time.time())
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token = ? AND s.expires_at > ? AND u.disabled = 0",
        (token, now),
    ).fetchone()
    return row


def get_session(conn, token):
    if not token:
        return None
    return conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()


def destroy_session(conn, token):
    if not token:
        return
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()


def verify_csrf(session_csrf, provided):
    if not session_csrf or not provided:
        return False
    try:
        return hmac.compare_digest(str(session_csrf), str(provided))
    except Exception:
        return False
