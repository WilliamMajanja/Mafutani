"""Trading app server. Stdlib only (http.server + sqlite3).

- Serves /api/* via api.routes.Router
- GET /stream/quotes as SSE (polls quotes_l1, 15s heartbeat)
- Serves web/ static with correct MIME, SPA fallback to web/index.html
- init_db on boot, background feed poll thread every 30s (synthetic_tick)
"""

import json
import mimetypes
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")

try:
    from api.routes import Router
except ImportError:
    try:
        from routes import Router  # type: ignore
    except ImportError:
        Router = None  # type: ignore

try:
    from data.db import init_db
except ImportError:
    init_db = None  # type: ignore

try:
    from core.rate_limit import RateLimiter
    # Anonymous traffic keeps the original flat per-IP cap. This is the
    # abuse/DDoS control and it is deliberately NOT raised.
    _limiter = RateLimiter(600)
    # Authenticated traffic is throttled against the caller's own tier
    # instead (see _dispatch). These two backstops bound a single IP that
    # holds many sessions, which a per-user bucket alone would not.
    _auth_user_limiter = RateLimiter(600)
    _auth_ip_limiter = RateLimiter(600)
except Exception:
    _limiter = None
    _auth_user_limiter = None
    _auth_ip_limiter = None

# A single IP may spend at most this multiple of its own tier allowance
# across *all* of its sessions. Without it, one address running many
# accounts is bounded by nothing but the sum of those accounts' tiers.
# A single IP may spend at most this multiple of a session's own tier
# allowance across *all* of its sessions. Without it, one address running
# many accounts is bounded by nothing but the sum of those accounts' tiers.
#
# There was also a floor here (1200), on the theory that a house or small
# office behind one NAT needs room for several people. It did not work: the
# lowest tier's rate times this multiple is 60 x 4 = 240, so any floor at or
# below 240 is dead code, and any floor above it stops being a floor and
# becomes the limit -- which handed a Free account a 1200/min ceiling, 20x
# its own 60/min allowance, while Pro sat at the same number with a 4x
# headroom it had earned. A constant like that is either inert or a bug; this
# one was mostly the second.
#
# The NAT case is real but is not what this bound is for. A user behind a
# shared IP is still limited by their own per-user bucket; only the aggregate
# across *accounts* is bounded here. If shared-IP traffic ever complains, the
# lever is the multiple -- not a floor that silently overrides it.
AUTH_IP_BACKSTOP_MULTIPLE = 4

_ROUTER = None


def get_router():
    global _ROUTER
    if _ROUTER is None and Router is not None:
        _ROUTER = Router()
    return _ROUTER


MIME_OVERRIDES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _client_ip(handler):
    try:
        return handler.client_address[0] if handler.client_address else "unknown"
    except Exception:
        return "unknown"


