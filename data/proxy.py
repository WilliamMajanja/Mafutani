"""Zero-dependency proxy transport for outbound venue calls. Stdlib only.

Why this exists
---------------
Every venue call -- Binance, Coinbase, Kraken, stooq -- goes out from this
machine's IP address. For a read-only public ticker that is mostly
innocuous. For a *signed* live order it is not: the exchange sees where
the money is being moved from, which is a real privacy exposure and, in
some jurisdictions, the thing that determines whether the venue will serve
you at all.

This module lets all of that traffic leave through a proxy the operator
controls, using nothing but `socket`, `ssl` and `urllib`.

Why not just use urllib's ProxyHandler
--------------------------------------
It handles HTTP proxies well and SOCKS5 not at all. `requests[socks]`,
`socketio`, `PySocks` and `httpx` all support it, and every one of them
is a dependency -- which this project does not have and will not take.

So: SOCKS5 is implemented here. It is about a hundred lines because the
protocol genuinely is small -- a greeting, an optional username/password
exchange, and a CONNECT request. There is no cryptography in it, which is
exactly why it stays inside the stdlib budget.

Protocols supported
-------------------
* ``socks5://``  -- RFC 1928, no-auth or username/password (RFC 1929).
* ``http://``   -- absolute-form requests for plain HTTP, and ``CONNECT``
  tunnelling for HTTPS.
* ``https://``  -- a proxy reached over TLS. Rare, supported anyway.

Security notes, stated plainly
------------------------------
* A SOCKS5 or HTTP proxy sees the *destination* (and, for plain HTTP, the
  whole request). It does not see inside a TLS session, so HTTPS payloads
  remain end-to-end encrypted to the venue. A proxy is not anonymity: the
  proxy operator can correlate your traffic, and if it is a hostile proxy
  it can observe which host you talk to and when.
* Credentials may appear in the proxy URL. `describe()` never returns them,
  and `_redact()` scrubs them before anything is logged.
* No proxy is configured by default, and with none configured this module
  delegates to `urllib` exactly as before. Turning the feature on is the
  only way to change behaviour.

Configuration
-------------
``MAFUTANI_PROXY``          -- primary, e.g. ``socks5://127.0.0.1:1080``
``SOCKS_PROXY`` / ``HTTPS_PROXY`` / ``HTTP_PROXY``  -- conventional fallbacks
``MAFUTANI_PROXY_NO_PROXY`` -- comma-separated hosts to bypass
"""

import base64
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request

USER_AGENT = "mafutani-tcc/1.1"

# Venues are the hosts worth routing through a proxy. Everything else
# (localhost, the app's own loopback) must not be.
DEFAULT_NO_PROXY = ("localhost", "127.0.0.1", "::1")

_SCHEME_RE = re.compile(r"^(?P<scheme>[a-zA-Z0-9]+)://"
                        r"(?:(?P<user>[^:@/]*)(?::(?P<pw>[^@/]*))?@)?"
                        r"(?P<host>\[[0-9A-Fa-f:]+\]|[^:/?#]+)"
                        r"(?::(?P<port>\d+))?$")


class ProxyError(Exception):
    """Any failure to establish or use the proxy. Never swallowed."""


def _redact(url):
    """Proxy URL with any credentials removed. Safe to log."""
    if not url:
        return ""
    return re.sub(r"//[^/@]*@", "//***@", str(url))


