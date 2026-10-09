"""Log sanitisation: keep secrets out of log files.

The stdlib's `BaseHTTPRequestHandler.log_request` writes the *entire*
request line verbatim, which means any credential that ever appears in a
query string -- from our own code, a browser prefetch, a proxy, a curl
mistake, or a bookmarked URL -- is persisted to disk in cleartext. This
module is the single choke point that prevents that.

Rules:
  * Sensitive parameter *values* are replaced with `***`. Parameter
    *names* are kept, because "someone tried to authenticate with a
    token param" is useful signal and not itself a secret.
  * Redaction is applied on a word boundary so `password` is caught but
    `passwordless` is not, and case-insensitively so `PassWord=` is
    caught.
  * `scrub()` is idempotent, so it is safe to apply to a line that has
    already been through it.
  * It never raises: a logging path must not be able to kill a request.
"""

import re

# Parameter names whose values must never reach a log file.
SENSITIVE_PARAMS = frozenset([
    "password", "passwd", "pwd", "pass", "pw",
    "token", "access_token", "refresh_token", "id_token", "session",
    "secret", "client_secret", "api_key", "apikey", "key",
    "csrf", "csrf_token", "xsrf",
    "code", "otp", "totp", "mfa",
    "authorization", "auth",
    "signature", "sig", "nonce",
    "email", "user", "username", "login",
])

MASK = "***"

# key=value / key:value pairs, value quoted or bare, terminated by a
# delimiter.
#
# The whole pattern sits inside a zero-width LOOKAHEAD on purpose. A plain
# consuming match would let a non-sensitive key swallow a sensitive one:
# in `https://host/x?api_key=SECRET`, `https` matches first, is judged
# non-sensitive, and `re.sub` then resumes *after* the entire URL -- so
# `api_key` is never examined and the secret is logged in cleartext. A
# lookahead is scanned at every offset, so nested candidates are always
# found. Individual field spans are taken from the match for rebuilding.
_PAIR_RE = re.compile(
    r"""(?=(?P<name>[A-Za-z_][A-Za-z0-9_.\-]*)   # parameter name
        \s*[=:]\s*                               # separator
        (?P<val>"[^"]*"|'[^']*'|[^\s&;,\)\]}"']+))  # value
    """,
    re.VERBOSE)

# Bare `Bearer <token>` / `Basic <token>` in free text.
_AUTH_RE = re.compile(
    r"\b(?P<scheme>Bearer|Basic|Token)\s+(?P<val>[A-Za-z0-9._\-+/=]{8,})",
    re.IGNORECASE)

# otpauth:// URIs embed the TOTP secret as `secret=`.
_OTPAUTH_RE = re.compile(r"(otpauth://[^\s]*?secret=)([A-Z2-7]+)", re.IGNORECASE)


def is_sensitive(name):
    return str(name or "").strip().lower() in SENSITIVE_PARAMS


# `scheme://user:password@host` -- mask only the password, keep the user so
# the log still says which account was used.
_USERINFO_RE = re.compile(r"(://[^/\s:@]+):[^@/\s]+@")


def _redact_userinfo(text):
    try:
        return _USERINFO_RE.sub(r"\1:" + MASK + "@", text)
    except Exception:
        return text


def redact_value_text(text):
    """Mask sensitive `k=v` pairs anywhere inside `text`.

    Implemented with an explicit span walk rather than `re.sub` because the
    match is zero-width (see `_PAIR_RE`); the output is rebuilt by hand.
    """
    if not text:
        return text
    out = []
    pos = 0
    for m in _PAIR_RE.finditer(text):
        name = m.group("name")
        if not is_sensitive(name):
            continue
        val = m.group("val")
        start = m.start("name")
        if start < pos:
            continue  # already inside a redacted span
        out.append(text[pos:start])
        quoted = val[:1] in ("'", '"') and val[:1] == val[-1:]
        out.append("{}={}".format(
            name, (val[0] + MASK + val[-1]) if quoted else MASK))
        pos = m.end("val")
    out.append(text[pos:])
    return "".join(out)


def redact_url(url):
    """Return `url` with sensitive query/credential components masked.

    `https://u:p@host/x`   -> `https://u:***@host/x`
    `/?password=hunter2`   -> `/?password=***`
    """
    if not url:
        return ""
    out = str(url)
    try:
        # userinfo in an authority section
        out = _redact_userinfo(out)
        # otpauth secrets
        out = _OTPAUTH_RE.sub(r"\1" + MASK, out)
        # query parameters
        out = redact_value_text(out)
        return out
    except Exception:
        return MASK


def redact_path(path):
    """Redact a request path, preserving the query's non-sensitive parts.

    Unlike :func:`redact_url` this keeps enough structure to stay useful
    for debugging ("GET /api/screener?min_vol=0&max_spread_bps=100").
    Safe to call on arbitrary text, not only on a well-formed path: a
    trailing ` HTTP/1.1` is set aside first so the protocol survives
    redaction instead of being absorbed into the last query value.
    """
    if not path:
        return ""
    try:
        text = str(path)
        protocol = ""
        parts = text.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].upper().startswith("HTTP/"):
            text, protocol = parts[0], " " + parts[1]
        base, sep, query = text.partition("?")
        base = redact_url(base) if "://" in base else scrub(base)
        if not sep or not query:
            return base + protocol
        kept = []
        for pair in query.split("&"):
            if not pair:
                continue
            name, eq, val = pair.partition("=")
            if is_sensitive(name):
                kept.append("{}=***".format(name) if eq else name)
            else:
                kept.append(scrub(pair))
        return base + "?" + "&".join(kept) + protocol
    except Exception:
        return MASK


def scrub(text):
    """Sanitise a whole log line: query params, auth headers, otpauth."""
    if text is None:
        return ""
    out = str(text)
    try:
        out = _redact_userinfo(out)
        out = _AUTH_RE.sub(lambda m: "{} {}".format(m.group("scheme"), MASK), out)
        out = _OTPAUTH_RE.sub(r"\1" + MASK, out)
        out = redact_value_text(out)
        return out
    except Exception:
        try:
            return MASK
        except Exception:
            return ""


def scrub_headers(headers):
    """Copy of `headers` with sensitive values masked (for debug dumps)."""
    try:
        out = {}
        for k, v in (headers or {}).items():
            out[k] = MASK if is_sensitive(k) else scrub(v)
        return out
    except Exception:
        return {}