class Handler(BaseHTTPRequestHandler):
    server_version = "Mafutani-TCC/1.1"

    def log_message(self, fmt, *args):
        """Every server log line passes through the redactor.

        The stdlib logs the raw request line (including the full query
        string) via `log_request`, so any credential that reaches a URL
        would be written to disk in cleartext. Scrubbing here, at the
        single sink, is what actually prevents that.
        """
        try:
            msg = fmt % args
        except Exception:
            msg = " ".join(str(a) for a in args)
        try:
            from core.logsafe import scrub
            sys.stderr.write("{} {}\n".format(
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), scrub(msg)))
            sys.stderr.flush()
        except Exception:
            pass

    def log_error(self, fmt, *args):
        self.log_message(fmt, *args)

    def log_request(self, code="-", size="-"):
        """Replace the stdlib's verbatim requestline echo."""
        try:
            parts = (self.requestline or "").split()
            method = parts[0] if parts else "-"
            target = parts[1] if len(parts) > 1 else "-"
        except Exception:
            method, target = "-", "-"
        self.log_message('%s %s %s', method, target, code)

    def send_response(self, code, message=None):
        self._status_code = code
        return super().send_response(code, message)

    # -- verbs ---------------------------------------------------------
    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- dispatch ------------------------------------------------------
    def _handle(self, method):
        t0 = time.time()
        self.command = method
        self._status_code = None
        try:
            return self._dispatch(method)
        finally:
            try:
                parsed = urllib.parse.urlparse(self.path or "/")
                self.log_message('"%s %s" %s %.0fms', method,
                                 self._redacted_target(parsed),
                                 self._status_code
                                 if self._status_code is not None else "-",
                                 (time.time() - t0) * 1000)
            except Exception:
                pass

    @staticmethod
    def _redacted_target(parsed):
        """path + query with sensitive values masked, for logging."""
        try:
            from core.logsafe import redact_path
            return redact_path(parsed.path or "/"
                               + (("?" + parsed.query) if parsed.query else ""))
        except Exception:
            return parsed.path or "/"

    def _session_tier(self):
        """(user_id, api_rate) for this request's session, or (None, None).

        RFC-001's api_rate, resolved from the session cookie. Returns None
        for anonymous traffic, which is throttled on the flat IP cap
        instead -- an unauthenticated caller has no tier to honour, and
        looking one up per request for them would be a wasted query on
        exactly the traffic most likely to be hostile.
        """
        try:
            from data.db import connect as _c
            from routes import _get_user_and_session
            from core.tiers import get_entitlements
        except Exception:
            return None, None
        conn = None
        try:
            conn = _c()
            user, _sess = _get_user_and_session(conn, self)
            if user is None:
                return None, None
            from routes import _row_to_dict
            ud = _row_to_dict(user)
            uid = ud.get("id")
            rate = int(get_entitlements(ud.get("tier", "Free")).get("api_rate", 0))
            if not uid or rate <= 0:
                return None, None
            return int(uid), rate
        except Exception:
            return None, None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _enforce_rate(self, uid, tier_rate):
        """Apply whichever limit applies to this request. True if allowed.

        Split out of _dispatch so the cheap adversarial checks below can run
        first: resolving a session costs a database round trip, and a
        scanner walking honeypot paths should not be able to make the
        server do one per probe.
        """
        if uid is None:
            # Anonymous: the original flat per-IP cap, unchanged.
            if _limiter is not None:
                try:
                    if not _limiter.allowed(_client_ip(self)):
                        return False
                except Exception:
                    return True
            return True
        # Authenticated: this user's tier allowance binds first, so a Free
        # user is held to 60 rather than the anonymous 600, and an
        # Institutional user is not clipped by a limit that was never meant
        # to describe them.
        if _auth_user_limiter is not None:
            try:
                if not _auth_user_limiter.allowed("u:%s" % uid, tier_rate):
                    return False
            except Exception:
                pass
        # ...but one IP running many sessions is still bounded, at 4x the
        # account's own allowance. This is what stops per-user buckets from
        # multiplying into an unbounded per-IP rate.
        if _auth_ip_limiter is not None:
            backstop = tier_rate * AUTH_IP_BACKSTOP_MULTIPLE
            try:
                if not _auth_ip_limiter.allowed(_client_ip(self), backstop):
                    return False
            except Exception:
                pass
        return True

    def _dispatch(self, method):
        parsed = urllib.parse.urlparse(self.path or "/")
        path = parsed.path or "/"

        # -- adversarial fingerprinting ----------------------------------
        # Runs before routing so a scanner never reaches a real endpoint.
        # A honeypot hit or a known tool UA throttles the client and
        # returns 404, never confirming what was detected.
        try:
            from core.fingerprint import fingerprint as fp
            ip = _client_ip(self)
            ua = self.headers.get("User-Agent", "")

            if fp.is_honeypot(path):
                fp.record_honeypot(path, ip)
                fp.throttle(ip, seconds=600)
                self._emit_security_event("honeypot.hit", path=path, ip=ip)
                return self._send_json({"error": "not found"}, 404)

            if fp.is_tool(ua):
                fp.throttle(ip, seconds=300)
                self._emit_security_event("tool.detected", ua=ua, ip=ip)
                return self._send_json({"error": "not found"}, 404)

            if fp.is_throttled(ip):
                return self._send_json({"error": "rate limited"}, 429)
        except Exception:
            # A fingerprinting failure must never block a request.
            pass

        # -- rate limiting -------------------------------------------------
        # After the adversarial checks, because honouring a tier needs a
        # session lookup and a database round trip, and a scanner walking
        # honeypot paths must not be able to make the server do one per
        # probe. The fingerprinting block above already throttles those
        # clients outright, so nothing is lost by ordering it first.
        _uid, _rate = self._session_tier()
        try:
            if not self._enforce_rate(_uid, _rate):
                payload = {"error": "rate limited"}
                if _uid is not None:
                    payload["limit_per_min"] = _rate
                return self._send_json(payload, 429)
        except Exception:
            pass

        if path == "/stream/quotes" and method == "GET":
            return self._sse_quotes()
        if path.startswith("/api/"):
            router = get_router()
            if router is None:
                return self._send_json({"error": "api unavailable"}, 500)
            try:
                handled = router.dispatch(self)
            except BrokenPipeError:
                return None
            except Exception as e:
                try:
                    return self._send_json({"error": "internal: {}".format(e)}, 500)
                except Exception:
                    return None
            if not handled:
                return self._send_json({"error": "not found"}, 404)
            return handled
        return self._serve_static(path)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
            # Responses that can carry credentials or session material must
            # never be cached by the browser or an intermediary.
            try:
                sensitive = (self.path or "").startswith(("/api/auth", "/api/me"))
            except Exception:
                sensitive = True
            if sensitive:
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, private")
                self.send_header("Pragma", "no-cache")
            try:
                self.wfile.write(body)
            except Exception:
                pass
        except Exception:
            pass
        return True

    def _security_headers(self):
        """Baseline hardening headers on every response.

        `Referrer-Policy: no-referrer` matters here specifically: without
        it a browser can copy a full URL (including any query string) into
        the Referer of a third-party request.
        """
        try:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; font-src 'self' data:; "
                "object-src 'none'; base-uri 'none'; form-action 'self'; "
                "frame-ancestors 'none'")
            if os.environ.get("HTTPS") == "1":
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        except Exception:
            pass

    def _emit_security_event(self, kind, **fields):
        """Journal a security event. Never let logging break a response.

        The event goes to stderr (which is captured by the log sink) and
        to the audit log if one is wired. A security event that fails to
        write must not become a 500 -- the request was already handled.
        """
        try:
            parts = ["sec.{}".format(kind)]
            for k, v in sorted(fields.items()):
                parts.append("{}={}".format(k, v))
            sys.stderr.write("{} {}\n".format(
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                " ".join(parts)))
            sys.stderr.flush()
        except Exception:
            pass
        try:
            audit = getattr(self.server, "audit_log", None)
            if audit is not None:
                audit("security", kind, **fields)
        except Exception:
            pass

    # -- SSE ------------------------------------------------------------
    def _sse_quotes(self):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
        except Exception:
            return True
        last_beat = time.time()
        try:
            from data.db import connect as _connect
        except ImportError:
            _connect = None  # type: ignore
        while time.time() - last_beat < 300:
            try:
                payload = []
                if _connect is not None:
                    try:
                        conn = _connect()
                        try:
                            syms = conn.execute(
                                "SELECT symbol FROM symbols WHERE enabled = 1").fetchall()
                            for s in syms:
                                try:
                                    sym = s["symbol"]
                                except Exception:
                                    sym = s[0]
                                q = conn.execute(
                                    "SELECT * FROM quotes_l1 WHERE symbol = ?"
                                    " ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
                                if q is None:
                                    continue
                                try:
                                    d = {k: q[k] for k in q.keys()}
                                except Exception:
                                    d = {"symbol": sym}
                                payload.append(d)
                        finally:
                            try:
                                conn.close()
                            except Exception:
                                pass
                    except Exception:
                        pass
                msg = "data: {}\n\n".format(json.dumps({"quotes": payload, "ts": int(time.time())}))
                self.wfile.write(msg.encode("utf-8"))
                try:
                    self.wfile.flush()
                except Exception:
                    pass
                if time.time() - last_beat >= 15:
                    try:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                    except Exception:
                        pass
                    last_beat = time.time()
                time.sleep(5)
            except (BrokenPipeError, ConnectionResetError):
                break
            except Exception:
                try:
                    time.sleep(5)
                except Exception:
                    break
        return True

    # -- static ----------------------------------------------------------
    def _serve_static(self, path):
        rel = urllib.parse.unquote(path)
        if rel == "/":
            rel = "/index.html"
        # block traversal
        safe = os.path.normpath(rel).lstrip("/")
        fpath = os.path.join(WEB_DIR, safe)
        if not os.path.abspath(fpath).startswith(os.path.abspath(WEB_DIR)):
            return self._send_json({"error": "forbidden"}, 403)
        if os.path.isdir(fpath):
            fpath = os.path.join(fpath, "index.html")
        if not os.path.isfile(fpath):
            # SPA fallback
            index = os.path.join(WEB_DIR, "index.html")
            if os.path.isfile(index):
                fpath = index
            else:
                return self._send_json({"error": "not found: {}".format(path)}, 404)
        ext = os.path.splitext(fpath)[1].lower()
        ctype = MIME_OVERRIDES.get(ext) or mimetypes.guess_type(fpath)[0] or \
            "application/octet-stream"
        try:
            with open(fpath, "rb") as f:
                body = f.read()
        except Exception as e:
            return self._send_json({"error": "read failed: {}".format(e)}, 500)
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # no-cache (not no-store) so an operator can always pick up an
            # edited asset immediately, which is what previously hid a JS
            # SyntaxError that silently killed login.
            self.send_header("Cache-Control", "no-cache")
            self._security_headers()
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass
        return True


# ------------------------------------------------------------------ feed loop


def _prune_stale_venue_quotes():
    """Trim venue_quotes. Runs whether or not persistence is enabled.

    This is deliberately NOT inside `_venue_quotes`, and that separation is
    the whole point. Pruning used to happen only after a successful write,
    under the same `MAFUTANI_ARB_PERSIST` flag -- so the moment an operator
    turned persistence off, the rows already written were orphaned
    forever: nothing wrote, and nothing pruned.

    Measured on this deployment: 727 rows sitting 10.6 days old against a
    3-day `MAFUTANI_VQ_MAX_AGE_S`, all of which `prune_venue_quotes` would
    have removed. That is precisely the "growing table nobody asked for"
    that the opt-in flag and the retention defaults were added to prevent,
    and the flag had made retention unreachable.

    Retaining is optional; honouring a retention limit after the fact is
    not. An operator who opts out should stop accumulating, not be left
    holding whatever was already there.
    """
    try:
        from data.db import connect as _c
        from data.feeds import prune_venue_quotes as _prune_vq
    except ImportError:
        return 0
    try:
        conn = _c()
    except Exception:
        return 0
    try:
        return _prune_vq(conn)
    except Exception:
        return 0
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _venue_quotes(symbols, timeout=5, workers=8):
    """Optional: record polled venue books to venue_quotes.

    Off unless MAFUTANI_ARB_PERSIST=1, because the scanner no longer needs
    it -- `data.feeds.live_venue_books` fetches and caches on demand, so
    arbitrage works from a cold start with nothing stored at all.

    Persisting costs a growing second market-data table on a database the
    operator may not want one in, for a record of past books the scanner
    does not read. So it is opt-in, and the default writes nothing.

    Never touches quotes_l1: the primary tape keeps one honest price per
    instrument, this is the second dimension arbitrage compares.

    Failures are silent by construction -- a scanner that cannot see both
    books must report "one venue", never invent the second.

    Pruning is NOT done here. It runs unconditionally from the feed tick
    via `_prune_stale_venue_quotes`, so that turning this flag off stops
    the writes without stranding the rows already written.
    """
    try:
        from data.feeds import (VenuePoller, persist_venue_quotes,
                                venue_persist_enabled)
    except ImportError:
        return 0
    if not venue_persist_enabled():
        return 0
    poller = VenuePoller(symbols, timeout=timeout, workers=workers)
    try:
        routes = poller.routes()
    except Exception:
        return 0
    if not routes:
        return 0
    books = poller.poll_once()
    if not books:
        return 0
    try:
        from data.db import connect as _c
        conn = _c()
    except Exception:
        return 0
    try:
        return persist_venue_quotes(conn, books)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _real_quotes(symbols, timeout=5, workers=6):
    """Real top-of-book quotes for routed crypto symbols: {symbol: quote}.

    Called once per feed tick for the whole routed set rather than per
    symbol. Fetching inside the per-symbol loop made one tick's wall
    clock the *sum* of every venue's latency: with five routed symbols
    and a venue that hangs, a tick could block for 30s, stalling the
    equities' synthetic ticks too.

    So the calls run concurrently under a single shared deadline. A
    symbol with no route, or whose fetch fails, is simply absent from the
    result; the caller falls back to a generated tick with `source`
    recording that, so a simulated price is never stored as if it were a
    live one.
    """
    try:
        from data import cryptomarkets as _cm
        from data.feeds import CryptoPoller
    except ImportError:
        return {}

    routed = [s for s in symbols if _cm.route_for(s) is not None]
    if not routed:
        return {}

    def fetch(sym):
        try:
            quote = CryptoPoller([sym], timeout=timeout).fetch_one(sym)
        except Exception:
            return sym, None
        if not isinstance(quote, dict) or "error" in quote:
            return sym, None
        if not (quote.get("bid", 0) > 0 and quote.get("ask", 0) > 0):
            return sym, None
        return sym, quote

    out = {}
    try:
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import wait as _futures_wait
    except ImportError:  # pragma: no cover - concurrent.futures is stdlib
        for sym in routed:
            key, quote = fetch(sym)
            if quote:
                out[key] = quote
        return out

    pool = ThreadPoolExecutor(max_workers=min(workers, len(routed)))
    try:
        futures = [pool.submit(fetch, sym) for sym in routed]
        # One deadline for the whole batch, not one per symbol: a batch
        # allowed N x timeout is the same stall, merely parallelised.
        done, _not_done = _futures_wait(futures, timeout=timeout)
        for fut in done:
            try:
                key, quote = fut.result()
            except Exception:
                continue
            if quote:
                out[key] = quote
    except Exception:
        pass
    finally:
        # wait=False: a thread still blocked on a dead socket must not
        # hold up the tick. Its result is simply discarded.
        pool.shutdown(wait=False)
    return out


def _feed_once():
    try:
        from data.db import connect as _connect
        from data.feeds import synthetic_tick
    except ImportError:
        return
    try:
        conn = _connect()
    except Exception:
        return
    try:
        try:
            syms = conn.execute("SELECT symbol FROM symbols WHERE enabled = 1").fetchall()
        except Exception:
            return
        enabled = []
        for s in syms:
            try:
                enabled.append(s["symbol"])
            except Exception:
                enabled.append(s[0])
        # One batched, deadline-bounded fetch for the whole tick, not a
        # blocking network call per symbol inside the write loop.
        real = _real_quotes(enabled)
        # Venue books run after the tape is written and open their own
        # connection, so a slow venue endpoint can delay neither the tape
        # nor each symbol's write above. Opt-in only; the scanner fetches
        # on demand regardless of whether this runs.
        try:
            _venue_quotes(enabled)
        except Exception:
            pass
        # Retention runs regardless of whether the write above happened.
        # It only deletes rows already past their age limit, so on a
        # deployment that never persisted anything this is a cheap no-op
        # rather than ongoing work. Keeping it here rather than inside
        # _venue_quotes is what stops an operator opting out from stranding
        # whatever was written before they opted out.
        try:
            _prune_stale_venue_quotes()
        except Exception:
            pass
        now = int(time.time())
        for sym in enabled:
            try:
                q = conn.execute("SELECT * FROM quotes_l1 WHERE symbol = ?"
                                 " ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
                last = None
                if q is not None:
                    try:
                        last = float(q["last"] or q["ask"] or q["bid"] or 0) or None
                    except Exception:
                        last = None
                if not last:
                    try:
                        c = conn.execute("SELECT close FROM candles_1m WHERE symbol = ?"
                                         " ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
                        last = float(c["close"]) if c is not None else 100.0
                    except Exception:
                        last = 100.0
                # Prefer a real venue book; fall back to a generated tick
                # and label it as such. A routed symbol whose venue is
                # unreachable must not silently keep quoting a stale
                # synthetic price as though the market were simply quiet.
                tick = real.get(sym)
                source = "synthetic"
                if tick is None:
                    tick = synthetic_tick(sym, last or 100.0)
                else:
                    source = str(tick.get("source") or tick.get("venue")
                                  or "venue")
                conn.execute(
                    "INSERT INTO quotes_l1 (symbol, ts, bid, ask, bid_size, ask_size, last, source)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (sym, now, tick.get("bid", last), tick.get("ask", last),
                     tick.get("bid_size", 0), tick.get("ask_size", 0),
                     tick.get("last", (tick.get("bid", 0) + tick.get("ask", 0)) / 2 or last),
                     source))
                conn.commit()
            except Exception:
                continue
    finally:
        try:
            # Trim the tick history this loop just extended. quotes_l1 is
            # only ever read as "latest tick per symbol", so the older rows
            # cost index bloat and COUNT(*) time and are never used.
            try:
                from data.feeds import prune_quotes as _prune_quotes
                _prune_quotes(conn)
            except Exception:
                pass
            conn.close()
        except Exception:
            pass


def _feed_loop(interval=30):
    """Background synthetic-tick loop.

    Tick immediately on entry, then every `interval` seconds, forever. A
    single failed tick must never kill the thread, so exceptions are
    swallowed per-iteration rather than breaking the loop.
    """
    try:
        _feed_once()
    except Exception:
        pass
    while True:
        try:
            time.sleep(interval)
        except Exception:
            break
        try:
            _feed_once()
        except Exception:
            continue


def _print_account_ledger(conn):
    """Print local dev credentials on boot so the operator can log in
    without consulting the repo. Gated by core.devaccounts.ledger_enabled
    (off in production, or with PRINT_ACCOUNTS=0). Never fatal."""
    try:
        from core.devaccounts import format_ledger, ledger_enabled
        if not ledger_enabled():
            return
        for line in format_ledger(conn):
            print(line)
        print("  (rotate anything you reuse outside local dev)")
    except Exception as e:
        print("account ledger unavailable: {}".format(e))
    try:
        sys.stdout.flush()
    except Exception:
        pass


def main():
    from data.db import init_db
    if init_db is not None:
        try:
            c = init_db()
            try:
                _print_account_ledger(c)
            finally:
                try:
                    c.close()
                except Exception:
                    pass
        except Exception as e:
            print("init_db failed: {}".format(e))
    # The synthetic tick loop re-prices quotes in the background. Tests
    # that seed an exact price and then assert on it race that thread and
    # fail intermittently (observed: a seeded 120.0 read back as 119.994).
    # MAFUTANI_NO_FEED=1 makes the server deterministic for those runs;
    # it is off everywhere else, because a live tape is the point.
    if os.environ.get("MAFUTANI_NO_FEED", "0") == "1":
        print("feed loop disabled (MAFUTANI_NO_FEED=1)")
    else:
        t = threading.Thread(target=_feed_loop, kwargs={"interval": 30},
                             daemon=True)
        t.start()
    port = int(os.environ.get("PORT", "8000"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("Mafutani TCC serving on :{} (web={}, db={})".format(
        port, WEB_DIR, os.environ.get("DB_PATH", os.path.join("data", "trading.db"))))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