def proxy_url():
    """The configured proxy, or None.

    Resolution order: MAFUTANI_PROXY, then SOCKS_PROXY, HTTPS_PROXY,
    HTTP_PROXY. An explicit MAFUTANI_PROXY="" disables proxying entirely,
    which is the escape hatch when a stray environment variable in the
    deployment is sending orders through somewhere unintended.
    """
    if "MAFUTANI_PROXY" in os.environ:
        v = os.environ.get("MAFUTANI_PROXY", "").strip()
        return v or None
    for name in ("SOCKS_PROXY", "socks_proxy", "HTTPS_PROXY", "https_proxy",
                 "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(name, "").strip()
        if v:
            return v
    return None


def parse_proxy(url=None):
    """(scheme, host, port, user, password) for the configured proxy."""
    url = url or proxy_url()
    if not url:
        return None
    if "://" not in url:
        url = "http://" + url
    m = _SCHEME_RE.match(url)
    if not m:
        raise ProxyError("cannot parse proxy url: {}".format(_redact(url)))
    scheme = m.group("scheme").lower()
    host = m.group("host")
    if host.startswith("["):
        host = host[1:-1]
    port = m.group("port")
    default = {"socks5": 1080, "socks5h": 1080, "http": 8080,
               "https": 8443}.get(scheme, 8080)
    try:
        port = int(port) if port else default
    except (TypeError, ValueError):
        raise ProxyError("proxy port is not a number: {}".format(
            _redact(url)))
    return (scheme, host, port, m.group("user"), m.group("pw"))


def _should_bypass(host, no_proxy=None, include_loopback=True):
    """True when `host` must be reached directly.

    `include_loopback` exists so the tests can drive the proxy against a
    loopback origin. In production it stays on: proxying a connection to
    localhost is never what anyone wants, and a deployment that sets a
    proxy must not silently break the app's own internal calls.
    """
    h = str(host or "").lower()
    if no_proxy is None:
        if include_loopback and h in DEFAULT_NO_PROXY:
            return True
        raw = os.environ.get("MAFUTANI_PROXY_NO_PROXY", "")
    else:
        raw = no_proxy
    for entry in str(raw or "").split(","):
        e = entry.strip().lower()
        if not e:
            continue
        if e == "*":
            return True
        if h == e or h.endswith("." + e.lstrip(".")):
            return True
    return False


# --------------------------------------------------------------------- socks5
def _recv_exactly(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ProxyError(
                "proxy closed the connection after {} of {} bytes"
                .format(len(buf), n))
        buf += chunk
    return buf


def _socks5_connect(sock, host, port, user=None, pw=None, timeout=10):
    """RFC 1928 handshake. Returns the connected, unencrypted socket."""
    # Method negotiation. Offer no-auth, plus username/password when
    # credentials exist. Offering both is required: the server picks.
    methods = [0x00] if not user else [0x00, 0x02]
    sock.sendall(bytes([0x05, len(methods)] + methods))
    ver, method = _recv_exactly(sock, 2)
    if ver != 0x05:
        raise ProxyError("proxy is not SOCKS5 (version byte {})".format(ver))
    if method == 0xFF:
        # The overwhelmingly common cause is missing credentials, and
        # "rejected every auth method" sends an operator hunting through
        # the protocol instead of at their environment.
        if not user:
            raise ProxyError(
                "proxy requires username/password auth and no credentials are "
                "configured (include them in the proxy url: "
                "socks5://user:pass@host:port)")
        raise ProxyError("proxy rejected the supplied credentials")
    if method == 0x02:
        if not user:
            raise ProxyError("proxy demands username/password but none configured")
        u = user.encode("utf-8")
        p = (pw or "").encode("utf-8")
        if len(u) > 255 or len(p) > 255:
            raise ProxyError("proxy credential longer than 255 bytes")
        sock.sendall(bytes([0x01, len(u)]) + u + bytes([len(p)]) + p)
        ver2, status = _recv_exactly(sock, 2)
        if ver2 != 0x01 or status != 0x00:
            raise ProxyError("proxy rejected the supplied credentials")

    # CONNECT. Address type 3 (domain) keeps the proxy doing the DNS, which
    # is the point: resolving locally would leak the destination hostname
    # to the local resolver and defeat the proxy's value for the caller.
    hb = host.encode("idna") if any(ord(c) > 127 for c in host) else host.encode("ascii")
    if len(hb) > 255:
        raise ProxyError("hostname too long for SOCKS5 domain form")
    req = bytes([0x05, 0x01, 0x00, 0x03, len(hb)]) + hb + port.to_bytes(2, "big")
    sock.sendall(req)
    ver, rep, _rsv, atyp = _recv_exactly(sock, 4)
    if ver != 0x05:
        raise ProxyError("bad SOCKS5 reply version {}".format(ver))
    if rep != 0x00:
        raise ProxyError("SOCKS5 CONNECT refused, code {} ({})".format(
            rep, {1: "general failure", 2: "connection not allowed",
                  3: "network unreachable", 4: "host unreachable",
                  5: "connection refused", 6: "TTL expired",
                  7: "command not supported", 8: "address type not supported"}
            .get(rep, "unknown")))
    if atyp == 0x01:
        _recv_exactly(sock, 4 + 2)
    elif atyp == 0x03:
        ln = _recv_exactly(sock, 1)[0]
        _recv_exactly(sock, ln + 2)
    elif atyp == 0x04:
        _recv_exactly(sock, 16 + 2)
    else:
        raise ProxyError("unknown SOCKS5 address type {}".format(atyp))
    return sock


def _http_connect(sock, host, port, user=None, pw=None, timeout=10):
    """CONNECT tunnelling through an HTTP proxy."""
    lines = ["CONNECT {0}:{1} HTTP/1.1".format(host, port),
             "Host: {0}:{1}".format(host, port),
             "Proxy-Connection: keep-alive",
             "User-Agent: " + USER_AGENT]
    if user:
        token = base64.b64encode(
            "{0}:{1}".format(user, pw or "").encode("utf-8")).decode("ascii")
        lines.append("Proxy-Authorization: Basic " + token)
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))

    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ProxyError("proxy closed the connection during CONNECT")
        buf += chunk
        if len(buf) > 65536:
            raise ProxyError("proxy sent an oversized CONNECT response")
    head, _, rest = buf.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = status_line.split(" ", 2)
    code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if code != 200:
        raise ProxyError("proxy refused CONNECT: {}".format(status_line[:120]))
    return sock, rest


# --------------------------------------------------------------------- request
def _url_parts(url):
    m = re.match(r"^(?P<scheme>https?)://(?P<host>\[[0-9A-Fa-f:]+\]|[^:/?#]+)"
                 r"(?::(?P<port>\d+))?(?P<path>/[^#]*)?", str(url or ""))
    if not m:
        raise ProxyError("cannot parse url: {}".format(url))
    host = m.group("host")
    if host.startswith("["):
        host = host[1:-1]
    scheme = m.group("scheme").lower()
    port = int(m.group("port")) if m.group("port") else (
        443 if scheme == "https" else 80)
    return scheme, host, port, (m.group("path") or "/")


def open_tunnel(host, port, timeout=10, proxy=None, use_tls=True):
    """A socket connected to `host:port`, optionally through the proxy."""
    cfg = parse_proxy(proxy) if (proxy or proxy_url()) else None
    if cfg is None:
        sock = socket.create_connection((host, port), timeout=timeout)
    else:
        scheme, phost, pport, user, pw = cfg
        sock = socket.create_connection((phost, pport), timeout=timeout)
        sock.settimeout(timeout)
        try:
            if scheme.startswith("socks5"):
                _socks5_connect(sock, host, port, user, pw, timeout)
            else:
                rest = b""
                sock, rest = _http_connect(sock, host, port, user, pw, timeout)
                if rest:
                    raise ProxyError(
                        "proxy sent unexpected data before the tunnel")
        except ProxyError:
            try:
                sock.close()
            except Exception:
                pass
            raise
        except Exception as e:
            try:
                sock.close()
            except Exception:
                pass
            raise ProxyError("proxy handshake failed ({}): {}".format(
                type(e).__name__, e))
    if use_tls:
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=host)
    return sock


def request(method, url, body=None, headers=None, timeout=10, proxy=None,
            bypass_loopback=True, no_proxy=None):
    """One HTTP(S) request, through the proxy when one is configured.

    Returns (status, header_dict, body_text). Raises ProxyError when the
    proxy is unusable and urllib.error.URLError when the origin is.

    The no-proxy path delegates to urllib rather than reimplementing HTTP,
    so default behaviour is exactly what it always was. Only the proxied
    path is hand-rolled, which keeps the blast radius of this module to
    the feature that needs it.
    """
    scheme, host, port, path = _url_parts(url)
    cfg = parse_proxy(proxy) if (proxy or proxy_url()) else None
    use_proxy = cfg is not None and not _should_bypass(
        host, no_proxy=no_proxy, include_loopback=bypass_loopback)

    if not use_proxy:
        req = urllib.request.Request(url, data=body, method=method.upper())
        req.add_header("User-Agent", USER_AGENT)
        for k, v in (headers or {}).items():
            req.add_header(str(k), str(v))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return (resp.status, dict(resp.headers),
                        resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return (e.code, dict(e.headers or {}),
                    e.read().decode("utf-8", "replace"))
        except urllib.error.URLError as e:
            raise urllib.error.URLError("origin unreachable: {}".format(e.reason))

    host_header = host if port in (80, 443) else "{}:{}".format(host, port)
    req_lines = ["{} {} HTTP/1.1".format(method.upper(), path),
                 "Host: " + host_header,
                 "User-Agent: " + USER_AGENT,
                 "Connection: close",
                 "Accept: */*"]
    for k, v in (headers or {}).items():
        req_lines.append("{}: {}".format(k, v))
    payload = b""
    if body is not None:
        payload = body if isinstance(body, bytes) else str(body).encode("utf-8")
        req_lines.append("Content-Length: {}".format(len(payload)))
    raw = ("\r\n".join(req_lines) + "\r\n\r\n").encode("utf-8") + payload

    sock = open_tunnel(host, port, timeout=timeout, proxy=proxy,
                       use_tls=(scheme == "https"))
    try:
        sock.settimeout(timeout)
        sock.sendall(raw)
        head, rest = _read_head(sock)
    finally:
        try:
            sock.close()
        except Exception:
            pass

    head_text = head.decode("latin-1", "replace")
    lines = head_text.split("\r\n")
    status_line = lines[0].split(" ", 2)
    status = int(status_line[1]) if len(status_line) > 1 and \
        status_line[1].isdigit() else 0
    hdrs = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            hdrs[k.strip()] = v.strip()

    chunked = hdrs.get("Transfer-Encoding", "").lower() == "chunked"
    length = None
    if not chunked:
        try:
            length = int(hdrs.get("Content-Length", ""))
        except (TypeError, ValueError):
            length = None

    if chunked:
        body = _read_until_chunk_end(sock, rest)
        return status, hdrs, _dechunk(body).decode("utf-8", "replace")
    if length is not None:
        # Read exactly what was promised and stop.
        #
        # Reading to end-of-stream instead looks equivalent and is not: a
        # proxy or origin using keep-alive holds the socket open, so the
        # reader blocks until the timeout on every single request and then
        # reports a transport failure for a response that already arrived
        # in full. Length-delimited reading is what HTTP actually
        # specifies.
        body = _read_exactly(sock, rest, length)
        return status, hdrs, body.decode("utf-8", "replace")
    # No length and not chunked: fall back to close-delimited.
    body = _read_to_eof(sock, rest)
    return status, hdrs, body.decode("utf-8", "replace")


def _read_head(sock, cap=262144):
    """Read until the header/body boundary. Returns (head_bytes, body)."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ProxyError("connection closed before response headers")
        buf += chunk
        if len(buf) > cap:
            raise ProxyError("response headers exceeded 256KB")
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


def _read_exactly(sock, have, length):
    """Exactly `length` bytes total, given `have` already buffered."""
    buf = have
    while len(buf) < length:
        chunk = sock.recv(min(65536, length - len(buf)))
        if not chunk:
            break
        buf += chunk
    return buf[:length]


def _read_to_eof(sock, have):
    buf = have
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 32 * 1024 * 1024:
            raise ProxyError("response exceeded 32MB")
    return buf


def _read_until_chunk_end(sock, have):
    buf = have
    while not buf.endswith(b"0\r\n\r\n"):
        chunk = sock.recv(65536)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 32 * 1024 * 1024:
            raise ProxyError("response exceeded 32MB")
    return buf


def _dechunk(body):
    """Decode a chunked transfer body."""
    out = []
    while True:
        line, _, rest = body.partition(b"\r\n")
        try:
            size = int(line.strip().split(b";")[0], 16)
        except (ValueError, IndexError):
            break
        if size == 0:
            break
        out.append(rest[:size])
        body = rest[size + 2:]
    return b"".join(out)


def describe():
    """Non-secret proxy status, for /api/status and the CLI."""
    cfg = parse_proxy()
    if cfg is None:
        return {"enabled": False, "url": None}
    scheme, host, port, user, pw = cfg
    return {"enabled": True, "scheme": scheme,
            "host": host, "port": port,
            "authenticated": bool(user),
            # Redacted, never the raw URL with credentials in it.
            "url": _redact(proxy_url())}


def probe(timeout=8):
    """Confirm the proxy actually works, and what it looks like from out.

    Returns a dict describing reachability. Never raises: this is a
    diagnostic, and a diagnostic that throws is worse than useless during
    an incident.
    """
    out = {"enabled": proxy_url() is not None, "ok": False,
           "via_proxy": False, "exit_ip": None, "error": None}
    if not out["enabled"]:
        out["error"] = "no proxy configured"
        return out
    try:
        status, _h, text = request("GET", "https://api.ipify.org?format=json",
                                   timeout=timeout)
        out["ok"] = 200 <= status < 300
        if out["ok"]:
            import json as _json
            try:
                out["exit_ip"] = _json.loads(text).get("ip")
            except (ValueError, TypeError):
                pass
        else:
            out["error"] = "HTTP {} from the check endpoint".format(status)
        out["via_proxy"] = True
    except Exception as e:
        out["error"] = "{}: {}".format(type(e).__name__, e)
    return out
