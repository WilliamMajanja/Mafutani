"""HTTP API routes. Stdlib only.

Exposes class Router with method dispatch(handler) -> True if handled else False.
Works with http.server BaseHTTPRequestHandler (duck-typed; no import of it here).

Auth: Cookie `session=<token>`; CSRF: POST/DELETE require `X-CSRF` header
matching the session's csrf_token (signup/login exempt).

All engine imports are lazy with fallbacks so the server boots even if
engines/ is absent.
"""

import json
import os
import time
import threading
import urllib.parse

_BOOT_TS = time.time()  # process start approx (routes imported at boot)

# ---------------------------------------------------------------- lazy core


def _load(modname, names):
    try:
        mod = __import__(modname, fromlist=names)
        return {n: getattr(mod, n) for n in names if hasattr(mod, n)}
    except Exception:
        return {}


def _db_connect():
    try:
        from data.db import connect
        return connect()
    except Exception:
        import sqlite3
        path = os.environ.get("DB_PATH", os.path.join("data", "trading.db"))
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            pass
        return conn


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        try:
            return dict(row)
        except Exception:
            return {"value": row}


def _f(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return v


# ---------------------------------------------------------------- http helpers


def _send_json(handler, obj, status=200, extra_headers=None):
    body = json.dumps(obj).encode("utf-8")
    handler.send_response(int(status))
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for k, v in (extra_headers or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except Exception:
        pass
    return True


# -- per-symbol total volume cache -------------------------------------
# Summing candles_1m per request (43k rows x 36 symbols) is ~10s on slow
# filesystems (WSL /mnt/c). Cache the aggregate and refresh lazily.
_VOL_CACHE = {"ts": 0.0, "vols": {}, "lock": threading.Lock()}
# The summary table is authoritative, so the in-process cache is only
# there to collapse a burst of concurrent screener/exchanges/analysis
# requests into one read.
VOL_CACHE_TTL = 5.0  # seconds

# tf -> table, for /api/candles. A fixed map rather than a formatted string
# so no user-supplied value can ever reach the SQL text. Adding a resolution
# means adding a row here, never a new string-building path.
_CANDLE_TABLES = {"1m": "candles_1m", "1d": "candles_1d"}


def refresh_symbol_volume(conn):
    """Rebuild the symbol_volume summary from candles_1m.

    Marks the summary fresh. Call after any bulk candle import; the screener
    otherwise reads the materialised totals instead of re-aggregating
    1.5M rows per request.
    """
    try:
        conn.execute("DELETE FROM symbol_volume")
        conn.execute(
            "INSERT INTO symbol_volume (symbol, total_volume, candle_rows,"
            " built_at, stale)"
            " SELECT symbol, COALESCE(SUM(volume),0), COUNT(*), ?, 0"
            " FROM candles_1m GROUP BY symbol",
            (int(time.time()),))
        conn.commit()
    except Exception:
        pass
    invalidate_volume_cache()
    return True


def invalidate_symbol_volume(conn=None):
    """Flag the summary as stale so the next read rebuilds it.

    Used by the CSV importer, which can add candles behind the API's back.
    """
    try:
        (conn or _db_connect()).execute(
            "UPDATE symbol_volume SET stale = 1")
        (conn or _db_connect()).commit()
    except Exception:
        pass
    invalidate_volume_cache()
    return True


def invalidate_volume_cache():
    with _VOL_CACHE["lock"]:
        _VOL_CACHE["ts"] = 0.0
        _VOL_CACHE["vols"] = {}


def _aggregate_volumes(conn):
    """Full re-aggregation, persisted into the summary table."""
    vols = {}
    try:
        for r in conn.execute(
                "SELECT symbol, COALESCE(SUM(volume),0) AS v"
                " FROM candles_1m GROUP BY symbol"):
            vols[str(r["symbol"])] = float(r["v"] or 0)
    except Exception:
        return {}
    try:
        conn.execute("DELETE FROM symbol_volume")
        conn.execute(
            "INSERT OR REPLACE INTO symbol_volume"
            " (symbol, total_volume, candle_rows, built_at, stale)"
            " SELECT symbol, COALESCE(SUM(volume),0), COUNT(*), ?, 0"
            " FROM candles_1m GROUP BY symbol",
            (int(time.time()),))
        conn.commit()
    except Exception:
        pass
    return vols


def volume_by_symbol(conn):
    """symbol -> total candle volume.

    Reads the materialised `symbol_volume` summary. If the table is empty or
    flagged stale (a bulk candle import happened), it is rebuilt once and
    then served from the summary. A short in-process cache absorbs the
    burst of screener/exchanges/analysis requests that hit this together.
    """
    now = time.time()
    with _VOL_CACHE["lock"]:
        if _VOL_CACHE["vols"] and now - _VOL_CACHE["ts"] < VOL_CACHE_TTL:
            return dict(_VOL_CACHE["vols"])
    vols = {}
    try:
        needs_build = False
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(stale),0) s FROM symbol_volume"
        ).fetchone()
        if row is not None:
            needs_build = (int(row["n"] or 0) == 0) or (int(row["s"] or 0) > 0)
        if needs_build:
            vols = _aggregate_volumes(conn)
        else:
            for r in conn.execute("SELECT symbol, total_volume FROM symbol_volume"):
                vols[str(r["symbol"])] = float(r["total_volume"] or 0)
    except Exception:
        # No summary table (very old DB): fall back to the live aggregate.
        try:
            for r in conn.execute(
                    "SELECT symbol, COALESCE(SUM(volume),0) AS v"
                    " FROM candles_1m GROUP BY symbol"):
                vols[str(r["symbol"])] = float(r["v"] or 0)
        except Exception:
            return {}
    with _VOL_CACHE["lock"]:
        _VOL_CACHE["vols"] = vols
        _VOL_CACHE["ts"] = now
    return dict(vols)


# CFTC market names do not match app symbols. "GOLD - COMMODITY EXCHANGE
# INC." has to become XAUUSD, and picking the wrong contract means showing
# a real, correctly-parsed number about the wrong market -- which is worse
# than showing nothing at all.
#
# Exact market-name matches only. No fuzzy or substring fallback: a
# partial match on "GOLD" also catches "GOLD -1 TROY OUNCE" and
# "MICRO GOLD", and a screener row quietly showing the wrong contract's
# positioning is precisely the failure this table exists to prevent.
#
# Every string below was verified against the live 2026-09-29 reports.
# The first draft was written from memory and 11 of 21 were wrong
# ("NATURAL GAS - NYMEX" does not exist; it is "NAT GAS NYME - NEW YORK
# MERCANTILE EXCHANGE"), which is exactly the kind of error that fails
# silently by never matching. tests/test_cot_overlay.py re-verifies every
# entry against stored rows, so a CFTC rename fails a test instead of
# quietly dropping a symbol off the screener.
#
# Symbols absent from this table are reported as unmatched rather than
# guessed. That is a deliberate gap, not an oversight: BTCUSD, ETHUSD and
# AAPL have no CFTC futures contract. BRENT is also absent -- the reports
# carry "BRENT LAST DAY" and Brent/WTI spreads, neither of which is the
# Brent front contract the app quotes, and a near-match would be worse
# than an honest gap.
COT_CONTRACT_MAP = {
    # energy
    "WTI":       "WTI-PHYSICAL - NEW YORK MERCANTILE EXCHANGE",
    "NGAS":      "NAT GAS NYME - NEW YORK MERCANTILE EXCHANGE",
    "GASOLINE":  "GASOLINE RBOB - NEW YORK MERCANTILE EXCHANGE",
    "HEATOIL":   "NY HARBOR ULSD - NEW YORK MERCANTILE EXCHANGE",
    # metals
    "XAUUSD":    "GOLD - COMMODITY EXCHANGE INC.",
    "XAGUSD":    "SILVER - COMMODITY EXCHANGE INC.",
    "XPTUSD":    "PLATINUM - NEW YORK MERCANTILE EXCHANGE",
    "XPDUSD":    "PALLADIUM - NEW YORK MERCANTILE EXCHANGE",
    "COPPER":    "COPPER- #1 - COMMODITY EXCHANGE INC.",
    "ALUM":      "ALUMINUM MWP - COMMODITY EXCHANGE INC.",
    # grains & oilseeds
    "WHEAT":     "WHEAT-SRW - CHICAGO BOARD OF TRADE",
    "CORN":      "CORN - CHICAGO BOARD OF TRADE",
    "OATS":      "OATS - CHICAGO BOARD OF TRADE",
    "RRICE":     "ROUGH RICE - CHICAGO BOARD OF TRADE",
    "SOYBEANS":  "SOYBEANS - CHICAGO BOARD OF TRADE",
    "SOYOIL":    "SOYBEAN OIL - CHICAGO BOARD OF TRADE",
    "SOYMEAL":   "SOYBEAN MEAL - CHICAGO BOARD OF TRADE",
    # softs
    "COTTON":    "COTTON NO. 2 - ICE FUTURES U.S.",
    "COFFEE":    "COFFEE C - ICE FUTURES U.S.",
    "COCOA":     "COCOA - ICE FUTURES U.S.",
    "SUGAR":     "SUGAR NO. 11 - ICE FUTURES U.S.",
    "OJ":        "FRZN CONCENTRATED ORANGE JUICE - ICE FUTURES U.S.",
    # livestock
    "LHOGS":     "LEAN HOGS - CHICAGO MERCANTILE EXCHANGE",
    "LCATTLE":   "LIVE CATTLE - CHICAGO MERCANTILE EXCHANGE",
    "FCATTLE":   "FEEDER CATTLE - CHICAGO MERCANTILE EXCHANGE",
}

# Financials live in the TFF report, not the commodity one. Kept separate
# because the trader classifications differ -- a EURUSD net from the
# legacy report and one from TFF are different measurements. Gold and
# silver are deliberately absent: they are physical commodities and are
# reported in the disaggregated family, not TFF.
COT_CONTRACT_MAP_TFF = {
    "EURUSD": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
    "GBPUSD": "BRITISH POUND - CHICAGO MERCANTILE EXCHANGE",
}

# Which report a symbol's positioning should be read from.
COT_REPORT_FOR = {}
for _s in COT_CONTRACT_MAP:
    COT_REPORT_FOR.setdefault(_s, "disagg_comb")
for _s in COT_CONTRACT_MAP_TFF:
    COT_REPORT_FOR.setdefault(_s, "tff_comb")


def _cot_resolver(conn, weeks=156):
    """Return fn(symbol) -> COT block for the analysis overlay.

    Resolves every mapped symbol once, up front. The alternative -- a
    query per symbol inside the scan loop -- meant 40 near-identical
    LIKE queries over a 1.4M-row table on every screener page.

    Returns a refusal-shaped block for anything it cannot answer, because
    `analyze_symbol` has to be able to distinguish "no COT for this
    symbol" from "COT for this symbol is neutral".
    """
    try:
        from data import cotstore as _cs
    except ImportError:
        return lambda s: {"available": False, "reason": "cot store unavailable"}

    wanted = {}
    for sym, report in COT_REPORT_FOR.items():
        wanted.setdefault(report, set()).add(sym)

    cache = {}
    any_data = False
    for report, syms in wanted.items():
        if not _cs.table_summary(conn, report):
            continue
        any_data = True
        table = (COT_CONTRACT_MAP if report.startswith("disagg")
                 else COT_CONTRACT_MAP_TFF)
        for sym in syms:
            market = table.get(sym)
            if not market:
                continue
            try:
                found = _cs.search_markets(conn, market, report, limit=40)
            except Exception:
                found = []
            exact = [m for m in found if m.get("market") == market]
            if not exact:
                cache[sym] = {"available": False,
                              "reason": "CFTC contract not stored: " + market}
                continue
            try:
                cache[sym] = _cs.analyse_contract(
                    conn, exact[0]["contract_code"], report=report,
                    weeks=weeks)
            except Exception:
                cache[sym] = {"available": False,
                              "reason": "COT analysis failed for " + sym}

    def resolve(sym):
        key = str(sym or "").strip().upper()
        if key in cache:
            return cache[key]
        if not any_data:
            return {"available": False,
                    "reason": "no COT data stored - run scripts/ingest_cot.py"}
        if key not in COT_REPORT_FOR:
            return {"available": False,
                    "reason": "no CFTC futures contract maps to " + key}
        return {"available": False,
                "reason": "CFTC contract not stored for " + key}

    return resolve


def _err(handler, msg, status=400, extra=None):
    payload = {"error": str(msg)}
    if extra:
        payload.update(extra)
    # A native form submit cannot read a JSON error body, so redirect back
    # to the login page with the reason in the fragment (never the query
    # string) and render the SPA, which shows #login-error.
    try:
        if _wants_html(handler):
            from urllib.parse import quote
            return _redirect(handler, "/#/login?e=" + quote(str(msg)[:200]))
    except Exception:
        pass
    return _send_json(handler, payload, status=status)


def _client_ip(h):
    """Best-effort client IP for throttling/audit. X-Forwarded-For aware."""
    try:
        fwd = h.headers.get("X-Forwarded-For") or ""
        if fwd:
            return fwd.split(",")[0].strip()[:64]
    except Exception:
        pass
    try:
        return (h.client_address[0] or "")[:64]
    except Exception:
        return ""


def _safe_json(text, default=None):
    """Tolerant json.loads for DB text columns; never raises."""
    if text is None:
        return [] if default is None else default
    if isinstance(text, (list, dict)):
        return text
    try:
        return json.loads(text)
    except Exception:
        return [] if default is None else default


def _dump_json(obj):
    """json.dumps that never raises (falls back to an empty list)."""
    try:
        return json.dumps(obj)
    except Exception:
        return "[]"


def _read_json(handler):
    """Parse a request body as JSON, or as a form when Content-Type says so.

    The form fallback exists so the login page still works if app.js fails
    to load: the browser then submits `application/x-www-form-urlencoded`,
    and rejecting it with "invalid json" would surface as a mysteriously
    broken login rather than a working one.
    """
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0:
        return {}, None
    if length > 1_000_000:
        return None, "body too large"
    try:
        raw = handler.rfile.read(length)
    except Exception as e:
        return None, "read error: {}".format(e)
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    except Exception:
        return None, "invalid encoding"
    if not text.strip():
        return {}, None
    ctype = (handler.headers.get("Content-Type") or "").lower()
    if "application/x-www-form-urlencoded" in ctype:
        import urllib.parse as _up
        pairs = _up.parse_qsl(text, keep_blank_values=True)
        return dict(pairs), None
    try:
        data = json.loads(text)
    except Exception:
        return None, "invalid json"
    if not isinstance(data, dict):
        return None, "json body must be an object"
    return data, None


def _wants_html(handler):
    """True when the caller is a browser form navigation, not fetch().

    fetch() sets `Accept: application/json`; a native form submit does not.
    Used to answer auth requests with a 303 redirect instead of JSON, so
    the no-JS fallback lands the user on a real page.
    """
    try:
        accept = (handler.headers.get("Accept") or "").lower()
    except Exception:
        return False
    if "application/json" in accept:
        return False
    return "text/html" in accept


def _redirect(handler, location, extra_headers=None):
    """303 to `location`, carrying any extra headers (e.g. the session cookie).

    Used only for the no-JS form fallback. `Location` is rebuilt from a
    fixed, same-origin path and scrubbed, so it can never become an open
    redirect or carry a credential.

    `extra_headers` may be a dict or a list of (name, value) pairs --
    `_set_session_cookie` returns a dict, other helpers may return pairs.
    """
    try:
        from core.logsafe import redact_path
        target = redact_path(str(location or "/"))
    except Exception:
        target = "/"
    if not target.startswith("/") or target.startswith("//"):
        target = "/"
    if isinstance(extra_headers, dict):
        pairs = list(extra_headers.items())
    else:
        try:
            pairs = [tuple(p) for p in (extra_headers or [])]
        except Exception:
            pairs = []
    try:
        handler.send_response(303)
        handler.send_header("Location", target)
        handler.send_header("Content-Length", "0")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Referrer-Policy", "no-referrer")
        for k, v in pairs:
            handler.send_header(str(k), str(v))
        handler.end_headers()
    except Exception as e:
        # Do not swallow silently: an exception before end_headers() means
        # the client gets an empty response, which is very hard to debug.
        _log_redirect_failure(e)
    return True


def _log_redirect_failure(e):
    try:
        import sys
        sys.stderr.write("redirect failed: {}\n".format(e))
        sys.stderr.flush()
    except Exception:
        pass


def _cookies(handler):
    out = {}
    try:
        raw = handler.headers.get("Cookie") or ""
    except Exception:
        raw = ""
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip().strip('"')
    return out


def _session_token(handler):
    return _cookies(handler).get("session", "")


def _get_user_and_session(conn, handler):
    token = _session_token(handler)
    if not token:
        return None, None
    try:
        sess = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
    except Exception:
        return None, None
    if sess is None:
        return None, None
    try:
        sd = _row_to_dict(sess)
        if int(sd.get("expires_at", 0)) <= int(time.time()):
            return None, sd
        user = conn.execute(
            "SELECT * FROM users WHERE id = ? AND disabled = 0", (sd.get("user_id"),)
        ).fetchone()
        if user is None:
            return None, sd
        return user, sess
    except Exception:
        return None, None


def _check_csrf(handler, sess):
    if sess is None:
        return False
    try:
        sd = _row_to_dict(sess)
        expected = sd.get("csrf_token", "")
    except Exception:
        return False
    provided = (handler.headers.get("X-CSRF") or handler.headers.get("X-Csrf")
                or handler.headers.get("X-CSRF-Token") or handler.headers.get("X-Csrf-Token") or "")
    if not expected or not provided:
        return False
    import hmac
    try:
        return hmac.compare_digest(str(expected), str(provided))
    except Exception:
        return False


def _session_cookie_flags():
    # SameSite=Lax blocks cross-site CSRF; Secure only when served over TLS.
    base = "HttpOnly; SameSite=Lax"
    if os.environ.get("HTTPS", "0") == "1":
        base += "; Secure"
    return base


def _set_session_cookie(handler_extra, token):
    # returns extra_headers dict
    if not token:
        return {"Set-Cookie": "session=; Path=/; Max-Age=0; {}"
                .format(_session_cookie_flags())}
    return {"Set-Cookie": "session={}; Path=/; Max-Age=86400; {}"
            .format(token, _session_cookie_flags())}


def _audit(conn, actor_id, action, tier="", paper_live="paper", details=None):
    try:
        conn.execute(
            "INSERT INTO audit_log (ts, actor_user_id, action, tier, paper_live, details_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (int(time.time()), actor_id, str(action), str(tier or ""),
             str(paper_live or "paper"), json.dumps(details or {})),
        )
        conn.commit()
    except Exception:
        pass


def _risk_event(conn, user_id, kind, reason, payload=None):
    try:
        conn.execute(
            "INSERT INTO risk_events (user_id, ts, kind, reason, order_payload_json)"
            " VALUES (?, ?, ?, ?, ?)",
            (int(user_id), int(time.time()), str(kind), str(reason),
             json.dumps(payload or {})),
        )
        conn.commit()
    except Exception:
        pass


# ---------------------------------------------------------------- fallback engines


def _fallback_risk_check(conn, user_id, order):
    """Minimal risk check when engines/risk_check.py is absent."""
    try:
        from core.risk import get_limits as _get_limits
        limits = _get_limits(conn, user_id)
    except Exception:
        limits = {"max_position_qty": 100.0, "max_notional": 100000.0,
                  "max_trades_per_hour": 20, "kill_switch": 0, "allowlist_json": "[]"}
    if int(limits.get("kill_switch") or 0):
        return False, "kill-switch engaged"
    try:
        qty = float(order.get("qty") or 0)
    except (TypeError, ValueError):
        return False, "invalid qty"
    if qty <= 0:
        return False, "qty must be > 0"
    try:
        price = float(order.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    # estimate notional via price or latest quote/position
    notional = abs(qty) * price if price > 0 else 0.0
    if notional == 0:
        try:
            sym = order.get("symbol", "")
            q = conn.execute(
                "SELECT * FROM quotes_l1 WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
                (sym,)).fetchone()
            if q is not None:
                qd = _row_to_dict(q)
                ref = float(qd.get("last") or qd.get("ask") or qd.get("bid") or 0)
                notional = abs(qty) * ref
        except Exception:
            pass
    try:
        if notional > float(limits.get("max_notional", 1e12)):
            return False, "notional exceeds max_notional"
    except Exception:
        pass
    try:
        pos = conn.execute(
            "SELECT * FROM positions WHERE user_id = ? AND symbol = ?",
            (int(user_id), order.get("symbol", ""))).fetchone()
        cur_qty = float(_row_to_dict(pos).get("qty", 0)) if pos else 0.0
        side = str(order.get("side", "buy")).lower()
        new_qty = cur_qty + (qty if side == "buy" else -qty)
        if abs(new_qty) > float(limits.get("max_position_qty", 1e12)):
            return False, "position qty exceeds max_position_qty"
    except Exception:
        pass
    allow = limits.get("allowlist_json", "[]")
    try:
        lst = json.loads(allow) if isinstance(allow, str) else list(allow or [])
        if lst and order.get("symbol") not in lst:
            return False, "symbol not in allowlist"
    except Exception:
        pass
    try:
        max_tph = int(limits.get("max_trades_per_hour", 20))
        hour_ago = int(time.time()) - 3600
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM orders WHERE user_id = ? AND created_at > ?",
            (int(user_id), hour_ago)).fetchone()
        cnt = int(n["c"]) if n is not None else 0
        if cnt >= max_tph:
            return False, "max trades per hour exceeded"
    except Exception:
        pass
    return True, "ok"


def _engine_risk_check(conn, user_id, order, portfolio=None, limits=None):
    try:
        mod = __import__("engines.risk_check", fromlist=["check"])
        fn = getattr(mod, "check", None)
        if callable(fn):
            res = fn(order, portfolio or {}, limits or {})
            if isinstance(res, tuple) and len(res) == 2:
                return bool(res[0]), str(res[1])
            if isinstance(res, dict):
                return bool(res.get("allow", res.get("ok", False))), str(
                    res.get("reason", res.get("msg", "engine decision")))
            return bool(res), "engine decision"
    except Exception:
        pass
    return _fallback_risk_check(conn, user_id, order)


def _latest_price(conn, symbol, default=100.0):
    try:
        q = conn.execute("SELECT * FROM quotes_l1 WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
                         (symbol,)).fetchone()
        if q is not None:
            qd = _row_to_dict(q)
            for k in ("last", "ask", "bid"):
                v = float(qd.get(k) or 0)
                if v > 0:
                    return v
    except Exception:
        pass
    try:
        c = conn.execute("SELECT * FROM candles_1m WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
                         (symbol,)).fetchone()
        if c is not None:
            return float(_row_to_dict(c).get("close") or default)
    except Exception:
        pass
    return float(default)


def _latest_price_sourced(conn, symbol, default=100.0):
    """Latest price plus where it came from.

    Returns (price, "quote"|"candle"|"fallback"). Callers that fill an
    order must reject "fallback": a fabricated price is worse than no
    fill, because it books a loss or a gain that never happened.
    """
    sym = str(symbol or "").strip().upper()
    try:
        q = conn.execute("SELECT * FROM quotes_l1 WHERE symbol = ?"
                         " ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
        if q is not None:
            qd = _row_to_dict(q)
            for k in ("last", "ask", "bid"):
                v = float(qd.get(k) or 0)
                if v > 0:
                    return v, "quote"
    except Exception:
        pass
    try:
        c = conn.execute("SELECT * FROM candles_1m WHERE symbol = ?"
                         " ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
        if c is not None:
            v = float(_row_to_dict(c).get("close") or 0)
            if v > 0:
                return v, "candle"
    except Exception:
        pass
    return float(default), "fallback"


def _apply_paper_fill(conn, user_id, order_id, order):
    """Insert fill row + update position for a paper order. Returns fill dict.

    Routes through the real engines.paper.match when possible (slippage +
    tiered fees + short-capable avg-price bookkeeping), else a corrected
    built-in fallback. Both paths support short selling: negative position
    qty, direction-aware realized P&L, and avg re-basing on flip-through-zero.
    """
    symbol = order.get("symbol", "")
    side = str(order.get("side", "buy")).lower()
    try:
        qty = abs(float(order.get("qty") or 0))
    except (TypeError, ValueError):
        qty = 0.0
    otype = str(order.get("type", "market")).lower() or "market"
    price = order.get("price")
    try:
        price_f = float(price) if price is not None else 0.0
    except (TypeError, ValueError):
        price_f = 0.0

    # -- real engine first (paper matching: touch + slippage + fees)
    try:
        mod = __import__("engines.paper", fromlist=["match"])
        match = getattr(mod, "match", None)
        if callable(match):
            engine_order = dict(order)
            engine_order.update({
                "id": order_id, "symbol": symbol, "side": side,
                "qty": qty, "type": otype if otype in ("market", "limit", "stop") else "market",
                "price": price_f if price_f else (order.get("price")),
            })
            res = match(conn, int(user_id), engine_order)
            if isinstance(res, dict):
                out = {
                    "resting": not bool(res.get("filled")),
                    "filled": bool(res.get("filled")),
                    "price": res.get("price"),
                    "qty": qty,
                    "fee": res.get("fee", 0.0),
                    "ts": res.get("ts", int(time.time())),
                    "note": res.get("reason") if not res.get("filled") else "",
                }
                if res.get("fill_id") is not None:
                    out["id"] = res.get("fill_id")
                # Settle cash before returning. Without this the engine's
                # early return bypasses settlement and the debit never
                # reaches the accounts table.
                res_price = float(res.get("price") or 0.0)
                if res.get("filled") and res_price > 0:
                    acct = conn.execute(
                        "SELECT cash FROM accounts WHERE user_id = ?",
                        (int(user_id),)).fetchone()
                    cash = float(acct["cash"]) if acct is not None else 0.0
                    notional = qty * res_price
                    fee = float(res.get("fee", 0.0) or 0.0)
                    if side == "buy":
                        cash -= notional + fee
                    else:
                        cash += notional - fee
                    conn.execute(
                        "UPDATE accounts SET cash = ? WHERE user_id = ?",
                        (cash, int(user_id)))
                    out["cash_after"] = round(cash, 2)
                conn.commit()
                return out
    except Exception:
        pass

    # -- built-in fallback (same math, no engine import)
    if price_f <= 0 or otype == "market":
        ref, ref_src = _latest_price_sourced(conn, symbol)
        if otype == "market":
            # A market order must be filled at a real observed price.
            # The old code called _latest_price(conn, symbol), which
            # returns a hardcoded 100.0 when the symbol has no quote and
            # no candles. That silently filled 5,000oz of XAUUSD at
            # $100 against a $2,699 market and 5,000 of NICKEL at $100
            # against $15,931 -- manufacturing ~1.3M of fictional
            # realized loss on an account that never traded at those
            # prices. Inventing a price is never correct; refusing to
            # fill is.
            if ref_src == "fallback":
                raise ValueError(
                    "no market price for {}: symbol has no quote and no "
                    "candles, refusing to fill at an invented price".format(
                        symbol))
            price_f = ref
        elif price_f <= 0:
            price_f = ref
    # resting limit: not marketable -> stays open, no fill
    if otype == "limit" and price_f > 0:
        ref = _latest_price(conn, symbol)
        if (side == "buy" and price_f < ref) or (side == "sell" and price_f > ref):
            conn.commit()
            return {"resting": True, "price": price_f, "qty": 0,
                    "ts": int(time.time()), "note": "limit rests (not marketable)"}
    # slippage + fee (simple defaults)
    fee = abs(qty) * price_f * 0.0005  # 5 bps
    ts = int(time.time())
    cur = conn.execute(
        "INSERT INTO fills (order_id, user_id, symbol, side, qty, price, fee, ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (order_id, int(user_id), symbol, side,
         qty if side == "buy" else -qty, price_f, fee, ts))
    fill_id = cur.lastrowid
    # update position (avg-price bookkeeping)
    pos = conn.execute("SELECT * FROM positions WHERE user_id = ? AND symbol = ?",
                       (int(user_id), symbol)).fetchone()
    delta = qty if side == "buy" else -qty
    if pos is None:
        conn.execute(
            "INSERT INTO positions (user_id, symbol, qty, avg_price, realized_pnl, updated_at)"
            " VALUES (?, ?, ?, ?, 0, ?)",
            (int(user_id), symbol, delta, price_f, ts))
    else:
        pd = _row_to_dict(pos)
        old_qty = float(pd.get("qty") or 0)
        old_avg = float(pd.get("avg_price") or 0)
        new_qty = old_qty + delta
        rpnl = float(pd.get("realized_pnl") or 0)
        if old_qty != 0 and (old_qty > 0) != (delta > 0):
            closed = min(abs(old_qty), abs(delta))
            rpnl += closed * (price_f - old_avg) * (1 if old_qty > 0 else -1)
        if new_qty == 0:
            new_avg = 0.0
        elif (old_qty > 0) == (delta > 0) or old_qty == 0:
            new_avg = (abs(old_qty) * old_avg + abs(delta) * price_f) / abs(new_qty)
        else:
            # flip through zero (long -> short or short -> long): new basis
            new_avg = price_f
        conn.execute(
            "UPDATE positions SET qty = ?, avg_price = ?, realized_pnl = ?, updated_at = ?"
            " WHERE user_id = ? AND symbol = ?",
            (new_qty, new_avg, rpnl, ts, int(user_id), symbol))
    # -- cash settlement --------------------------------------------------
    # A fill moves cash: a buy spends notional + fee, a sell earns
    # notional - fee. Without this the balance never reflects trades and
    # the purchased asset is unusable, which is the failure this fixes.
    acct = conn.execute("SELECT cash FROM accounts WHERE user_id = ?",
                        (int(user_id),)).fetchone()
    cash = float(acct["cash"]) if acct is not None else 0.0
    notional = qty * price_f
    if side == "buy":
        cash -= notional + fee
    else:
        cash += notional - fee
    conn.execute("UPDATE accounts SET cash = ? WHERE user_id = ?",
                 (cash, int(user_id)))
    conn.execute("UPDATE orders SET status = 'filled' WHERE id = ?", (order_id,))
    conn.commit()
    return {"id": fill_id, "price": price_f, "qty": qty, "fee": fee, "ts": ts,
            "cash_after": round(cash, 2)}


def _preview_context(conn, symbol, limit=250):
    """Market context the preview is computed against.

    Returns (candles_oldest_first, quote_or_None). The quote is what the
    spread filter reads, so a preview that omitted it would silently skip
    the max_spread_bps gate the live path enforces -- a preview that
    cannot reproduce live behaviour is worse than no preview.
    """
    try:
        rows = conn.execute(
            "SELECT * FROM candles_1m WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
            (symbol, int(limit))).fetchall()
    except Exception:
        rows = []
    candles = [_row_to_dict(r) for r in rows][::-1]
    quote = None
    try:
        r = conn.execute(
            "SELECT * FROM quotes_l1 WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
            (symbol,)).fetchone()
        if r is not None:
            quote = _row_to_dict(r)
    except Exception:
        quote = None
    return candles, quote


def _signal_label(side):
    """Normalise the engine's side to the buy/sell/flat vocabulary the UI uses."""
    s = str(side or "").strip().lower()
    if s in ("buy", "long"):
        return "buy"
    if s in ("sell", "short"):
        return "sell"
    return "flat"


# How old a venue book may be and still count as live. Beyond this the
# scanner reports the symbol as having fewer venues rather than
# comparing a fresh price against a stale one.
ARB_MAX_QUOTE_AGE_S = 90


def _arbitrage_books(conn, symbols=None, max_age_s=None, allow_live=True):
    """Latest per-(symbol, venue) book: {symbol: {venue: book}}.

    Two sources, in order of preference:

    1. `venue_quotes`, when a row is fresh. Reading stored books keeps a
       scan from becoming a burst of outbound venue calls.
    2. `data.feeds.live_venue_books`, which polls on demand behind a
       short in-process cache.

    The second source is what makes the scanner work at all on a fresh
    deployment. It used to read only `venue_quotes`, which meant arbitrage
    was silently empty until the background feed loop happened to have
    run -- so a scan on a new database, or one started with venue polling
    switched off, reported "no opportunities" when the honest answer was
    "never looked". A scanner that cannot distinguish those two states
    reads as broken.

    Stale rows are excluded either way. A 40-minute-old Binance quote
    next to a live Coinbase one would show as a 900-bps "dislocation"
    that vanished the instant you looked, which is the single most
    effective way to make an arbitrage scanner useless.
    """
    from data.venuebooks import VENUE_ROUTES
    now = int(time.time())
    age = int(max_age_s) if max_age_s else ARB_MAX_QUOTE_AGE_S
    wanted = None
    if symbols:
        wanted = {str(s or "").strip().upper() for s in symbols}
    out = {}
    try:
        rows = conn.execute(
            "SELECT vq.symbol, vq.venue, vq.ts, vq.bid, vq.ask, vq.bid_size,"
            " vq.ask_size, vq.last, vq.source FROM venue_quotes vq"
            " JOIN (SELECT symbol, venue, MAX(ts) AS mx FROM venue_quotes"
            "       GROUP BY symbol, venue) g"
            "   ON g.symbol = vq.symbol AND g.venue = vq.venue"
            "  AND g.mx = vq.ts").fetchall()
    except Exception:
        rows = []
    for r in rows:
        d = _row_to_dict(r)
        sym = str(d.get("symbol", "")).upper()
        if wanted and sym not in wanted:
            continue
        if sym not in VENUE_ROUTES:
            continue
        d["age_s"] = now - int(d.get("ts") or 0)
        if age > 0 and d["age_s"] > age:
            # Kept, and flagged, rather than dropped.

            # The default-risk literature finds arbitrage dislocations
            # *persist* on venues traders distrust, because the
            # arbitrageurs who could close them hesitate. So a gap that is
            # still there after the freshness window is not noise to be
            # swept away -- it is the case worth surfacing.

            # It is still never reported as an executable edge (see
            # cross_venue: stale books yield edge_bps=None). The
            # distinction that matters: "we cannot price this right now"
            # and "there is no opportunity" are different facts, and the
            # old code collapsed them into one empty table.
            d["stale"] = True
        out.setdefault(sym, {})[str(d.get("venue", ""))] = d

    if not allow_live:
        return out

    # Fetch whatever has no FRESH book, so a warm venue_quotes does not
    # trigger a redundant round of venue calls.
    #
    # "Fresh", not "present". Keeping stale rows (above) is right for the
    # persistence signal, but a symbol whose only rows are stale must not
    # count as satisfied -- doing so silently served 4.8-day-old prices as
    # though they were live, because the stale row suppressed the fetch.
    need = [s for s in (wanted or VENUE_ROUTES.keys())
            if not any(not b.get("stale") for b in (out.get(s) or {}).values())]
    if not need:
        return out
    try:
        from data.feeds import live_venue_books
        live = live_venue_books(need)
    except Exception:
        live = {}
    for sym, node in (live or {}).items():
        if wanted and sym not in wanted:
            continue
        # A live fetch must never override a stored book: the stored one
        # is the same venue's own quote and merging them would fabricate
        # a venue nobody published.
        out.setdefault(sym, {}).update(node)
    return out


def _arbitrage_partition(conn, books):
    """Split {symbol: {venue: book}} into crypto / fx / commodity buckets.

    The split comes from the `symbols` table rather than from the venue
    registry: `symbols.asset_type` is the same field the screener and the
    analysis engine already read, so a symbol cannot be a crypto pair to
    one page and an FX pair to the arbitrage scanner.

    A symbol with no row falls into "other" and is dropped. Inventing a
    class for it would feed a model an instrument it cannot price
    meaningfully.
    """
    out = {"crypto": {}, "fx": {}, "commodity": {}, "other": {}}
    if not books:
        return out
    known = {}
    try:
        for row in conn.execute("SELECT symbol, asset_type FROM symbols"):
            d = _row_to_dict(row)
            known[str(d.get("symbol", "")).upper()] = str(
                d.get("asset_type", "") or "").strip().lower()
    except Exception:
        return out
    for sym, node in books.items():
        bucket = out.get(known.get(sym, "other"))
        if bucket is None:
            bucket = out["other"]
        if bucket is not out["other"]:
            bucket[sym] = node
    return out


def _arbitrage_history(conn, symbols, limit=240):
    """{symbol: [closes...]} for the z-score on commodity spreads.

    Aligned by *timestamp*, not by row index: two contracts have their
    own candle sets, and pairing the 100th bar of one with the 100th of
    the other mixes times that are minutes apart. A gold/silver ratio
    computed from misaligned bars is a different, meaningless number.
    """
    out = {}
    wanted = {str(s or "").strip().upper() for s in (symbols or [])}
    if not wanted:
        return out
    series = {}
    for sym in sorted(wanted):
        try:
            rows = conn.execute(
                "SELECT ts, close FROM candles_1m WHERE symbol = ?"
                " ORDER BY ts DESC LIMIT ?", (sym, int(limit))).fetchall()
        except Exception:
            rows = []
        series[sym] = {int(r[0]): _f(r[1]) for r in rows if _f(r[1]) > 0}
    if len(series) < 2:
        return {}
    # Index every series onto the shared timestamps so each pair is
    # compared like-for-like.
    common = None
    for sym, points in series.items():
        keys = set(points.keys())
        common = keys if common is None else (common & keys)
    if not common or len(common) < 20:
        return {}
    stamps = sorted(common, reverse=True)[:int(limit)]
    for sym, points in series.items():
        out[sym] = [points[t] for t in stamps if t in points]
    return out


def _engine_arbitrage(conn, ud, asset_class="all", symbol="", min_edge_bps=None,
                      size=None):
    """Run the arbitrage scanner for a tier. Returns (payload dict).

    `asset_class` is one of crypto / fx / commodity / all. The models are
    dispatched per class because the *kind* of arbitrage differs: only
    crypto has two venues, only FX has a quote convention that makes a
    cross a multiplication, only commodity has correlated contracts
    worth a ratio. Running all four models over all three classes would
    produce mostly "no books" rows.
    """
    tier = str((ud or {}).get("tier", "Free"))
    want = str(asset_class or "all").strip().lower() or "all"
    payload = {"asset_class": want, "tier": tier, "generated_at": int(time.time()),
               "models": {}, "opportunities": [], "venues": []}
    try:
        from data.venuebooks import all_venues
        payload["venues"] = all_venues()
    except Exception:
        pass

    if want not in ("all", "crypto", "fx", "commodity"):
        payload["error"] = "asset_class must be crypto|fx|commodity|all"
        return payload

    books = _arbitrage_books(conn, symbols=[symbol] if symbol else None)

    # Partition by asset class before running models. Passing one flat
    # book set to all four models produced crypto cycles through EURUSD
    # and "cross-venue" on a pair with a single venue -- structurally
    # meaningless numbers that happened to be arithmetically computable.
    # The models are per-class because the *kind* of arbitrage differs.
    by_class = _arbitrage_partition(conn, books)

    rows = []
    from engines import arbitrage as _arb

    # symbol -> class, so a class filter can select from one flat pool.
    class_of = {}
    for cls, bucket in by_class.items():
        if cls == "other":
            continue
        for sym in bucket:
            class_of[sym] = cls
    def _in_scope(cls):
        return want == "all" or want == cls

    # Cross-venue arbitrage is not crypto-specific. It means "two venues
    # quote this instrument", and gold qualifies: Binance and Kraken both
    # publish XAUTUSD. Restricting this to the crypto bucket left the
    # commodity page empty even when it had two real books.
    cross_books = {}
    for cls, bucket in by_class.items():
        if not _in_scope(cls):
            continue
        for sym, node in bucket.items():
            if len(node) >= 2:
                cross_books[sym] = node
    rows.extend(_arb.scan_crypto(sorted(cross_books.keys()), cross_books,
                                 size=size, attribute=True))

    if _in_scope("crypto"):
        crypto_syms = sorted(by_class["crypto"].keys())
        # Triangular cycles need venue-tagged books so each leg can be
        # costed against its own venue.
        rows.extend(_arb.scan_triangles(crypto_syms, by_class["crypto"]))
    if _in_scope("fx"):
        rows.extend(_arb.scan_fx(by_class["fx"]))
    if _in_scope("commodity"):
        hist = _arbitrage_history(conn, sorted(by_class["commodity"].keys()))
        rows.extend(_arb.scan_commodity(by_class["commodity"], history=hist))

    # Flat vs per-model counts, so a UI can label a section without
    # re-deriving the grouping.
    by_model = {}
    for r in rows:
        m = r.get("model", "?")
        by_model[m] = by_model.get(m, 0) + 1
    payload["models"] = by_model
    payload["scanned"] = len(rows)

    threshold = None
    if min_edge_bps is not None:
        try:
            threshold = float(min_edge_bps)
        except (TypeError, ValueError):
            threshold = None
    # `opportunities` keeps everything, priceable or not, so the UI can
    # explain *why* there is no edge rather than showing an empty table.
    payload["opportunities"] = rows

    # `profitable` is strictly edge > 0, always. An earlier version gated
    # this on `min_edge_bps` being absent-or-met and reported negative
    # edges as "net-positive", which put "2 net-positive" above a table
    # of two losing trades. A caller-supplied threshold can only narrow
    # this set further, never widen it.
    priced = []
    for r in rows:
        e = r.get("edge_bps")
        if e is None or e <= 0:
            continue
        if threshold is not None and e < threshold:
            continue
        priced.append(r)
    priced.sort(key=lambda r: -(r.get("edge_bps") or 0.0))

    # Attribution rollup: how many priced edges are one venue's book?
    #
    # Kept beside `profitable_count` rather than buried per-row, because
    # the failure this guards against is a scanner reporting a profitable
    # cross-venue edge that is really one venue quoting oddly -- see
    # engines.arbitrage.venue_attribution and Flowerday et al. (2026). A
    # headline count that hides this is the same bug with a summary on top.
    fragile = 0
    untested = 0
    for r in priced:
        attr = r.get("attribution") or {}
        if not attr.get("testable"):
            untested += 1
        elif attr.get("robust") is False:
            fragile += 1
    payload["profitable"] = priced
    payload["profitable_count"] = len(priced)
    payload["priced_count"] = sum(1 for r in rows if r.get("edge_bps") is not None)
    payload["attribution"] = {
        "priced": len(priced),
        "fragile": fragile,
        "untested": untested,
        "robust": len(priced) - fragile - untested if priced else 0,
        "note": ("fragile = the edge collapses when one venue is removed, so "
                 "it is that venue's book rather than a market dislocation. "
                 "untested = fewer than 3 venues, so attribution could not "
                 "run."),
    }
    return payload



def _engine_preview(conn, strategy_name, symbol, params):
    """Evaluate one strategy against stored candles and return its verdict.

    This is the *only* preview path. It goes through the same
    engines.signals.preview_signal entry point the backtester and the
    live board use, so there is no live/preview drift.

    It previously looked for a `preview` symbol that does not exist and
    then called the per-strategy functions with two arguments when they
    take three (candles, quote, params). The TypeError was swallowed by
    a bare `except Exception: pass`, so every strategy -- including
    VWAP Pullback -- fell through to a generic EMA9/EMA21 computation
    and reported it under that strategy's name. A tool that answers
    confidently from the wrong function is worse than a broken one.
    """
    params = params or {}
    candles, quote = _preview_context(conn, symbol)

    def wrap(res, source):
        res = res if isinstance(res, dict) else {}
        side = _signal_label(res.get("side"))
        out = {
            "signal": side,
            "side": res.get("side"),
            "qty": res.get("qty"),
            "reason": str(res.get("reason", "")),
            "source": source,
            "strategy": strategy_name,
            "symbol": symbol,
            "bars": len(candles),
        }
        if quote is not None:
            last = quote.get("last")
            bid, ask = quote.get("bid"), quote.get("ask")
            out["last"] = last
            out["bid"] = bid
            out["ask"] = ask
            try:
                if bid and ask and float(bid) > 0 and float(ask) > 0:
                    mid = (float(bid) + float(ask)) / 2.0
                    if mid > 0:
                        out["spread_bps"] = round((float(ask) - float(bid)) / mid * 10000.0, 2)
            except (TypeError, ValueError):
                pass
        if not out["bars"]:
            out["reason"] = "no candles stored for {}".format(symbol)
        return out

    try:
        from engines.signals import preview_signal
    except Exception as e:
        return wrap({"side": None, "reason": "signal engine unavailable: {}".format(e)},
                    "engine-unavailable")
    return wrap(preview_signal(strategy_name, candles, quote, params), "engine")


def _fallback_backtest(conn, strategy_name, symbol, params, from_ts=0, to_ts=0):
    try:
        q = "SELECT * FROM candles_1m WHERE symbol = ?"
        args = [symbol]
        if from_ts:
            q += " AND ts >= ?"
            args.append(int(from_ts))
        if to_ts:
            q += " AND ts <= ?"
            args.append(int(to_ts))
        # Same window the engine uses. It was hardcoded to 20000 here while
        # the engine said something else, which is how a "cap" ends up
        # enforced in one place and ignored in the other.
        try:
            from engines.backtest import BACKTEST_MAX_BARS
        except ImportError:
            BACKTEST_MAX_BARS = 259200
        q += " ORDER BY ts ASC LIMIT ?"
        args.append(BACKTEST_MAX_BARS)
        rows = conn.execute(q, args).fetchall()
        candles = [_row_to_dict(r) for r in rows]
    except Exception:
        candles = []
    closes = [float(c.get("close", 0)) for c in candles]
    equity = []
    cash = 10000.0
    pos = 0.0
    wins = 0
    losses = 0
    peak = cash
    max_dd = 0.0
    if len(closes) >= 22:
        for i in range(21, len(closes)):
            fast = sum(closes[i - 9:i]) / 9
            slow = sum(closes[i - 21:i]) / 21
            px = closes[i]
            if strategy_name == "manual":
                continue
            if fast > slow and pos <= 0:
                if pos < 0:
                    cash += abs(pos) * px
                    if px < closes[i - 1]:
                        wins += 1
                    else:
                        losses += 1
                    pos = 0.0
                if pos == 0 and cash > px:
                    q = cash * 0.95 / px
                    pos = q
                    cash -= q * px
            elif fast < slow and pos >= 0:
                if pos > 0:
                    cash += pos * px
                    if px > closes[i - 1]:
                        wins += 1
                    else:
                        losses += 1
                    pos = 0.0
            val = cash + pos * px
            equity.append({"ts": candles[i].get("ts", i), "v": round(val, 2)})
            peak = max(peak, val)
            if peak > 0:
                max_dd = min(max_dd, (val - peak) / peak)
    final = (cash + pos * (closes[-1] if closes else 0)) if closes else cash
    total_ret = (final - 10000.0) / 10000.0
    n_trades = wins + losses
    metrics = {
        "strategy": strategy_name,
        "symbol": symbol,
        "candles": len(closes),
        "final_value": round(final, 2),
        "total_return": round(total_ret, 4),
        "cagr": round(total_ret, 4),
        "sharpe": 0.0,
        "max_drawdown": round(max_dd, 4),
        "win_rate": round(wins / n_trades, 4) if n_trades else 0.0,
        "profit_factor": 1.0,
        "trades": n_trades,
        "avg_hold": 0.0,
    }
    # This fallback models no per-trade P&L -- it is a long/flat EMA proxy
    # that exists so the page is never empty when the engine is missing.
    # It reports no Sharpe (0.0 above) for the same reason, so it must not
    # carry a significance verdict either. An explicit refusal is the
    # honest answer; omitting the key entirely would leave a consumer
    # reading "no verdict" as "no problem found".
    metrics["significance"] = {
        "ok": False,
        "verdict": "not evaluable",
        "trades": 0,
        "dsr": None,
        "psr": None,
        "reasons": ["fallback engine: no per-trade P&L, so no edge test "
                    "is possible"],
    }
    try:
        from engines.backtest import coverage
        metrics["coverage"] = coverage(candles, from_ts, to_ts)
    except Exception:
        pass
    return metrics, equity


def _engine_backtest(conn, strategy_name, symbol, params, from_ts, to_ts):
    try:
        mod = __import__("engines.backtest", fromlist=["run"])
        fn = getattr(mod, "run", None)
        if callable(fn):
            res = fn(conn=conn, strategy=strategy_name, symbol=symbol,
                     params=params, from_ts=from_ts, to_ts=to_ts)
            if isinstance(res, dict):
                metrics = res.get("metrics", res)
                # Attach the significance verdict to the metrics dict the
                # API persists and returns. A backtest that reports a
                # Sharpe without saying whether that Sharpe survives
                # deflation is the exact failure engines/significance.py
                # was written to prevent, and this is where every
                # /api/backtests response comes from.
                sig_rep = res.get("significance")
                if isinstance(sig_rep, dict) and isinstance(metrics, dict):
                    metrics = dict(metrics)
                    metrics["significance"] = sig_rep
                cov = res.get("coverage")
                if isinstance(cov, dict) and isinstance(metrics, dict):
                    metrics["coverage"] = cov
                return metrics, res.get("equity", [])
            if isinstance(res, tuple) and len(res) == 2:
                return res[0], res[1]
    except Exception:
        pass
    return _fallback_backtest(conn, strategy_name, symbol, params,
                              from_ts, to_ts)


# ---------------------------------------------------------------- status service


def _candle_row_total(conn):
    """Total candles_1m rows, via the summary when it is trustworthy.

    The summary is a GROUP BY over the same table, so SUM(candle_rows) is
    exactly COUNT(*) -- but it costs three row reads instead of 1.5M. If the
    summary is missing or flagged stale (a bulk import since the last build)
    we fall back to the real count rather than report a stale number.
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(candle_rows),0) total,"
            " COALESCE(SUM(stale),0) stale FROM symbol_volume").fetchone()
        if row is not None and int(row["n"] or 0) > 0 and int(row["stale"] or 0) == 0:
            return int(row["total"] or 0)
    except Exception:
        pass
    try:
        return int(conn.execute("SELECT COUNT(*) FROM candles_1m").fetchone()[0])
    except Exception:
        return -1


def _status_payload(conn):
    """Verbose service snapshot: uptime, table counts, feed freshness."""
    now = int(time.time())
    out = {"ok": True, "service": "mafutani-tcc",
           "name": "Mafutani Trading Consolidation and Consortium",
           "version": "1.1.0", "time": now,
           "uptime_s": int(now - _BOOT_TS), "tables": {}, "feeds": []}
    # candles_1m is excluded from the naive COUNT(*) loop: it holds ~1.5M
    # rows, and counting them costs ~4.5s on this filesystem. Its count is
    # taken from the materialised summary instead (below), which is exact
    # whenever the summary is fresh.
    for t in ("users", "orders", "fills", "positions", "backtests",
              "quotes_l1", "risk_events", "audit_log", "journal"):
        try:
            r = conn.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()
            out["tables"][t] = int(r[0]) if r else 0
        except Exception:
            out["tables"][t] = -1
    out["tables"]["candles_1m"] = _candle_row_total(conn)
    try:
        syms = [r[0] for r in conn.execute(
            "SELECT symbol FROM symbols WHERE enabled = 1 ORDER BY symbol").fetchall()]
    except Exception:
        syms = []
    out["symbols"] = syms
    # Per-symbol candle counts come from the materialised summary: a
    # COUNT(*) per symbol visits every one of the 1.5M rows and made this
    # endpoint take ~10s. MAX(ts) is an O(1) index seek on (symbol, ts).
    candle_counts = {}
    try:
        for r in conn.execute(
                "SELECT symbol, candle_rows FROM symbol_volume WHERE stale = 0"):
            candle_counts[str(r["symbol"])] = int(r["candle_rows"] or 0)
    except Exception:
        candle_counts = {}
    for s in syms:
        try:
            q = conn.execute("SELECT ts, last, source FROM quotes_l1 WHERE symbol = ?"
                             " ORDER BY ts DESC LIMIT 1", (s,)).fetchone()
            c = conn.execute("SELECT MAX(ts) FROM candles_1m WHERE symbol = ?",
                             (s,)).fetchone()
            qd = _row_to_dict(q) if q else {}
            out["feeds"].append({
                "symbol": s,
                "quote_ts": int(qd.get("ts") or 0),
                "quote_age_s": now - int(qd.get("ts") or now),
                "last": float(qd.get("last") or 0),
                # 'synthetic' or the venue that produced the book. A caller
                # that cares whether a price is real can now tell.
                "source": str(qd.get("source") or "synthetic"),
                "candles": candle_counts.get(s, 0),
                "candles_ts": int(c[0] or 0) if c else 0,
            })
        except Exception:
            continue
    # Feed provenance roll-up: how many symbols are quoting a real venue
    # book versus a generated tick.
    try:
        real = sum(1 for f in out["feeds"] if f.get("source") != "synthetic")
        out["feeds_real"] = real
        out["feeds_synthetic"] = len(out["feeds"]) - real
    except Exception:
        pass
    try:
        ks = conn.execute("SELECT COUNT(*) FROM risk_limits WHERE kill_switch = 1").fetchone()
        out["kill_switches"] = int(ks[0]) if ks else 0
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- Router


def _live_allowed(ud):
    """(allowed, reason) for live trading, checked from first principles.

    Every live path re-derives this rather than trusting a flag stored
    earlier: the env can change, the tier can be downgraded, and a
    cached "live is fine" is exactly the thing that must not be trusted
    with real money.
    """
    import os as _os
    if _os.environ.get("LIVE_TRADING_ENABLED", "0") != "1":
        return False, "live trading disabled on this host"
    try:
        from core.tiers import require_live
        require_live((ud or {}).get("tier", "Free"))
    except PermissionError as e:
        return False, str(e)
    except ImportError:
        # Fail closed: a missing tier module must not wave anyone through.
        return False, "live trading unavailable: tier check failed"
    try:
        from brokers import live_trading_enabled
        if not live_trading_enabled():
            return False, "live trading disabled by broker configuration"
    except ImportError:
        return False, "live trading unavailable: broker layer not loaded"
    return True, ""


def _make_agent_executor(conn, ud, mode):
    """Build the callable that lets an armed agent place an order.

    Returns a function taking a Proposal and returning an order id. It
    is the *only* path from an agent to the book, and it routes through
    the same risk check and fill logic a human click uses -- an agent
    gets no privileged route.
    """
    def _exec(proposal):
        return _dispatch_order(conn, ud, proposal, mode)
    return _exec


def _dispatch_order(conn, ud, proposal, mode):
    """Place one order for an agent. Returns {ok, id, error}.

    Mirrors the validation and risk gate of _order_create. It is a
    separate function rather than a call into the HTTP handler because
    an agent has no request to come in on.
    """
    symbol = str(getattr(proposal, "symbol", "") or "").strip().upper()
    side = str(getattr(proposal, "side", "") or "").strip().lower()
    try:
        qty = float(getattr(proposal, "qty", 0) or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "qty must be numeric"}
    if not symbol:
        return {"ok": False, "error": "symbol required"}
    if side not in ("buy", "sell"):
        return {"ok": False, "error": "side must be buy|sell"}
    if qty <= 0:
        return {"ok": False, "error": "qty must be > 0"}
    order = {"symbol": symbol, "side": side, "qty": qty, "type": "market",
             "price": None, "mode": mode}
    allow, reason = _engine_risk_check(conn, ud["id"], order)
    if not allow:
        _risk_event(conn, ud["id"], "agent_order_reject", reason, order)
        return {"ok": False, "error": "risk reject: {}".format(reason)}
    import secrets as _s
    cur = conn.execute(
        "INSERT INTO orders (user_id, symbol, side, type, qty, status, mode,"
        " client_order_id) VALUES (?, ?, ?, 'market', ?, 'open', ?, ?)",
        (ud["id"], symbol, side, qty, mode, _s.token_hex(8)))
    oid = cur.lastrowid
    conn.commit()
    try:
        _apply_paper_fill(conn, ud["id"], oid, order)
    except Exception as e:
        conn.execute("UPDATE orders SET status = 'rejected' WHERE id = ?", (oid,))
        conn.commit()
        _risk_event(conn, ud["id"], "agent_fill_error", str(e), order)
        return {"ok": False, "error": "fill failed: {}".format(e)}
    _audit(conn, ud["id"], "agent.order", ud.get("tier", ""), mode,
           {"order_id": oid, "order": order,
            "agent": getattr(proposal, "agent_id", "")})
    return {"ok": True, "id": oid}


class Router:
    """Route http.server handler requests. dispatch(handler) -> bool."""

    def dispatch(self, handler):
        try:
            method = (handler.command or "").upper()
            raw_path = handler.path or "/"
            parsed = urllib.parse.urlparse(raw_path)
            path = parsed.path or "/"
            qs = urllib.parse.parse_qs(parsed.query or "")
        except Exception:
            return False
        conn = _db_connect()
        try:
            return self._route(conn, handler, method, path, qs)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # -- routing table -------------------------------------------------
    def _route(self, conn, h, method, path, qs):
        if path.startswith("/api/"):
            return self._route_api(conn, h, method, path, qs)
        return False

    def _route_api(self, conn, h, method, path, qs):
        # service status (public: ops-friendly, no sensitive data)
        if path == "/api/status" and method == "GET":
            return self._status(conn, h)
        # market news (free public RSS, cached)
        if path == "/api/news" and method == "GET":
            return self._news(conn, h, qs)
        # auth
        if path == "/api/auth/signup" and method == "POST":
            return self._signup(conn, h)
        if path == "/api/auth/login" and method == "POST":
            return self._login(conn, h)
        if path == "/api/auth/logout" and method == "POST":
            return self._logout(conn, h)
        if path == "/api/auth/2fa" and method == "GET":
            return self._auth_totp(conn, h)
        if path == "/api/auth/2fa/setup" and method == "POST":
            return self._auth_totp(conn, h, action="setup")
        if path == "/api/auth/2fa/enable" and method == "POST":
            return self._auth_totp(conn, h, action="enable")
        if path == "/api/auth/2fa/disable" and method == "POST":
            return self._auth_totp(conn, h, action="disable")
        if path == "/api/chains" and method == "GET":
            return self._chains(h)
        if path == "/api/markets" and method == "GET":
            return self._markets(conn, h, qs)
        if path == "/api/me/entitlements" and method == "GET":
            return self._entitlements(conn, h)
        # market data
        if path == "/api/symbols" and method == "GET":
            return self._symbols(conn, h)
        if path == "/api/quote" and method == "GET":
            return self._quote(conn, h, qs)
        if path == "/api/quotes" and method == "GET":
            return self._quotes_all(conn, h, qs)
        if path == "/api/allocation" and method == "GET":
            return self._allocation(conn, h, qs)
        if path == "/api/candles" and method == "GET":
            return self._candles(conn, h, qs)
        if path == "/api/screener" and method == "GET":
            return self._screener(conn, h, qs)
        if path == "/api/exchanges" and method == "GET":
            return self._exchanges(conn, h)
        if path == "/api/analysis" and method == "GET":
            return self._analysis(conn, h, qs)
        if path == "/api/anomalies" and method == "GET":
            return self._anomalies(conn, h, qs)
        if path == "/api/chart_context" and method == "GET":
            return self._chart_context(conn, h, qs)
        # arbitrage scanner (crypto / fx / commodity). Auth + tier gated.
        if path == "/api/arbitrage" and method == "GET":
            return self._arbitrage(conn, h, qs)
        if path == "/api/arbitrage/venues" and method == "GET":
            return self._arbitrage_venues(conn, h)
        # watchlists
        if path == "/api/watchlists" and method == "GET":
            return self._wl_list(conn, h)
        if path == "/api/watchlists" and method == "POST":
            return self._wl_create(conn, h)
        if path.startswith("/api/watchlists/") and method == "DELETE":
            return self._wl_delete(conn, h, path)
        # strategies / signals
        if path == "/api/strategies" and method == "GET":
            return self._strategies(conn, h)
        if path == "/api/signals/preview" and method == "POST":
            return self._signals_preview(conn, h)
        # CFTC Commitments of Traders
        if path == "/api/cot" and method == "GET":
            return self._cot_markets(conn, h, qs)
        if path == "/api/cot/contract" and method == "GET":
            return self._cot_contract(conn, h, qs)
        if path == "/api/cot/history" and method == "GET":
            return self._cot_history(conn, h, qs)
        if path == "/api/cot/sync" and method == "POST":
            return self._cot_sync(conn, h)
        # openterminal
        if path == "/api/terminal/command" and method == "POST":
            return self._terminal_command(conn, h)
        # broker layer
        if path == "/api/brokers" and method == "GET":
            return self._brokers_list(conn, h)
        if path == "/api/brokers/command" and method == "POST":
            return self._broker_command(conn, h)
        # backtests
        if path == "/api/backtests" and method == "POST":
            return self._bt_create(conn, h)
        if path == "/api/backtests/validate" and method == "POST":
            return self._bt_validate(conn, h)
        if path == "/api/backtests" and method == "GET":
            return self._bt_list(conn, h, qs)
        if path.startswith("/api/backtests/") and method == "GET":
            return self._bt_get(conn, h, path)
        # orders / portfolio
        if path == "/api/orders" and method == "POST":
            return self._order_create(conn, h)
        if path == "/api/orders" and method == "GET":
            return self._order_list(conn, h, qs)
        if path.startswith("/api/orders/") and method == "DELETE":
            return self._order_cancel(conn, h, path)
        if path == "/api/positions/flatten" and method == "POST":
            return self._flatten(conn, h)
        if path == "/api/portfolio" and method == "GET":
            return self._portfolio(conn, h)
        # risk
        if path == "/api/risk" and method == "GET":
            return self._risk_get(conn, h)
        if path == "/api/risk" and method == "POST":
            return self._risk_set(conn, h)
        # agents (agentic trading)
        if path == "/api/agents" and method == "GET":
            return self._agents_list(conn, h)
        if path == "/api/agents" and method == "POST":
            return self._agent_create(conn, h)
        if path == "/api/agents/tick" and method == "POST":
            return self._agent_tick(conn, h)
        if path.startswith("/api/agents/") and method == "POST":
            return self._agent_action(conn, h, path)
        # journal
        if path == "/api/journal" and method == "GET":
            return self._journal_list(conn, h)
        if path == "/api/journal" and method == "POST":
            return self._journal_add(conn, h)
        # admin
        if path == "/api/admin/overview" and method == "GET":
            return self._admin_overview(conn, h)
        if path == "/api/admin/users" and method == "GET":
            return self._admin_users(conn, h)
        if path == "/api/admin/audit" and method == "GET":
            return self._admin_audit(conn, h, qs)
        _err(h, "not found: {} {}".format(method, path), 404)
        return True

    # -- 2FA (TOTP) -----------------------------------------------------
    def _auth_totp(self, conn, h, action=None):
        """GET  /api/auth/2fa  -> status
        POST /api/auth/2fa/setup    -> secret + otpauth URI (once)
        POST /api/auth/2fa/enable   {code} -> verifies, flips on
        POST /api/auth/2fa/disable  {code} -> verifies, flips off"""
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        ud = _row_to_dict(user)
        try:
            from core import authz as az
        except Exception:
            return _err(h, "2FA unavailable", 503)
        if action is None:
            return _send_json(h, {
                "enabled": int(ud.get("totp_enabled") or 0) == 1,
                "recovery_left": len(_safe_json(ud.get("recovery_codes", "[]"))),
            })
        if not self._require_csrf(conn, h, sess):
            return True
        if action == "setup":
            secret = az.new_totp_secret()
            conn.execute("UPDATE users SET totp_secret = ?, totp_enabled = 0"
                         " WHERE id = ?", (secret, ud["id"]))
            conn.commit()
            _audit(conn, ud["id"], "auth.2fa_setup", ud.get("tier", ""), "paper", {})
            return _send_json(h, {
                "secret": secret,
                "otpauth_uri": az.otpauth_url(secret, ud.get("email", "")),
                "digits": az.TOTP_DIGITS, "period": az.TOTP_STEP,
            })
        data, err = _read_json(h)
        if err:
            return _err(h, err)
        code = str((data or {}).get("code", "")).strip()
        if action == "enable":
            if not az.verify_totp(ud.get("totp_secret", ""), code):
                return _err(h, "invalid code", 400)
            codes = az.new_recovery_codes()
            conn.execute(
                "UPDATE users SET totp_enabled = 1, recovery_codes = ? WHERE id = ?",
                (_dump_json(codes), ud["id"]))
            conn.commit()
            _audit(conn, ud["id"], "auth.2fa_enabled", ud.get("tier", ""), "paper", {})
            return _send_json(h, {"enabled": True, "recovery_codes": codes})
        if action == "disable":
            if int(ud.get("totp_enabled") or 0):
                matched, _rem = az.verify_recovery_code(
                    ud.get("recovery_codes", "[]"), code)
                if not az.verify_totp(ud.get("totp_secret", ""), code) and not matched:
                    return _err(h, "invalid code", 400)
            conn.execute("UPDATE users SET totp_enabled = 0, totp_secret = '',"
                         " recovery_codes = '[]' WHERE id = ?", (ud["id"],))
            conn.commit()
            _audit(conn, ud["id"], "auth.2fa_disabled", ud.get("tier", ""), "paper", {})
            return _send_json(h, {"enabled": False})
        return _err(h, "unknown 2FA action", 404)

    # -- wallet chain registry ------------------------------------------
    def _chains(self, h):
        """Supported EVM chains and their USD₮ contract addresses.

        Public, unauthenticated and static: chain ids, display names, block
        explorer links and token contract addresses. It contains no
        credentials and no RPC endpoint, because the browser reads on-chain
        state through the user's own wallet provider -- this server never
        sees a key and never relays anyone else's traffic.

        Served from data.chains so the contract addresses have exactly one
        home. A copy in JavaScript is a copy that goes stale.
        """
        try:
            from data.chains import list_chains
        except Exception as e:
            return _err(h, "chain registry unavailable: {}".format(e), 503)
        return _send_json(h, {
            "chains": list_chains(),
            "token": "USDT",
            "read_only": True,
            "notes": (
                "Read-only. The server holds no keys and cannot sign or "
                "send. Balances are read via the connected wallet's own "
                "provider, and decimals() is read from the contract rather "
                "than assumed (it is 6 on Ethereum but 18 on BNB Chain)."),
        })

    # -- global markets config -----------------------------------------
    def _markets(self, conn, h, qs):
        """Global markets: region config + live session status per region
        + venue counts drawn from the DB."""
        try:
            from core.markets import all_markets, market_hours, region_for_venue
        except Exception as e:
            return _err(h, "markets config unavailable: {}".format(e), 503)
        try:
            venues = []
            counts = {}
            try:
                rows = conn.execute("SELECT code, name, type, asset_classes,"
                                    " region, currency, tz FROM exchanges"
                                    " ORDER BY code").fetchall()
                for r in rows:
                    d = _row_to_dict(r)
                    rc = region_for_venue(d.get("code", ""), d.get("region", ""))
                    if rc:
                        counts[rc] = counts.get(rc, 0) + 1
                    venues.append({
                        "code": d.get("code"), "name": d.get("name", ""),
                        "type": d.get("type", ""), "region": rc,
                        "asset_classes": [x for x in
                                          str(d.get("asset_classes", "")).split(",")
                                          if x],
                        "currency": d.get("currency", "USD"),
                        "tz": d.get("tz", "UTC"),
                    })
            except Exception:
                pass
            regions = all_markets()
            for rg in regions:
                st = market_hours(rg["code"])
                rg["open"] = st["open"]
                rg["venues_tracked"] = counts.get(rg["code"], 0)
            return _send_json(h, {
                "regions": regions, "venues": venues,
                "config_path": "core/markets.py",
            })
        except Exception as e:
            return _err(h, "markets failed: {}".format(e), 500)

    # -- auth helpers --------------------------------------------------
    def _require_user(self, conn, h):
        user, sess = _get_user_and_session(conn, h)
        if user is None:
            _err(h, "unauthorized", 401)
            return None, None
        return user, sess

    def _require_csrf(self, conn, h, sess):
        if not _check_csrf(h, sess):
            _err(h, "csrf mismatch", 403)
            return False
        return True

    def _require_admin(self, conn, h, user):
        try:
            ud = _row_to_dict(user)
            if str(ud.get("tier", "")).lower() == "admin":
                return True
        except Exception:
            pass
        _err(h, "admin only", 403)
        return False

    # -- auth endpoints ------------------------------------------------
    def _signup(self, conn, h):
        data, err = _read_json(h)
        if err:
            return _err(h, err)
        email = str((data or {}).get("email", "")).strip().lower()
        password = str((data or {}).get("password", ""))
        if "@" not in email or len(email) < 3:
            return _err(h, "invalid email")
        if len(password) < 8:
            return _err(h, "password must be >= 8 chars")
        # ---- throttle signup per ip: stops mass account creation ------
        ip = _client_ip(h)
        # Per-IP signup throttle. Fails CLOSED for the same reason as
        # login: an unevaluable control must not become no control.
        signup_key = "ip:signup:{}".format(ip or "-")
        signup_policy = {"max_attempts": 10, "window": 3600, "base": 60}
        try:
            from core.authz import Throttle
            left = Throttle(conn, signup_key, **signup_policy).check()
            if left > 0:
                _audit(conn, None, "auth.signup_throttled", "", "paper",
                       {"ip": ip, "retry_in": left})
                return _err(h, "too many signups from this address — "
                               "retry in {}s".format(left), 429)
        except Exception as e:
            _audit(conn, None, "auth.throttle_unavailable", "", "paper",
                   {"where": "signup", "error": str(e)})
            return _err(h, "signup temporarily unavailable", 503)
        try:
            ex = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if ex is not None:
                # Counted with the SAME policy the check used.
                try:
                    Throttle(conn, signup_key, **signup_policy).fail()
                except Exception as e:
                    _audit(conn, None, "auth.throttle_unavailable", "", "paper",
                           {"where": "signup-record", "error": str(e)})
                return _err(h, "email already registered", 409)
            fns = _load("core.auth", ["pbkdf2_hash", "create_session"])
            pw = fns.get("pbkdf2_hash")
            if pw is None:
                import hashlib, secrets as _s
                salt = _s.token_bytes(16)
                dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
                pwh = "pbkdf2_sha256$200000${}${}".format(salt.hex(), dk.hex())
            else:
                pwh = pw(password)
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, tier) VALUES (?, ?, 'Free')",
                (email, pwh))
            uid = cur.lastrowid
            cs = fns.get("create_session")
            if cs is None:
                import secrets as _s2
                token = _s2.token_urlsafe(32)
                csrf = _s2.token_urlsafe(32)
                conn.execute(
                    "INSERT INTO sessions (token, user_id, created_at, expires_at, csrf_token)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (token, uid, int(time.time()), int(time.time()) + 86400, csrf))
                conn.commit()
            else:
                token, csrf, _exp = cs(conn, uid)
                conn.commit() if hasattr(conn, "commit") else None
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO risk_limits (user_id) VALUES (?)", (uid,))
                conn.commit()
            except Exception:
                pass
            # ---- full account provisioning: risk limits, default
            # watchlist, starter strategies. Same code path the seeder
            # uses, so a signup is as complete as a seeded account.
            prov = {}
            try:
                from core.provision import provision
                prov = provision(conn, uid, "Free")
            except Exception as e:
                prov = {"error": str(e)}
            _audit(conn, uid, "auth.signup", "Free", "paper",
                   {"email": email, "provisioned": bool(prov.get("watchlist"))})
            cookie = _set_session_cookie(h, token)
            if _wants_html(h):
                return _redirect(h, "/#/dashboard", cookie)
            return _send_json(h, {"user": {"id": uid, "email": email, "tier": "Free"},
                                  "csrf": csrf, "csrf_token": csrf,
                                  "provisioned": prov},
                                  extra_headers=cookie)
        except Exception as e:
            msg = str(e)
            if "UNIQUE" in msg or "unique" in msg:
                return _err(h, "email already registered", 409)
            return _err(h, "signup failed", 500)

    def _login(self, conn, h):
        data, err = _read_json(h)
        if err:
            return _err(h, err)
        email = str((data or {}).get("email", "")).strip().lower()
        password = str((data or {}).get("password", ""))
        code = str((data or {}).get("code", (data or {}).get("totp", ""))).strip()
        ip = _client_ip(h)
        # ---- brute-force throttle: identity AND ip scoped, exponential lock
        #
        # Fails CLOSED. If throttling cannot be evaluated we deny the
        # attempt rather than serve unlimited logins -- a broken control
        # must not silently become no control.
        try:
            from core.authz import Throttle, throttle_keys
        except Exception as e:
            _audit(conn, None, "auth.throttle_unavailable", "", "paper",
                   {"where": "login", "error": str(e)})
            return _err(h, "login temporarily unavailable", 503)
        for key in throttle_keys(email, ip):
            try:
                left = Throttle(conn, key).check()
            except Exception as e:
                _audit(conn, None, "auth.throttle_unavailable", "", "paper",
                       {"where": "login", "key": key, "error": str(e)})
                return _err(h, "login temporarily unavailable", 503)
            if left > 0:
                _audit(conn, None, "auth.login_throttled", "", "paper",
                       {"email": email, "retry_in": left})
                return _err(h, "too many login attempts — retry in {}s".format(left), 429)
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        fns = _load("core.auth", ["verify", "create_session"])
        vf = fns.get("verify")
        ok = False
        if row is not None:
            rd0 = _row_to_dict(row)
            if not int(rd0.get("disabled") or 0):
                try:
                    ok = vf(password, rd0.get("password_hash", "")) if vf else False
                except Exception:
                    ok = False
        if not ok:
            # Recorded with the same policy the check used, so the
            # recorded history and the check agree.
            for key in throttle_keys(email, ip):
                try:
                    Throttle(conn, key).fail()
                except Exception as e:
                    _audit(conn, None, "auth.throttle_unavailable", "", "paper",
                           {"where": "record", "key": key, "error": str(e)})
            if row is not None:
                try:
                    conn.execute("UPDATE users SET failed_logins = failed_logins + 1"
                                 " WHERE id = ?", (_row_to_dict(row)["id"],))
                    conn.commit()
                except Exception:
                    pass
            _audit(conn, None, "auth.login_failed", "", "paper", {"email": email})
            # generic message: never reveal whether the account exists
            return _err(h, "invalid credentials", 401)
        rd = _row_to_dict(row)
        # ---- second factor (TOTP or a single-use recovery code)
        if int(rd.get("totp_enabled") or 0):
            # Fails CLOSED: if the 2FA module cannot be loaded we must not
            # fall through to "password was enough". Skipping the second
            # factor on an internal error would turn a transient import
            # failure into a full account takeover path.
            try:
                from core import authz as _az
            except Exception as e:
                _audit(conn, rd["id"], "auth.twofa_unavailable", "", "paper",
                       {"error": str(e)})
                return _err(h, "two-factor verification unavailable", 503)
            twofa = None
            secret = rd.get("totp_secret", "")
            if code and _az.verify_totp(secret, code):
                twofa = "totp"
            elif code:
                matched, remaining = _az.verify_recovery_code(
                    rd.get("recovery_codes", "[]"), code)
                if matched:
                    twofa = "recovery"
                    conn.execute("UPDATE users SET recovery_codes = ? WHERE id = ?",
                                 (remaining, rd["id"]))
                    conn.commit()
            if twofa is None:
                # password was right but the 2nd factor is not: do NOT count
                # toward the lockout (a stolen password must not be able to
                # lock the owner out) — just demand the code.
                _audit(conn, rd["id"], "auth.2fa_required", rd.get("tier", ""),
                       "paper", {"email": email})
                return _err(h, "two-factor code required", 401,
                             extra={"twofa_required": True})
        if Throttle is not None:
            for key in throttle_keys(email, ip):
                Throttle(conn, key).ok()
        try:
            conn.execute("UPDATE users SET failed_logins = 0, last_login_ts = ?"
                         " WHERE id = ?", (int(time.time()), rd["id"]))
            conn.commit()
        except Exception:
            pass
        cs = fns.get("create_session")
        if cs is None:
            import secrets as _s2
            token = _s2.token_urlsafe(32)
            csrf = _s2.token_urlsafe(32)
            conn.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at, csrf_token)"
                " VALUES (?, ?, ?, ?, ?)",
                (token, rd["id"], int(time.time()), int(time.time()) + 86400, csrf))
            conn.commit()
        else:
            token, csrf, _exp = cs(conn, rd["id"])
        _audit(conn, rd["id"], "auth.login", rd.get("tier", ""), "paper", {"email": email})
        cookie = _set_session_cookie(h, token)
        if _wants_html(h):
            return _redirect(h, "/#/dashboard", cookie)
        return _send_json(h, {"user": {"id": rd["id"], "email": rd["email"],
                                       "tier": rd.get("tier", "Free")},
                              "csrf": csrf, "csrf_token": csrf},
                          extra_headers=cookie)

    def _logout(self, conn, h):
        user, sess = _get_user_and_session(conn, h)
        if sess is not None and not _check_csrf(h, sess):
            return _err(h, "csrf mismatch", 403)
        token = _session_token(h)
        try:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
        except Exception:
            pass
        if user is not None:
            _audit(conn, _row_to_dict(user).get("id"), "auth.logout",
                   _row_to_dict(user).get("tier", ""), "paper", {})
        return _send_json(h, {"ok": True},
                          extra_headers={"Set-Cookie": "session=; Path=/; Max-Age=0; {}"
                                         .format(_session_cookie_flags())})

    def _entitlements(self, conn, h):
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        ud = _row_to_dict(user)
        try:
            from core.tiers import get_entitlements
            ent = get_entitlements(ud.get("tier", "Free"))
        except Exception:
            ent = {"tier": ud.get("tier", "Free")}
        ent["user"] = {"id": ud.get("id"), "email": ud.get("email"), "tier": ud.get("tier")}
        try:
            if _s is not None:
                sd = _row_to_dict(_s)
                ent["csrf"] = sd.get("csrf_token", "")
                ent["csrf_token"] = sd.get("csrf_token", "")
        except Exception:
            pass
        return _send_json(h, ent)

    # -- market data ---------------------------------------------------
    def _symbols(self, conn, h):
        try:
            rows = conn.execute("SELECT * FROM symbols ORDER BY symbol").fetchall()
            return _send_json(h, {"symbols": [_row_to_dict(r) for r in rows]})
        except Exception as e:
            return _err(h, "symbols failed: {}".format(e), 500)

    def _allocation(self, conn, h, qs):
        """Capital allocation from the account's own measured history.

        Reads the user's real fills, derives win rate, profit/loss ratio
        and net-of-cost edge per symbol, and returns a sized book. The
        load-bearing part is that a symbol with no positive net edge
        comes back at size zero with the reason attached -- the endpoint
        is expected to recommend doing nothing most of the time, and it
        says so per row rather than quietly omitting the row.
        """
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        ud = _row_to_dict(user)
        try:
            equity = float(conn.execute(
                "SELECT cash FROM accounts WHERE user_id = ?",
                (ud["id"],)).fetchone()[0] or 0.0)
        except Exception:
            equity = 0.0
        try:
            equity += sum(
                float(_row_to_dict(r).get("qty") or 0)
                * float(_row_to_dict(r).get("last") or _row_to_dict(r).get("avg_price") or 0)
                for r in conn.execute(
                    "SELECT * FROM positions WHERE user_id = ? AND qty != 0",
                    (ud["id"],)).fetchall())
        except Exception:
            pass
        try:
            costs_bps = float((qs.get("costs_bps", ["4"])[0]) or 4)
        except (TypeError, ValueError):
            costs_bps = 4.0
        costs_bps = max(0.0, min(costs_bps, 500.0))
        # Pair up fills per symbol so a round trip is one observation.
        candidates = []
        try:
            rows = conn.execute(
                "SELECT * FROM fills WHERE user_id = ? ORDER BY id", (ud["id"],)
            ).fetchall()
        except Exception as e:
            return _err(h, "fills failed: {}".format(e), 500)
        by_symbol = {}
        for r in rows:
            fd = _row_to_dict(r)
            by_symbol.setdefault(str(fd.get("symbol", "")).upper(), []).append(fd)
        from engines.allocation import (allocate_book, edge_per_unit_risk,
                                        profit_loss_ratio, win_rate)
        for sym, fills in sorted(by_symbol.items()):
            if not sym:
                continue
            legs, open_leg = [], None
            for f in fills:
                q = float(f.get("qty") or 0)
                p = float(f.get("price") or 0)
                fee = float(f.get("fee") or 0)
                n_val = abs(q) * p
                if q > 0:
                    open_leg = {"qty": q, "price": p}
                elif q < 0 and open_leg is not None:
                    take = min(-q, open_leg["qty"])
                    legs.append({
                        "pnl": (p - open_leg["price"]) * take - fee,
                        "notional": take * p})
                    open_leg["qty"] -= take
                    if open_leg["qty"] <= 1e-12:
                        open_leg = None
            if not legs:
                continue
            wp, n = win_rate(legs)
            ratio = profit_loss_ratio(legs)
            edge = edge_per_unit_risk({}, legs, costs_bps=costs_bps)
            # Per-trade return volatility, for volatility targeting.
            rets = [l["pnl"] / l["notional"] for l in legs if l["notional"] > 0]
            vol = 0.0
            if len(rets) > 1:
                m = sum(rets) / len(rets)
                vol = (sum((x - m) ** 2 for x in rets) / len(rets)) ** 0.5
            candidates.append({
                "symbol": sym, "win_rate": wp, "ratio": ratio,
                "n_samples": n, "edge": edge,
                "trades_list": legs,
                "asset_vol_pct": vol * 100.0 if vol > 0 else 1.0,
            })
        book = allocate_book(equity, candidates, costs_bps=costs_bps,
                             target_vol_pct=1.0, max_fraction=0.25,
                             max_single=0.25)
        book["costs_bps"] = costs_bps
        book["symbols_measured"] = len(candidates)
        book["disclaimer"] = (
            "Size zero means the measured edge does not cover costs or is "
            "based on too few trades. That is the intended answer, not a "
            "missing calculation.")
        _audit(conn, ud["id"], "allocation.compute", ud.get("tier", ""), "paper",
               {"symbols": len(candidates), "deployed_pct": book["deployed_pct"]})
        return _send_json(h, book)

    def _quotes_all(self, conn, h, qs):
        """Latest quote for every enabled symbol, one row per symbol.

        The assets browser on Dashboard/Portfolio needs six fields per
        symbol and nothing else. /api/screener returns the full analysis
        row *including* a 200-element closes array per symbol -- 208KB and
        ~2.7s for 39 symbols, of which the browser used none. This is the
        same information for a fraction of the cost.
        """
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        try:
            syms = conn.execute(
                "SELECT symbol, name, asset_type, exchange FROM symbols"
                " WHERE enabled = 1 ORDER BY symbol").fetchall()
        except Exception as e:
            return _err(h, "symbols failed: {}".format(e), 500)
        out = []
        for r in syms:
            d = _row_to_dict(r)
            sym = str(d.get("symbol") or "").upper()
            if not sym:
                continue
            item = {"symbol": sym, "name": d.get("name", ""),
                    "asset_type": d.get("asset_type", ""),
                    "exchange": d.get("exchange", "")}
            try:
                q = conn.execute(
                    "SELECT bid, ask, last, ts, source FROM quotes_l1"
                    " WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
                    (sym,)).fetchone()
            except Exception:
                q = None
            if q is not None:
                qd = _row_to_dict(q)
                item["last"] = qd.get("last")
                item["bid"] = qd.get("bid")
                item["ask"] = qd.get("ask")
                item["source"] = qd.get("source", "")
                try:
                    bid, ask = float(qd.get("bid") or 0), float(qd.get("ask") or 0)
                    mid = (bid + ask) / 2.0
                    if mid > 0:
                        item["spread_bps"] = round((ask - bid) / mid * 10000.0, 3)
                except (TypeError, ValueError):
                    pass
            try:
                c = conn.execute(
                    "SELECT close FROM candles_1m WHERE symbol = ?"
                    " ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
                if c is not None:
                    item["prev_close"] = float(c[0] or 0) or None
            except Exception:
                pass
            last = item.get("last")
            prev = item.get("prev_close")
            try:
                if last and prev and float(prev) > 0:
                    item["change_pct"] = round(
                        (float(last) - float(prev)) / float(prev) * 100.0, 3)
            except (TypeError, ValueError):
                pass
            try:
                v = conn.execute(
                    "SELECT volume FROM candles_1m WHERE symbol = ?"
                    " ORDER BY ts DESC LIMIT 1", (sym,)).fetchone()
                if v is not None:
                    item["volume"] = float(v[0] or 0)
            except Exception:
                pass
            out.append(item)
        return _send_json(h, {"quotes": out, "count": len(out)})

    def _quote(self, conn, h, qs):
        """Latest stored quote for one symbol.

        Read-only and side-effect free, so it needs no CSRF token. The
        Scalp Lab shows this next to the symbol input so the operator can
        confirm they are previewing the market they think they are.
        """
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        symbol = (qs.get("symbol", [""])[0] or "").strip().upper()
        if not symbol:
            return _err(h, "symbol required")
        try:
            r = conn.execute(
                "SELECT symbol, ts, bid, ask, last, bid_size, ask_size, source"
                " FROM quotes_l1 WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
                (symbol,)).fetchone()
        except Exception as e:
            return _err(h, "quote failed: {}".format(e), 500)
        if r is None:
            return _send_json(h, {"symbol": symbol, "last": None, "found": False})
        q = _row_to_dict(r)
        bid, ask = q.get("bid"), q.get("ask")
        try:
            mid = (float(bid) + float(ask)) / 2.0
            if mid > 0:
                q["spread_bps"] = round((float(ask) - float(bid)) / mid * 10000.0, 2)
        except (TypeError, ValueError):
            pass
        q["found"] = True
        return _send_json(h, q)

    def _candles(self, conn, h, qs):
        symbol = (qs.get("symbol", [""])[0] or "").strip()
        if not symbol:
            return _err(h, "symbol required")
        try:
            limit = int(qs.get("limit", ["200"])[0])
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(limit, 5000))

        # Resolution routing.
        #
        # `tf` was already being sent by the frontend (web/app.js) and was
        # being silently ignored, so every chart read candles_1m and the
        # daily bars fetched into candles_1d were invisible. This is a bug,
        # not a feature request: the parameter existed on one side only.
        #
        # The table name comes from a fixed map and is never interpolated
        # from user input -- a `tf` of `1m; DROP TABLE candles_1m` must not
        # reach the query. An unrecognised tf falls back to 1m rather than
        # erroring, because a chart that renders at the wrong resolution is
        # recoverable and one that fails to render is not; the response
        # reports the resolution actually used so the mismatch is visible.
        tf = (qs.get("tf", ["1m"])[0] or "1m").strip().lower()
        table = _CANDLE_TABLES.get(tf, "candles_1m")
        resolved = tf if tf in _CANDLE_TABLES else "1m"

        try:
            rows = conn.execute(
                "SELECT symbol, ts, open, high, low, close, volume FROM "
                + table + " WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
                (symbol, limit)).fetchall()
            out = [_row_to_dict(r) for r in rows][::-1]
            return _send_json(h, {"symbol": symbol, "tf": resolved,
                                  "table": table, "candles": out})
        except Exception as e:
            return _err(h, "candles failed: {}".format(e), 500)

    def _chart_context(self, conn, h, qs):
        """Per-bar regime + anomaly flags for the 1m chart.

        Separate from /api/anomalies, which is a cross-symbol screener --
        "which instruments look unusual right now" -- and cannot answer
        "what was the regime on the bar under the cursor".

        1m only, deliberately. The regime window and the z window are both
        bar counts, so on daily bars they would mean 60 days and 20 days:
        a different question from the one these thresholds answer. Serving
        tf=1d from here would pair daily bars with 1m-shaped constants and
        report the result as if it meant something.
        """
        symbol = (qs.get("symbol", [""])[0] or "").strip()
        if not symbol:
            return _err(h, "symbol required")
        try:
            limit = int(qs.get("limit", ["200"])[0])
        except (TypeError, ValueError):
            limit = 200
        limit = max(30, min(limit, 2000))
        try:
            rows = conn.execute(
                "SELECT ts, open, high, low, close, volume FROM candles_1m"
                " WHERE symbol = ? ORDER BY ts DESC LIMIT ?",
                (symbol, limit)).fetchall()
            candles = [_row_to_dict(r) for r in rows][::-1]
        except Exception as e:
            return _err(h, "chart context failed: {}".format(e), 500)
        if not candles:
            return _send_json(h, {"symbol": symbol, "n": 0, "series": [],
                                  "basis": "no-data"})
        try:
            from engines.anomaly import chart_context
            ctx = chart_context([c.get("close") for c in candles],
                                candles=candles)
        except Exception as e:
            return _err(h, "chart context failed: {}".format(e), 500)
        ctx["symbol"] = symbol
        return _send_json(h, ctx)

    def _screener_rows(self, conn, min_vol=0.0, max_spread=1e9,
                      exchange=None, asset_type=None, region=None):
        """Shared scanner: per-symbol analysis rows (used by screener,
        exchanges aggregation, and analysis). Returns list of dicts."""
        from engines.analysis import analyze_symbol
        sql = "SELECT * FROM symbols WHERE enabled = 1"
        params = []
        if exchange:
            sql += " AND exchange = ?"
            params.append(exchange)
        if asset_type:
            sql += " AND asset_type = ?"
            params.append(asset_type)
        # ---- global markets: restrict to a region (AMER/EMEA/APAC) -----
        region_code = None
        if region:
            try:
                from core.markets import normalize_region
                region_code = normalize_region(region)
            except Exception:
                region_code = None
        syms = conn.execute(sql, params).fetchall()
        if region_code:
            from core.markets import region_for_venue
            venues = conn.execute("SELECT code, region FROM exchanges").fetchall()
            allowed = {r[0] for r in venues
                       if region_for_venue(r[0], r[1]) == region_code}
            syms = [s for s in syms
                    if str(_row_to_dict(s).get("exchange", "")) in allowed]
        vol_by_sym = volume_by_symbol(conn)
        rows = []
        # COT contracts are resolved once per scan, not per symbol. The
        # resolution is a LIKE query against ~470 market names and the
        # scanner walks every enabled symbol, so doing it inline turned a
        # cheap query into 40 of them.
        cot_cache = _cot_resolver(conn)
        for s in syms:
            meta = _row_to_dict(s)
            sym = meta.get("symbol", "")
            qd = {}
            q = conn.execute("SELECT * FROM quotes_l1 WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
                             (sym,)).fetchone()
            if q is not None:
                qd = _row_to_dict(q)
            vol = vol_by_sym.get(sym, 0.0)
            closes = candles = []
            try:
                crows = conn.execute(
                    "SELECT close FROM candles_1m WHERE symbol = ?"
                    " ORDER BY ts DESC LIMIT 200", (sym,)).fetchall()
                closes = [float(r[0]) for r in crows][::-1]
            except Exception:
                closes = []
            try:
                crows = conn.execute(
                    "SELECT ts, open, high, low, close, volume FROM candles_1m"
                    " WHERE symbol = ? ORDER BY ts DESC LIMIT 200", (sym,)).fetchall()
                candles = [_row_to_dict(r) for r in crows][::-1]
            except Exception:
                candles = []
            if vol >= min_vol:
                an = analyze_symbol(sym, closes, qd, meta, candles,
                                    cot=cot_cache(sym))
                # region label travels with every row so the UI can group
                try:
                    from core.markets import region_for_venue
                    rgn = region_for_venue(meta.get("exchange", "")) or ""
                except Exception:
                    rgn = ""
                row = {"symbol": sym, "name": meta.get("name", ""),
                       "asset_type": meta.get("asset_type", ""),
                       "exchange": meta.get("exchange", ""),
                       "region": rgn,
                       "currency": meta.get("currency", "USD"),
                       "sector": meta.get("sector", ""),
                       "bid": _f(qd.get("bid")), "ask": _f(qd.get("ask")),
                       "last": _f(qd.get("last")), "volume": vol,
                       "spread_bps": an.get("spread_bps"),
                       "rsi14": an.get("rsi14"), "trend": an.get("trend"),
                       "trend_strength": an.get("trend_strength"),
                       "momentum_pct": an.get("momentum_pct"),
                       "session_pct": an.get("session_pct"),
                       "vol_ann_pct": an.get("vol_ann_pct"),
                       "atr_pct": an.get("atr_pct"),
                       "rel_volume": an.get("rel_volume"),
                       "score": an.get("score"), "bias": an.get("bias"),
                       "reason": an.get("reason"), "summary": an.get("summary"),
                       "hurst": an.get("hurst"), "regime": an.get("regime"),
                       "regime_description": an.get("regime_description"),
                       "tf_alignment": an.get("tf_alignment"),
                       "tf_verdict": an.get("tf_verdict"),
                       "timeframes": an.get("timeframes", []),
                       "anomaly_score": an.get("anomaly_score"),
                       "anomalies": an.get("anomalies", []),
                       "research_class": an.get("research_class"),
                       "drivers": an.get("drivers", []),
                       "leverage": an.get("leverage"),
                       "cot": an.get("cot"),
                       "closes": closes}
                if row["spread_bps"] is not None and row["spread_bps"] > max_spread:
                    continue
                rows.append(row)
        return rows

    def _screener(self, conn, h, qs):
        try:
            min_vol = float((qs.get("min_vol", ["0"])[0] or 0))
        except (TypeError, ValueError):
            min_vol = 0.0
        try:
            max_spread = float((qs.get("max_spread_bps", ["1000000"])[0] or 1000000))
        except (TypeError, ValueError):
            max_spread = 1e9
        exchange = (qs.get("exchange", [""])[0] or "").strip() or None
        asset_type = (qs.get("asset_type", [""])[0] or "").strip() or None
        region = (qs.get("region", [""])[0] or "").strip() or None
        try:
            out = self._screener_rows(conn, min_vol=min_vol, max_spread=max_spread,
                                      exchange=exchange, asset_type=asset_type,
                                      region=region)
            return _send_json(h, {"results": out})
        except Exception as e:
            return _err(h, "screener failed: {}".format(e), 500)

    def _exchanges(self, conn, h):
        """Venue intelligence: each exchange's asset mix, volume, avg
        spread, avg setup score, and highest-conviction symbol."""
        try:
            from engines.analysis import aggregate_exchanges
            rows = self._screener_rows(conn)
            agg = {a["exchange"]: a for a in aggregate_exchanges(rows)}
            meta_rows = conn.execute("SELECT * FROM exchanges ORDER BY code").fetchall()
            out = []
            for r in meta_rows:
                m = _row_to_dict(r)
                code = m["code"]
                a = agg.get(code) or {}
                out.append({
                    "code": code, "name": m.get("name", code),
                    "type": m.get("type", "exchange"),
                    "asset_classes": (m.get("asset_classes") or "").split(","),
                    "region": m.get("region", ""), "currency": m.get("currency", "USD"),
                    "tz": m.get("tz", "UTC"), "notes": m.get("notes", ""),
                    "symbol_count": a.get("symbol_count", 0),
                    "symbols": a.get("symbols", []),
                    "total_volume": a.get("total_volume", 0.0),
                    "avg_spread_bps": a.get("avg_spread_bps"),
                    "avg_score": a.get("avg_score"),
                    "top_symbol": a.get("top_symbol"),
                })
            return _send_json(h, {"exchanges": out})
        except Exception as e:
            return _err(h, "exchanges failed: {}".format(e), 500)

    def _analysis(self, conn, h, qs):
        """Deep single-symbol intelligence: technicals + commodity card +
        venue context + leverage notes."""
        symbol = (qs.get("symbol", [""])[0] or "").strip().upper()
        if not symbol:
            return _err(h, "symbol required")
        try:
            meta_row = conn.execute("SELECT * FROM symbols WHERE symbol = ?", (symbol,)).fetchone()
            if meta_row is None:
                return _err(h, "unknown symbol: {}".format(symbol), 404)
            rows = self._screener_rows(conn)
            hit = next((r for r in rows if r["symbol"] == symbol), None)
            if hit is None:
                # symbol exists but has no data yet — minimal analysis base
                hit = {"symbol": symbol, "closes": [], "spread_bps": None,
                       "score": None, "bias": "flat", "reason": "no market data yet",
                       "summary": "no market data yet"}
            meta = _row_to_dict(meta_row)
            ex = {}
            if meta.get("exchange"):
                er = conn.execute("SELECT * FROM exchanges WHERE code = ?",
                                  (meta["exchange"],)).fetchone()
                if er is not None:
                    ex = _row_to_dict(er)
            card = None
            try:
                from engines.analysis import COMMODITY_CARDS
                card = COMMODITY_CARDS.get(symbol)
            except Exception:
                card = None
            payload = {
                "analysis": hit,
                "symbol": {"symbol": symbol, "name": meta.get("name", ""),
                           "asset_type": meta.get("asset_type", ""),
                           "exchange": meta.get("exchange", ""),
                           "currency": meta.get("currency", "USD"),
                           "sector": meta.get("sector", ""),
                           "tick_size": meta.get("tick_size"),
                           "lot_size": meta.get("lot_size")},
                "exchange": ex if ex else None,
                "research": card,
            }
            return _send_json(h, payload)
        except Exception as e:
            return _err(h, "analysis failed: {}".format(e), 500)

    def _anomalies(self, conn, h, qs):
        """Anomaly radar: highest-anomaly-score instruments with their
        flagged events, sorted by severity. Returns scan-wide stats
        (scanned/flagged clean/kind breakdown) plus flagged results."""
        try:
            min_score = float((qs.get("min_score", ["1"])[0] or 1))
        except (TypeError, ValueError):
            min_score = 1.0
        try:
            show_all = (qs.get("all", ["0"])[0] or "0").strip().lower() in ("1", "true", "yes")
        except Exception:
            show_all = False
        try:
            rows = self._screener_rows(conn)
            flagged = [r for r in rows if (r.get("anomaly_score") or 0) >= min_score]
            flagged.sort(key=lambda r: (r.get("anomaly_score") or 0), reverse=True)
            clean = [r for r in rows if (r.get("anomaly_score") or 0) < min_score]
            kinds = {}
            for r in flagged:
                for a in (r.get("anomalies") or []):
                    k = a.get("kind") or "unknown"
                    kinds[k] = kinds.get(k, 0) + 1
            by_sev = {"high": 0, "medium": 0, "low": 0}
            for r in flagged:
                for a in (r.get("anomalies") or []):
                    s = a.get("severity") or "low"
                    if s in by_sev:
                        by_sev[s] += 1
            out = []
            for r in flagged:
                out.append({
                    "symbol": r["symbol"], "name": r.get("name", ""),
                    "asset_type": r.get("asset_type", ""),
                    "exchange": r.get("exchange", ""),
                    "last": r.get("last"), "score": r.get("score"),
                    "bias": r.get("bias"), "regime": r.get("regime"),
                    "tf_alignment": r.get("tf_alignment"),
                    "anomaly_score": r.get("anomaly_score"),
                    "anomalies": r.get("anomalies", []),
                    "summary": r.get("summary"),
                })
            if show_all:
                for r in clean:
                    out.append({
                        "symbol": r["symbol"], "name": r.get("name", ""),
                        "asset_type": r.get("asset_type", ""),
                        "exchange": r.get("exchange", ""),
                        "last": r.get("last"), "score": r.get("score"),
                        "bias": r.get("bias"), "regime": r.get("regime"),
                        "tf_alignment": r.get("tf_alignment"),
                        "anomaly_score": r.get("anomaly_score"),
                        "anomalies": r.get("anomalies", []),
                        "summary": r.get("summary"),
                    })
            payload = {
                "results": out, "total": len(out),
                "scanned": len(rows), "flagged": len(flagged),
                "clean": len(clean), "kinds": kinds, "by_severity": by_sev,
            }
            return _send_json(h, payload)
        except Exception as e:
            return _err(h, "anomalies failed: {}".format(e), 500)

    # -- watchlists ----------------------------------------------------
    def _wl_list(self, conn, h):
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        uid = _row_to_dict(user)["id"]
        rows = conn.execute("SELECT * FROM watchlists WHERE user_id = ? ORDER BY id",
                            (uid,)).fetchall()
        items = []
        for r in rows:
            d = _row_to_dict(r)
            try:
                d["symbols"] = json.loads(d.get("symbols_json", "[]"))
            except Exception:
                d["symbols"] = []
            items.append(d)
        return _send_json(h, {"watchlists": items})

    def _wl_create(self, conn, h):
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        data, err = _read_json(h)
        if err:
            return _err(h, err)
        data = data or {}
        name = str(data.get("name", "default"))[:64] or "default"
        syms = data.get("symbols", data.get("symbols_json", []))
        if isinstance(syms, str):
            try:
                syms = json.loads(syms)
            except Exception:
                syms = [syms]
        syms = list(syms or [])
        uid = _row_to_dict(user)["id"]
        cur = conn.execute("INSERT INTO watchlists (user_id, name, symbols_json)"
                           " VALUES (?, ?, ?)", (uid, name, json.dumps(syms)))
        conn.commit()
        return _send_json(h, {"id": cur.lastrowid, "name": name, "symbols": syms}, status=201)

    def _wl_delete(self, conn, h, path):
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        wid = path.rsplit("/", 1)[-1]
        try:
            wid_i = int(wid)
        except (TypeError, ValueError):
            return _err(h, "invalid id")
        uid = _row_to_dict(user)["id"]
        cur = conn.execute("DELETE FROM watchlists WHERE id = ? AND user_id = ?",
                           (wid_i, uid))
        conn.commit()
        if cur.rowcount == 0:
            return _err(h, "not found", 404)
        return _send_json(h, {"ok": True})

    # -- strategies / preview ------------------------------------------
    def _strategies(self, conn, h):
        """Canonical strategy catalogue.

        The `strategies` table stores the row identity (id, mode, tier)
        but its params_json drifts from what the engine actually reads --
        seeded rows carry only {"fast":9,"slow":21} and omit qty and the
        spread gate. So the engine's own STRATEGIES list is treated as
        the source of truth for parameters and descriptions, and the DB
        supplies id/mode/tier. The client renders from this response
        alone, which removes the hardcoded duplicate list that used to
        claim methods were wired when the engine had no logic for them.
        """
        try:
            rows = conn.execute("SELECT * FROM strategies ORDER BY id").fetchall()
            items = [_row_to_dict(r) for r in rows]
        except Exception as e:
            return _err(h, "strategies failed: {}".format(e), 500)
        canonical = {}
        try:
            from engines.signals import STRATEGIES as _CANON
            for s in _CANON:
                canonical[s.get("name")] = s
        except Exception:
            canonical = {}
        for it in items:
            name = it.get("name", "")
            can = canonical.get(name)
            # expose params as a real object, not a string
            params = None
            if can is not None and isinstance(can.get("params"), dict):
                params = dict(can["params"])
            else:
                try:
                    params = json.loads(it.get("params_json") or "{}")
                    if not isinstance(params, dict):
                        params = {}
                except Exception:
                    params = {}
            it["params"] = params or {}
            if can is not None:
                it["description"] = can.get("description", "")
                if can.get("display_name"):
                    it["display_name"] = can["display_name"]
                if can.get("tier_required"):
                    it["tier_required"] = can["tier_required"]
            it.setdefault("description", "")
        user, _s = _get_user_and_session(conn, h)
        tier = _row_to_dict(user).get("tier", "Free") if user else "Free"
        try:
            from core.tiers import can_use_strategy
            for it in items:
                try:
                    it["allowed"] = bool(can_use_strategy(tier, it.get("name", "")))
                except Exception:
                    it["allowed"] = True
        except Exception:
            for it in items:
                it["allowed"] = True
        return _send_json(h, {"strategies": items, "tier": tier})

    def _signals_preview(self, conn, h):
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        data, err = _read_json(h)
        if err:
            return _err(h, err)
        data = data or {}
        strategy = data.get("strategy", data.get("strategy_id", data.get("name", "")))
        symbol = str(data.get("symbol", "")).strip().upper()
        params = data.get("params", {})
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                params = {}
        # resolve strategy id -> name
        sname = strategy
        srow = None
        try:
            if isinstance(strategy, int) or (isinstance(strategy, str) and strategy.isdigit()):
                r = conn.execute("SELECT * FROM strategies WHERE id = ?",
                                 (int(strategy),)).fetchone()
                if r is not None:
                    srow = _row_to_dict(r)
                    sname = srow.get("name", "")
            elif strategy:
                r = conn.execute("SELECT * FROM strategies WHERE name = ?", (str(strategy),)).fetchone()
                if r is not None:
                    srow = _row_to_dict(r)
                    sname = srow.get("name", "")
        except Exception:
            pass
        if not sname or not symbol:
            return _err(h, "strategy and symbol required")
        # Tier gate first. Checking the stub flag before the entitlement
        # check let a Free user query a Pro strategy and get a 200 with
        # "stub" instead of the 403 that tells them to upgrade.
        ud = _row_to_dict(user)
        try:
            from core.tiers import require_strategy
            require_strategy(ud.get("tier", "Free"), sname)
        except PermissionError as e:
            return _err(h, str(e), 403)
        except ImportError:
            return _err(h, "tier entitlements unavailable", 503)
        if srow and str(srow.get("mode", "wired")) == "stub":
            return _send_json(h, {"signal": "flat", "side": None, "qty": None,
                                  "strategy": sname, "symbol": symbol,
                                  "reason": "no live logic for this method yet",
                                  "source": "stub", "stub": True})
        prev = _engine_preview(conn, sname, symbol, params or {})
        prev.setdefault("strategy", sname)
        prev.setdefault("symbol", symbol)
        return _send_json(h, prev)

    # -- openterminal ---------------------------------------------------
    def _news(self, conn, h, qs):
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        symbol = str((qs.get("symbol", [""])[0] or "")).strip().upper() or None
        try:
            limit = int(qs.get("limit", ["20"])[0])
        except (TypeError, ValueError):
            limit = 20
        try:
            from data.news import get_news
            return _send_json(h, get_news(symbol, limit))
        except Exception as e:
            return _send_json(h, {"items": [], "updated_at": 0,
                                  "stale": True, "symbol": symbol or "",
                                  "error": str(e)})

    def _status(self, conn, h):
        try:
            return _send_json(h, _status_payload(conn))
        except Exception as e:
            return _err(h, "status failed: {}".format(e), 500)

    def _terminal_command(self, conn, h):
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        data, err = _read_json(h)
        if err:
            return _err(h, err)
        cmd = str((data or {}).get("cmd", (data or {}).get("command", "")))
        if not cmd.strip():
            return _err(h, "cmd required")
        ud = _row_to_dict(user)
        try:
            from core.terminal import execute as _term_exec
            ok, output, tdata = _term_exec(conn, ud, cmd)
        except Exception as e:
            return _err(h, "terminal error: {}".format(e), 500)
        return _send_json(h, {"ok": bool(ok), "output": str(output),
                              "data": tdata})

    # -- broker layer ---------------------------------------------------
    def _brokers_list(self, conn, h):
        """Which brokers/venues are available, and what is safe right now.

        Never returns credential material -- only whether credentials are
        present and a non-reversible fingerprint, so an operator can
        confirm *which* key is configured without it ever leaving the host.

        Credential status is reported *per venue*. A single global status
        would hide the case that matters most in a multi-venue deployment:
        one venue configured and another silently borrowing its key.
        """
        try:
            from brokers import (available, describe, list_profiles,
                                 live_trading_enabled)
            from brokers.registry import credential_status
        except Exception as e:
            return _err(h, "broker layer unavailable: {}".format(e), 503)
        venues = []
        for name in available():
            try:
                status = credential_status(name)
            except Exception as e:
                status = {"venue": name, "configured": False,
                          "error": str(e)}
            venues.append(status)
        return _send_json(h, {
            "brokers": describe(),
            "profiles": list_profiles(),
            "credentials": {
                # Per-venue status. `any_configured` is a convenience
                # for a UI badge; the venues array is the truth.
                "venues": venues,
                "any_configured": any(v.get("configured") for v in venues),
            },
            "live_trading_enabled": live_trading_enabled(),
            "default": "paper",
            "notes": (
                "Paper is the default and is never live. A live venue "
                "additionally requires LIVE_TRADING_ENABLED=1, a tier with "
                "live_enabled, and an explicit confirm:true on the "
                "request. Broker commands over HTTP are never armed, so "
                "live trading is only reachable from an interactive "
                "session that typed `arm yes`."),
        })

    def _broker_command(self, conn, h):
        """Run one broker command from the UI/terminal.

        A fresh, never-armed `BrokerSession` is built per request, so the
        interactive `arm` gate can never be satisfied over HTTP. Live
        venues are therefore unreachable here unless the caller passes
        `confirm: true` *and* every other live gate passes, which is
        checked again inside the adapter.
        """
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        data, err = _read_json(h)
        if err:
            return _err(h, err)
        data = data or {}
        name = str(data.get("broker", "paper")).strip().lower() or "paper"
        cmd = str(data.get("cmd", data.get("command", "")))
        if not cmd.strip():
            return _err(h, "cmd required")
        ud = _row_to_dict(user)
        try:
            result = self._run_broker_command(conn, ud, name, cmd, data)
        except Exception as e:
            return _err(h, "broker error: {}".format(e), 500)
        return _send_json(h, result)

    def _run_broker_command(self, conn, ud, name, cmd, data=None):
        """Build a broker, run one command, return a JSON-safe payload.

        Shared with the terminal so both surfaces behave identically.
        """
        from brokers import get_broker, BrokerSession, BrokerError
        from brokers.base import live_trading_enabled, assert_live_allowed

        data = data or {}
        # A venue is only ever constructed as live when the caller says so
        # explicitly AND the environment allows it. `confirm` is read from
        # the request body, never from the command string.
        wants_live = bool(data.get("live"))
        if wants_live:
            # Fail loudly rather than silently downgrading to a simulated
            # venue: a caller who believes they are trading live and is
            # actually filling against paper data is the single most
            # dangerous outcome this layer could produce.
            try:
                assert_live_allowed(
                    name,
                    confirm=bool(data.get("confirm")),
                    tier_live_enabled=self._tier_live_enabled(conn, ud),
                )
            except BrokerError as e:
                return {"ok": False, "broker": name, "live": False,
                        "output": "live refused: {}".format(e),
                        "events": []}
        live = wants_live
        try:
            broker = get_broker(
                name,
                conn=conn,
                user_id=ud.get("id"),
                user=ud,
                live=live,
                confirm=bool(data.get("confirm")),
                tier_live_enabled=self._tier_live_enabled(conn, ud),
            )
        except BrokerError as e:
            return {"ok": False, "broker": name, "output": str(e),
                    "events": []}
        if live and not getattr(broker, "is_live", False):
            # Paper can never be live; never let a caller believe otherwise.
            return {"ok": False, "broker": name, "live": False,
                    "output": "live refused: {} is a simulated venue".format(
                        broker.venue),
                    "events": []}

        events = []
        session = BrokerSession(broker, on_event=events.append)
        # Never armed over HTTP: `arm` is refused unless the operator is
        # sitting in the interactive session and the env allows it.
        session._armed_until = 0.0
        output = session.handle(cmd)
        lowered = output.lower()
        ok = not (lowered.startswith("broker error")
                  or lowered.startswith("error:")
                  or lowered.startswith("invalid:"))
        return {
            "ok": ok,
            "broker": broker.name,
            "venue": broker.venue,
            "live": bool(broker.is_live),
            "output": str(output),
            "events": events,
            "health": broker.health(),
        }

    def _tier_live_enabled(self, conn, ud):
        """Whether this user's tier permits live trading (fail closed)."""
        try:
            row = conn.execute("SELECT live_enabled FROM tiers WHERE name = ?",
                               (ud.get("tier", "Free"),)).fetchone()
            return bool(row["live_enabled"]) if row is not None else False
        except Exception:
            return False

    # -- backtests ------------------------------------------------------
    def _arbitrage(self, conn, h, qs):
        """GET /api/arbitrage -- cross-venue / triangular / FX / commodity.

        Session required (the scan reveals which venues this deployment
        watches and what each is quoting) and Pro-tier required, because
        a free scan is a free feed of the whole venue map.

        Read-only. It never places an order: an edge is an observation
        about two books, and turning it into a trade is a separate,
        individually risk-checked decision via POST /api/orders.
        """
        user, _sess = self._require_user(conn, h)
        if user is None:
            return True
        ud = _row_to_dict(user)
        try:
            from core.tiers import require_entitlement
            require_entitlement(ud.get("tier", "Free"), "arbitrage")
        except PermissionError as e:
            return _err(h, str(e), 403)
        except ImportError:
            return _err(h, "tier entitlements unavailable", 503)
        asset_class = (qs.get("asset_class", ["all"])[0] or "all").strip().lower()
        symbol = (qs.get("symbol", [""])[0] or "").strip().upper()
        min_edge = qs.get("min_edge_bps", [None])[0]
        size = qs.get("size", [None])[0]
        try:
            payload = _engine_arbitrage(conn, ud, asset_class=asset_class,
                                        symbol=symbol, min_edge_bps=min_edge,
                                        size=size)
        except Exception as e:
            return _err(h, "arbitrage scan failed: {}".format(e), 500)
        payload["disclaimer"] = (
            "Edges are net of two legs of taker fees and are observations, "
            "not executions. Paper trading only. Not financial advice.")
        return _send_json(h, payload)

    def _arbitrage_venues(self, conn, h):
        """GET /api/arbitrage/venues -- the venue registry + fee schedule.

        Public and unauthenticated, unlike the scan itself: it is static
        configuration (venue names, asset class, taker fee in bps) with
        no prices and no user data. The UI needs it to render a fee
        footnote, and an unauthenticated page should not have to fake
        one.
        """
        try:
            from data.venuebooks import all_venues
            return _send_json(h, {"venues": all_venues()})
        except Exception as e:
            return _err(h, "venue registry unavailable: {}".format(e), 503)

    # -- CFTC Commitments of Traders -------------------------------------
    #
    # Weekly positioning, published three days after the Tuesday it
    # describes. This is not a price feed: every response carries the
    # report date and its age, because a COT number read on Monday
    # describes a market as it stood nine days earlier and treating it as
    # current is the fastest way to draw a wrong conclusion from correct
    # data.
    #
    # Treated as read-only + Pro-gated, like the arbitrage scanner: the
    # report reveals the whole CFTC universe's positioning, which is a
    # map of where large money sits across ~470 contracts. Free tier gets
    # the contract list and nothing more.

    def _cot_guard(self, conn, h, need_data=True):
        """Returns (user_row, None) or (None, already-answered)."""
        user, _s = self._require_user(conn, h)
        if user is None:
            return None, True
        ud = _row_to_dict(user)
        try:
            from core.tiers import tier_at_least
            if not tier_at_least(ud.get("tier", "Free"), "Pro"):
                return None, _err(
                    h, "COT positioning data requires the Pro tier", 403)
        except ImportError:
            return None, _err(h, "tier entitlements unavailable", 503)
        if need_data:
            try:
                from data import cotstore as _cs
                if not _cs.table_summary(conn):
                    return None, _err(
                        h, "no COT data stored yet -- run "
                           "`python3 scripts/ingest_cot.py weekly`", 503)
            except Exception:
                return None, _err(h, "COT store unavailable", 503)
        return ud, None

    def _cot_markets(self, conn, h, qs):
        ud, done = self._cot_guard(conn, h, need_data=False)
        if done:
            return True
        try:
            from data import cotstore as _cs
            report = str((qs.get("report", ["disagg_comb"])[0] or
                          "disagg_comb")).strip()
            q = (qs.get("q", [""])[0] or "").strip()
            if q:
                rows = _cs.search_markets(conn, q, report)
            else:
                try:
                    limit = max(1, min(500, int(qs.get("limit", ["200"])[0])))
                except (TypeError, ValueError):
                    limit = 200
                rows = _cs.list_markets(conn, report, limit=limit)
            return _send_json(h, {
                "report": report,
                "query": q,
                "count": len(rows),
                "markets": rows,
                "newest": _cs.latest_report_date(conn, report),
                "stored": _cs.table_summary(conn),
            })
        except Exception as e:
            return _err(h, "cot markets failed: {}".format(e), 500)

    def _cot_contract(self, conn, h, qs):
        ud, done = self._cot_guard(conn, h)
        if done:
            return True
        code = str((qs.get("contract", [""])[0] or "")).strip()
        if not code:
            return _err(h, "contract required")
        report = str((qs.get("report", ["disagg_comb"])[0] or
                      "disagg_comb")).strip()
        try:
            weeks = int(qs.get("weeks", ["156"])[0])
        except (TypeError, ValueError):
            weeks = 156
        try:
            from data import cotstore as _cs
            out = _cs.analyse_contract(conn, code, report=report, weeks=weeks)
            if not out.get("market"):
                return _err(h, "unknown contract {}".format(code), 404)
            return _send_json(h, out)
        except Exception as e:
            return _err(h, "cot contract failed: {}".format(e), 500)

    def _cot_history(self, conn, h, qs):
        ud, done = self._cot_guard(conn, h)
        if done:
            return True
        code = str((qs.get("contract", [""])[0] or "")).strip()
        if not code:
            return _err(h, "contract required")
        report = str((qs.get("report", ["disagg_comb"])[0] or
                      "disagg_comb")).strip()
        category = (qs.get("category", [""])[0] or "").strip() or None
        try:
            limit = max(1, min(1000, int(qs.get("limit", ["260"])[0])))
        except (TypeError, ValueError):
            limit = 260
        try:
            from data import cotstore as _cs
            series = _cs.contract_history(conn, code, report=report,
                                          category=category, limit=limit)
            return _send_json(h, {
                "contract_code": code, "report": report,
                "category": category, "points": len(series), "series": series,
            })
        except Exception as e:
            return _err(h, "cot history failed: {}".format(e), 500)

    def _cot_sync(self, conn, h):
        """Pull the current-week files.

        CSRF-gated and Pro-gated, and rate-limited by the fact that it is
        a synchronous network call to a government site: one report set at
        a time, and the CFTC is asked once per report per call. There is
        no "refresh everything, repeatedly" affordance here on purpose --
        the release is weekly, so hammering it buys nothing and is rude.
        """
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        ud, done = self._cot_guard(conn, h, need_data=False)
        if done:
            return True
        data, err = _read_json(h)
        if err:
            return _err(h, err)
        data = data or {}
        try:
            from data import cot as _c
            from data import cotstore as _cs
        except ImportError:
            return _err(h, "cot modules unavailable", 503)
        report = str(data.get("report", "") or "").strip()
        if report not in _c.REPORTS:
            return _err(h, "report must be one of: {}".format(
                ", ".join(sorted(_c.REPORTS))))
        cache = os.environ.get("COT_CACHE_DIR") or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "cotcache")
        res = _cs.ingest_weekly(conn, report, cache_dir=cache, max_age=0)
        _audit(conn, ud["id"], "cot.sync", ud.get("tier", ""), "paper",
               {"report": report, "written": res["written"]})
        if not res["ok"]:
            return _err(h, "CFTC sync failed: {}".format(
                res["error"] or "no rows parsed"), 502)
        return _send_json(h, {"report": report, "rows": res["written"],
                              "report_date": res["report_date"],
                              "synced_at": int(time.time())})

    def _bt_create(self, conn, h):
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        data, err = _read_json(h)
        if err:
            return _err(h, err)
        data = data or {}
        strat = data.get("strategy_id", data.get("strategy", ""))
        symbol = str(data.get("symbol", "")).strip().upper()
        params = data.get("params", {})
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except Exception:
                params = {}
        from_ts = int(data.get("from_ts", data.get("from", 0)) or 0)
        to_ts = int(data.get("to_ts", data.get("to", 0)) or 0)
        if not symbol:
            return _err(h, "symbol required")
        # resolve strategy
        sid = None
        sname = ""
        try:
            if isinstance(strat, int) or (isinstance(strat, str) and str(strat).isdigit()):
                r = conn.execute("SELECT * FROM strategies WHERE id = ?", (int(strat),)).fetchone()
                if r is not None:
                    d = _row_to_dict(r)
                    sid, sname = d.get("id"), d.get("name", "")
            elif strat:
                r = conn.execute("SELECT * FROM strategies WHERE name = ?", (str(strat),)).fetchone()
                if r is not None:
                    d = _row_to_dict(r)
                    sid, sname = d.get("id"), d.get("name", "")
        except Exception:
            pass
        if not sname:
            sname = str(strat or "ema_trend")
        ud = _row_to_dict(user)
        try:
            from core.tiers import require_strategy
            require_strategy(ud.get("tier", "Free"), sname)
        except PermissionError as e:
            return _err(h, str(e), 403)
        except Exception:
            pass
        metrics, equity = _engine_backtest(conn, sname, symbol, params or {}, from_ts, to_ts)
        # Stride the equity curve rather than truncating it. A six-month run
        # produces ~259k points; equity[:5000] kept the first 11 days and
        # dropped the rest without saying so, so the chart rendered a
        # three-week curve under a six-month heading.
        downsampled = False
        try:
            from engines.backtest import downsample_equity
            equity, downsampled = downsample_equity(equity)
        except Exception:
            equity = list(equity or [])[:5000]
        cur = conn.execute(
            "INSERT INTO backtests (user_id, strategy_id, symbol, params_json, from_ts, to_ts,"
            " metrics_json, equity_json, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'done')",
            (ud["id"], sid, symbol, json.dumps(params or {}), from_ts, to_ts,
             json.dumps(metrics), json.dumps(equity)))
        conn.commit()
        bid = cur.lastrowid
        _audit(conn, ud["id"], "backtest.create", ud.get("tier", ""), "paper",
               {"id": bid, "strategy": sname, "symbol": symbol})
        return _send_json(h, {"id": bid, "metrics": metrics,
                              "equity": equity,
                              "equity_downsampled": downsampled}, status=201)

    def _bt_validate(self, conn, h):
        """Walk-forward validation for a strategy + parameter grid.

        Answers "if I had picked these parameters without seeing this
        data, what would they have scored?" rather than "what did this
        one parameter set score?", which is the number a plain backtest
        reports and the number that does not survive contact with a
        live tape.
        """
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        data, err = _read_json(h)
        if err:
            return _err(h, err)
        data = data or {}
        symbol = str(data.get("symbol", "")).strip().upper()
        strat = str(data.get("strategy", data.get("strategy_id", ""))).strip()
        if not symbol or not strat:
            return _err(h, "strategy and symbol required")
        grid = data.get("grid")
        if grid is not None and not isinstance(grid, dict):
            return _err(h, "grid must be an object of {param: [values]}")
        try:
            folds = max(1, min(8, int(data.get("folds", 3))))
        except (TypeError, ValueError):
            folds = 3
        objective_key = str(data.get("objective", "sharpe")).strip() or "sharpe"
        if objective_key not in ("sharpe", "profit_factor", "net_pnl"):
            return _err(h, "objective must be sharpe|profit_factor|net_pnl")
        try:
            limit = int(data.get("limit", 8000))
        except (TypeError, ValueError):
            limit = 8000
        limit = max(200, min(limit, 20000))
        # A sane default grid when the caller does not supply one, so the
        # button does something useful on first press.
        if not grid:
            grid = {"fast": [5, 9, 13], "slow": [21, 34, 55]} \
                if strat in ("ema_trend", "dual_ema") else None
            if grid is None:
                grid = {"period": [10, 20, 40], "entry_z": [1.5, 2.0, 2.5]}
        try:
            rows = conn.execute(
                "SELECT * FROM candles_1m WHERE symbol = ? ORDER BY ts ASC LIMIT ?",
                (symbol, limit)).fetchall()
        except Exception as e:
            return _err(h, "candles failed: {}".format(e), 500)
        candles = [_row_to_dict(r) for r in rows]
        if len(candles) < 60:
            return _send_json(h, {
                "symbol": symbol, "strategy": strat, "candles": len(candles),
                "skipped": "need at least 60 candles; have {}".format(len(candles)),
                "verdict": "not enough data to validate", "folds": [],
                "combos": 0, "is": {}, "oos": {}, "degradation": None,
                "robustness": 0.0})
        try:
            from engines.walkforward import walk_forward
            res = walk_forward(candles, strat, grid=grid, folds=folds,
                               objective_key=objective_key)
        except Exception as e:
            return _err(h, "validation failed: {}".format(e), 500)
        res["symbol"] = symbol
        ud = _row_to_dict(user)
        _audit(conn, ud["id"], "backtest.validate", ud.get("tier", ""), "paper",
               {"symbol": symbol, "strategy": strat, "folds": len(res.get("folds", [])),
                "degradation": res.get("degradation")})
        return _send_json(h, res)

    def _bt_list(self, conn, h, qs):
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        ud = _row_to_dict(user)
        try:
            limit = int((qs.get("limit", ["50"])[0]))
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))
        if str(ud.get("tier", "")).lower() == "admin":
            rows = conn.execute("SELECT * FROM backtests ORDER BY id DESC LIMIT ?",
                                (limit,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM backtests WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                                (ud["id"], limit)).fetchall()
        items = []
        for r in rows:
            d = _row_to_dict(r)
            for k in ("params_json", "metrics_json", "equity_json"):
                try:
                    d[k.replace("_json", "")] = json.loads(d.get(k, "{}") if k != "equity_json" else d.get(k, "[]"))
                except Exception:
                    pass
            items.append(d)
        return _send_json(h, {"backtests": items})

    def _bt_get(self, conn, h, path):
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        ud = _row_to_dict(user)
        bid = path.rsplit("/", 1)[-1]
        try:
            bid_i = int(bid)
        except (TypeError, ValueError):
            return _err(h, "invalid id")
        r = conn.execute("SELECT * FROM backtests WHERE id = ?", (bid_i,)).fetchone()
        if r is None:
            return _err(h, "not found", 404)
        d = _row_to_dict(r)
        if int(d.get("user_id")) != int(ud["id"]) and str(ud.get("tier", "")).lower() != "admin":
            return _err(h, "forbidden", 403)
        for k, default in (("params_json", "{}"), ("metrics_json", "{}"), ("equity_json", "[]")):
            try:
                d[k.replace("_json", "")] = json.loads(d.get(k, default))
            except Exception:
                pass
        return _send_json(h, d)

    # -- orders ----------------------------------------------------------
    def _order_create(self, conn, h):
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        data, err = _read_json(h)
        if err:
            return _err(h, err)
        data = data or {}
        # Normalise the symbol. Without this a client sending "xauusd"
        # created a *separate* position row from "XAUUSD" -- two rows for
        # one instrument, neither netting against the other, and the
        # lowercase one matched no quote so it was eventually filled at
        # the fallback price. One instrument, one spelling.
        symbol = str(data.get("symbol", "")).strip().upper()
        side = str(data.get("side", "")).strip().lower()
        otype = str(data.get("type", data.get("otype", data.get("order_type", "market")))).strip().lower() or "market"
        mode = str(data.get("mode", "paper")).strip().lower() or "paper"
        try:
            qty = float(data.get("qty", 0))
        except (TypeError, ValueError):
            return _err(h, "qty must be numeric")
        price = data.get("price", data.get("limit_price"))
        try:
            price_f = float(price) if price is not None else None
        except (TypeError, ValueError):
            price_f = None
        if not symbol:
            return _err(h, "symbol required")
        if side not in ("buy", "sell"):
            return _err(h, "side must be buy|sell")
        if qty <= 0:
            return _err(h, "qty must be > 0")
        if otype not in ("market", "limit", "stop", "trailing", "bracket"):
            otype = "market"
        if otype == "limit" and not price_f:
            return _err(h, "limit orders require price")
        ud = _row_to_dict(user)
        order = {"symbol": symbol, "side": side, "qty": qty, "type": otype,
                 "price": price_f, "mode": mode}
        # live gating
        if mode == "live":
            live_ok = os.environ.get("LIVE_TRADING_ENABLED", "0") == "1"
            if not live_ok:
                _risk_event(conn, ud["id"], "live_blocked", "LIVE_TRADING_ENABLED != 1", order)
                return _err(h, "live trading disabled", 403)
            try:
                from core.tiers import require_live
                require_live(ud.get("tier", "Free"))
            except PermissionError as e:
                _risk_event(conn, ud["id"], "live_blocked", str(e), order)
                return _err(h, str(e), 403)
            except ImportError as e:
                # Fail closed. A bare `except Exception: pass` here let a
                # broken/absent tier module wave every tier through the
                # live gate -- the one path where that is unacceptable.
                _risk_event(conn, ud["id"], "live_blocked",
                            "tier entitlements unavailable: %s" % e, order)
                return _err(h, "live trading unavailable: tier check failed", 503)
            if not data.get("confirm"):
                return _err(h, "live orders require {confirm: true}", 403)
        # risk check (paper + live)
        allow, reason = _engine_risk_check(conn, ud["id"], order)
        if not allow:
            _risk_event(conn, ud["id"], "reject", reason, order)
            _audit(conn, ud["id"], "order.rejected", ud.get("tier", ""), mode,
                   {"order": order, "reason": reason})
            return _err(h, "risk reject: {}".format(reason), 403)
        import secrets as _s
        coid = str(data.get("client_order_id", "") or _s.token_hex(8))
        cur = conn.execute(
            "INSERT INTO orders (user_id, symbol, side, type, qty, price, stop_price, status,"
            " mode, client_order_id) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (ud["id"], symbol, side, otype, qty, price_f, data.get("stop_price"), mode, coid))
        oid = cur.lastrowid
        conn.commit()
        fill = None
        if mode == "paper":
            try:
                fill = _apply_paper_fill(conn, ud["id"], oid, order)
            except Exception as e:
                conn.execute("UPDATE orders SET status = 'rejected' WHERE id = ?", (oid,))
                conn.commit()
                _risk_event(conn, ud["id"], "fill_error", str(e), order)
                return _err(h, "fill failed: {}".format(e), 500)
        else:
            # Live: submit to a real venue and believe only what it says.
            #
            # This used to run `_apply_paper_fill` whenever the router
            # returned something that was not "rejected" -- and the
            # router was a stub that returned "routed_stub". So a live
            # buy produced a simulated position, a fee and P&L for an
            # asset that was never bought. Now:
            #
            #   * the venue's own status decides the order's fate;
            #   * only a venue-confirmed fill writes to the local ledger;
            #   * an unreachable venue leaves the order `unknown`, which
            #     is the truth after a timeout, rather than `filled`.
            try:
                from data.feeds import place_live_order
                routed = place_live_order(
                    symbol, side, qty, otype, price_f,
                    broker=str(data.get("broker") or "binance"),
                    user_id=ud["id"], tier=ud.get("tier", "Free"),
                    client_order_id=coid)
            except Exception as e:
                # The order may or may not have reached the venue. Record
                # that honestly and make it findable, instead of
                # guessing.
                conn.execute(
                    "UPDATE orders SET status = 'unknown' WHERE id = ?", (oid,))
                conn.commit()
                _risk_event(conn, ud["id"], "live_unknown", str(e), order)
                _audit(conn, ud["id"], "order.unknown", ud.get("tier", ""), mode,
                       {"id": oid, "order": order, "error": str(e)})
                return _err(h, "live order state unknown — check the venue "
                                "before retrying: {}".format(e), 502)

            vstatus = str(routed.get("status") or "unknown")
            conn.execute(
                "UPDATE orders SET status = ?, venue_order_id = ? WHERE id = ?",
                (vstatus, str(routed.get("venue_order_id") or ""), oid))
            conn.commit()
            fill = routed
            if routed.get("filled"):
                # Only now does the local ledger learn about the fill.
                _apply_paper_fill(conn, ud["id"], oid, order)
            else:
                _audit(conn, ud["id"], "order.not_filled", ud.get("tier", ""),
                       mode, {"id": oid, "order": order, "venue": routed})
        _audit(conn, ud["id"], "order.create", ud.get("tier", ""), mode,
               {"id": oid, "order": order, "fill": fill})
        o = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
        od = _row_to_dict(o)
        # The order fields are echoed at the top level as well as under
        # "order". A client reading r.id / r.status got undefined and
        # rendered "order #?" for a trade that had in fact gone through.
        resp = {"order": od, "fill": fill}
        for k in ("id", "symbol", "side", "type", "qty", "price", "status", "mode"):
            if k in od:
                resp[k] = od[k]
        return _send_json(h, resp, status=201)

    def _order_list(self, conn, h, qs):
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        ud = _row_to_dict(user)
        try:
            limit = int(qs.get("limit", ["50"])[0])
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))
        rows = conn.execute("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                            (ud["id"], limit)).fetchall()
        return _send_json(h, {"orders": [_row_to_dict(r) for r in rows]})

    def _order_cancel(self, conn, h, path):
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        oid = path.rsplit("/", 1)[-1]
        try:
            oid_i = int(oid)
        except (TypeError, ValueError):
            return _err(h, "invalid id")
        ud = _row_to_dict(user)
        r = conn.execute("SELECT * FROM orders WHERE id = ? AND user_id = ?",
                         (oid_i, ud["id"])).fetchone()
        if r is None:
            return _err(h, "not found", 404)
        d = _row_to_dict(r)
        # 'unknown' is cancellable precisely because it may have reached
        # the exchange and still be working. Excluding it -- as this did --
        # left an operator unable to stop the one order most likely to
        # need stopping.
        if d.get("status") not in ("open", "routed", "unknown", "new", "partial"):
            return _err(h, "only open orders can be cancelled (status={})".format(d.get("status")))
        mode = d.get("mode", "paper")
        # A live cancel has to reach the venue. Flipping the local row to
        # 'cancelled' while the exchange keeps the order working is a lie
        # that only surfaces later as an unexpected fill.
        if mode == "live":
            try:
                from data.feeds import cancel_live_order
                cancel_live_order(d, user=ud)
            except Exception as e:
                # Leave the row alone. Marking it cancelled when the venue
                # refused is the exact failure this avoids.
                _audit(conn, ud["id"], "order.cancel_failed", ud.get("tier", ""),
                       mode, {"id": oid_i, "error": str(e)})
                return _err(h, "venue cancel failed -- order left as {}: {}"
                            .format(d.get("status"), e), 502)
        conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (oid_i,))
        conn.commit()
        _audit(conn, ud["id"], "order.cancel", ud.get("tier", ""), d.get("mode", "paper"),
               {"id": oid_i})
        o = conn.execute("SELECT * FROM orders WHERE id = ?", (oid_i,)).fetchone()
        return _send_json(h, {"order": _row_to_dict(o)})

    def _flatten(self, conn, h):
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        ud = _row_to_dict(user)
        data, _e = _read_json(h)
        mode = str((data or {}).get("mode", "paper")).lower() or "paper"
        rows = conn.execute("SELECT * FROM positions WHERE user_id = ? AND qty != 0",
                            (ud["id"],)).fetchall()
        closed = []
        rejected = []
        for r in rows:
            pd = _row_to_dict(r)
            sym = pd.get("symbol", "")
            qty = float(pd.get("qty") or 0)
            if qty == 0:
                continue
            side = "sell" if qty > 0 else "buy"
            order = {"symbol": sym, "side": side, "qty": abs(qty), "type": "market",
                     "price": None, "mode": mode}
            allow, reason = _engine_risk_check(conn, ud["id"], order)
            if not allow:
                _risk_event(conn, ud["id"], "flatten_reject", reason, order)
                # Report it. A silent skip made a partial flatten
                # indistinguishable from "you held nothing", so the
                # operator believed they were flat while still exposed.
                rejected.append({"symbol": sym, "qty": abs(qty),
                                 "stage": "risk", "reason": reason})
                continue
            import secrets as _s
            cur = conn.execute(
                "INSERT INTO orders (user_id, symbol, side, type, qty, status, mode,"
                " client_order_id) VALUES (?, ?, ?, 'market', ?, 'open', ?, ?)",
                (ud["id"], sym, side, abs(qty), mode, _s.token_hex(8)))
            oid = cur.lastrowid
            conn.commit()
            try:
                fill = _apply_paper_fill(conn, ud["id"], oid, order)
            except Exception as e:
                conn.execute("UPDATE orders SET status = 'rejected' WHERE id = ?", (oid,))
                conn.commit()
                _risk_event(conn, ud["id"], "flatten_error", str(e), order)
                rejected.append({"symbol": sym, "qty": abs(qty),
                                 "stage": "fill", "reason": str(e)})
                continue
            closed.append({"order_id": oid, "symbol": sym, "qty": abs(qty), "fill": fill})
        _audit(conn, ud["id"], "positions.flatten", ud.get("tier", ""), mode,
               {"closed": len(closed), "rejected": len(rejected)})
        payload = {"closed": closed, "count": len(closed),
                   "rejected": rejected, "rejected_count": len(rejected)}
        # Partial success is a 409, not a 200: the caller asked to close
        # everything and something is still open.
        if rejected and not closed:
            return _send_json(h, payload, status=409)
        return _send_json(h, payload)

    def _portfolio(self, conn, h):
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        ud = _row_to_dict(user)
        # Two different sets, deliberately:
        #   * every row carries the per-symbol realized P&L ledger, and
        #     that must be summed over ALL rows including the closed
        #     qty=0 ones. Filtering first reported realized_pnl = 0 for
        #     an account that had actually realised a large loss.
        #   * only non-zero rows are open positions, and only those are
        #     listed as holdings.
        all_rows = conn.execute(
            "SELECT * FROM positions WHERE user_id = ?", (ud["id"],)).fetchall()
        total_real = 0.0
        for r in all_rows:
            total_real += float(_row_to_dict(r).get("realized_pnl") or 0)
        poss = [r for r in all_rows if float(_row_to_dict(r).get("qty") or 0) != 0]
        fills = conn.execute("SELECT * FROM fills WHERE user_id = ? ORDER BY id DESC LIMIT 50",
                             (ud["id"],)).fetchall()
        out_pos = []
        total_unreal = 0.0
        for r in poss:
            pd = _row_to_dict(r)
            last = _latest_price(conn, pd.get("symbol", ""), pd.get("avg_price") or 0)
            qty = float(pd.get("qty") or 0)
            avg = float(pd.get("avg_price") or 0)
            unreal = (last - avg) * qty
            total_unreal += unreal
            pd["last"] = last
            pd["unrealized_pnl"] = round(unreal, 2)
            out_pos.append(pd)
        # Surface the live trading status so the UI can reflect it. A
        # user whose tier and env both allow live should see that, even
        # while the default broker is still paper.
        live_enabled = False
        try:
            from brokers import live_trading_enabled
            tier_live = self._tier_live_enabled(conn, ud)
            live_enabled = live_trading_enabled() and tier_live
        except Exception:
            live_enabled = False
        # Cash balance from the accounts table. The paper broker reads
        # the same row, so this is the single source of truth for the
        # displayed balance.
        cash = 0.0
        try:
            acct = conn.execute(
                "SELECT cash FROM accounts WHERE user_id = ?",
                (ud["id"],)).fetchone()
            if acct is not None:
                cash = float(acct["cash"] or 0.0)
        except Exception:
            cash = 0.0
        return _send_json(h, {"positions": out_pos,
                              "fills": [_row_to_dict(f) for f in fills],
                              "cash": round(cash, 2),
                              "live_trading_enabled": live_enabled,
                              "tier": ud.get("tier", ""),
                              "default_broker": "paper",
                              "summary": {"unrealized_pnl": round(total_unreal, 2),
                                          "realized_pnl": round(total_real, 2),
                                          "total_pnl": round(total_unreal + total_real, 2)}})

    # -- risk ------------------------------------------------------------
    def _risk_get(self, conn, h):
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        ud = _row_to_dict(user)
        try:
            from core.risk import get_limits
            lim = get_limits(conn, ud["id"])
        except Exception:
            lim = {}
        return _send_json(h, lim)

    def _risk_set(self, conn, h):
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        data, err = _read_json(h)
        if err:
            return _err(h, err)
        ud = _row_to_dict(user)
        try:
            from core.risk import set_limits
            lim = set_limits(conn, ud["id"], data or {})
        except Exception as e:
            return _err(h, "risk update failed: {}".format(e), 500)
        _audit(conn, ud["id"], "risk.update", ud.get("tier", ""), "paper",
               {"patch": data or {}})
        return _send_json(h, lim)

    # -- journal ----------------------------------------------------------
    # -- agents -----------------------------------------------------------
    def _agent_market(self, conn, symbols, bars=250):
        """Market snapshot the agents read: candles + latest quote per symbol.

        Symbols are normalised, and a symbol with no stored tape is
        reported rather than silently skipped -- otherwise an operator
        cannot tell an idle agent apart from one watching an instrument
        that does not exist.
        """
        market = {}
        missing = []
        for sym in symbols:
            s = str(sym or "").strip().upper()
            if not s:
                continue
            try:
                rows = conn.execute(
                    "SELECT * FROM candles_1m WHERE symbol = ?"
                    " ORDER BY ts DESC LIMIT ?", (s, int(bars))).fetchall()
            except Exception:
                rows = []
            if not rows:
                missing.append(s)
                continue
            quote = None
            try:
                q = conn.execute(
                    "SELECT * FROM quotes_l1 WHERE symbol = ?"
                    " ORDER BY ts DESC LIMIT 1", (s,)).fetchone()
                if q is not None:
                    quote = _row_to_dict(q)
            except Exception:
                quote = None
            market[s] = {"candles": [_row_to_dict(r) for r in rows][::-1],
                         "quote": quote or {}}
        return market, missing

    def _agents_list(self, conn, h):
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        try:
            from brokers.agent import runtime
        except Exception as e:
            return _err(h, "agent runtime unavailable: {}".format(e), 503)
        return _send_json(h, runtime().snapshot())

    def _agent_create(self, conn, h):
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        data, err = _read_json(h)
        if err:
            return _err(h, err)
        data = data or {}
        try:
            from brokers.agent import Mandate, MandateError, runtime
            m = Mandate(
                name=str(data.get("name", "")).strip() or "agent",
                strategy=str(data.get("strategy", "ema_trend")).strip(),
                symbols=data.get("symbols") or [],
                max_notional=data.get("max_notional", 5000),
                max_actions_per_hour=data.get("max_actions_per_hour", 12),
                # auto_execute is opt-in and defaults off.
                auto_execute=bool(data.get("auto_execute", False)),
                mode=str(data.get("mode", "paper")).strip().lower(),
                params=data.get("params") or {},
                agent_id=data.get("agent_id"))
        except Exception as e:
            # MandateError carries the operator-facing reason.
            return _err(h, "mandate invalid: {}".format(e), 400)
        if not m.symbols:
            return _err(h, "a mandate with no symbols has no authority", 400)
        ud = _row_to_dict(user)
        if m.mode == "live":
            # A live mandate is refused unless the operator says so
            # explicitly *and* the host and tier both permit it.
            if not data.get("confirm_live"):
                return _err(h, "live mandates require {confirm_live: true}", 403)
            allowed, reason = _live_allowed(ud)
            if not allowed:
                return _err(h, reason, 403)
        try:
            rt = runtime()
            agent = rt.add(m)
        except Exception as e:
            return _err(h, str(e), 409)
        # An executor is attached only when the mandate may act. Without
        # one the agent still proposes, and the runtime fails closed
        # rather than pretending an order went out.
        if m.auto_execute:
            agent.executor = _make_agent_executor(conn, ud, m.mode)
        if data.get("start"):
            rt.start(m.agent_id)
        _audit(conn, ud["id"], "agent.create", ud.get("tier", ""), m.mode,
               m.to_dict())
        return _send_json(h, {"agent": m.to_dict(),
                              "auto_execute": m.auto_execute}, status=201)

    def _agent_tick(self, conn, h):
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        data, err = _read_json(h)
        if err is not None and data is None:
            data = {}
        data = data or {}
        try:
            from brokers.agent import runtime
        except Exception as e:
            return _err(h, "agent runtime unavailable: {}".format(e), 503)
        rt = runtime()
        if not rt.agents:
            return _send_json(h, {"proposals": [], "count": 0, "agents": [],
                                  "note": "no agents registered"})
        try:
            bars = max(80, min(1000, int(data.get("bars", 250))))
        except (TypeError, ValueError):
            bars = 250
        syms = set()
        for a in rt.agents.values():
            syms.update(a.mandate.symbols)
        market, missing = self._agent_market(conn, sorted(syms), bars)
        rt.set_market(market)
        proposals = rt.tick()
        return _send_json(h, {
            "proposals": [p.to_dict() for p in proposals],
            "count": len(proposals),
            "scanned_symbols": sorted(market.keys()),
            "missing_symbols": missing,
            "agents": rt.snapshot()["agents"],
        })

    def _agent_action(self, conn, h, path):
        """start | pause | retire | ratify | auto on an agent id."""
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        data, err = _read_json(h)
        if err is not None and data is None:
            data = {}
        data = data or {}
        tail = path[len("/api/agents/"):].strip("/")
        parts = tail.split("/")
        agent_id = parts[0] if parts else ""
        action = parts[1] if len(parts) > 1 else ""
        try:
            from brokers.agent import runtime
        except Exception as e:
            return _err(h, "agent runtime unavailable: {}".format(e), 503)
        rt = runtime()
        agent = rt.get(agent_id)
        if agent is None:
            return _err(h, "no such agent", 404)
        ud = _row_to_dict(user)
        if action == "start":
            rt.start(agent_id)
        elif action == "pause":
            rt.pause(agent_id)
        elif action == "retire":
            rt.remove(agent_id)
        elif action == "auto":
            ok, reason = self._agent_arm(agent, data, ud, conn)
            if not ok:
                return _err(h, reason, 403)
        elif action == "ratify":
            return self._agent_ratify(conn, h, agent, data)
        else:
            return _err(h, "unknown agent action", 400)
        _audit(conn, ud["id"], "agent." + (action or "?"), ud.get("tier", ""),
               agent.mandate.mode if action != "retire" else "paper",
               {"agent": agent_id})
        return _send_json(h, {
            "ok": True,
            "agent": agent.status() if agent_id in rt.agents else {"retired": True}})

    def _agent_arm(self, agent, data, ud, conn=None):
        """Arm or disarm auto-execution.

        This is the most consequential thing an operator can do here, so
        it carries its own explicit confirm flag and re-checks the host
        and tier from scratch rather than trusting the stored mandate.
        """
        want = bool(data.get("enabled"))
        if not want:
            agent.executor = None
            agent.mandate.auto_execute = False
            return True, ""
        if not data.get("confirm"):
            return False, "arming auto-execute requires {confirm: true}"
        if agent.mandate.mode == "live":
            allowed, reason = _live_allowed(ud)
            if not allowed:
                return False, reason
        if conn is None:
            # Cannot build an executor without a connection, so refuse
            # rather than set the flag and leave the agent unable to act.
            return False, "no connection to arm auto-execute"
        agent.executor = _make_agent_executor(conn, ud, agent.mandate.mode)
        agent.mandate.auto_execute = True
        return True, ""

    def _agent_ratify(self, conn, h, agent, data):
        """Human-in-the-loop: turn a held proposal into a real order.

        The proposal still passes the platform's own risk engine. An
        operator approving an agent's idea does not exempt it from the
        same checks a manual click goes through.
        """
        ud = _row_to_dict(user)
        ts = data.get("ts")
        prop = None
        for p in reversed(agent.proposals):
            if ts is None or p.ts == ts:
                prop = p
                break
        if prop is None:
            return _err(h, "no matching proposal", 404)
        if prop.outcome == "executed":
            return _err(h, "proposal already executed", 409)
        if not agent.mandate.may_trade(prop.symbol):
            return _err(h, "symbol outside mandate", 403)
        if not agent.mandate.rate_ok():
            return _err(h, "agent action budget exhausted for this hour", 429)
        _audit(conn, ud["id"], "agent.ratify", ud.get("tier", ""),
               agent.mandate.mode,
               {"agent": agent.mandate.agent_id, "symbol": prop.symbol,
                "side": prop.side, "qty": prop.qty})
        try:
            result = _dispatch_order(conn, ud, prop, agent.mandate.mode)
        except Exception as e:
            return _err(h, "order failed: {}".format(e), 400)
        if not result.get("ok"):
            return _err(h, result.get("error", "order rejected"), 400)
        prop.outcome = "executed"
        prop.order_id = result.get("id")
        prop.notes.append("ratified by operator")
        agent.mandate.record_action()
        agent.stats["executed"] = agent.stats.get("executed", 0) + 1
        return _send_json(h, {"ok": True, "proposal": prop.to_dict(),
                              "order_id": result.get("id")})

    def _journal_list(self, conn, h):
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        ud = _row_to_dict(user)
        rows = conn.execute("SELECT * FROM journal WHERE user_id = ? ORDER BY id DESC LIMIT 100",
                            (ud["id"],)).fetchall()
        return _send_json(h, {"entries": [_row_to_dict(r) for r in rows]})

    def _journal_add(self, conn, h):
        user, sess = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_csrf(conn, h, sess):
            return True
        data, err = _read_json(h)
        if err:
            return _err(h, err)
        data = data or {}
        ud = _row_to_dict(user)
        try:
            ts = int(data.get("ts", int(time.time())) or int(time.time()))
        except (TypeError, ValueError):
            ts = int(time.time())
        cur = conn.execute(
            "INSERT INTO journal (user_id, ts, symbol, strategy, note, pnl_ref)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ud["id"], ts, str(data.get("symbol", "")),
             str(data.get("strategy", "")), str(data.get("note", "")),
             data.get("pnl_ref")))
        conn.commit()
        r = conn.execute("SELECT * FROM journal WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _send_json(h, _row_to_dict(r), status=201)

    # -- admin --------------------------------------------------------------
    def _admin_overview(self, conn, h):
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_admin(conn, h, user):
            return True
        def _count(sql, args=()):
            try:
                r = conn.execute(sql, args).fetchone()
                return int(r[0])
            except Exception:
                return 0
        return _send_json(h, {
            "users": _count("SELECT COUNT(*) FROM users"),
            "sessions": _count("SELECT COUNT(*) FROM sessions"),
            "symbols": _count("SELECT COUNT(*) FROM symbols"),
            "candles": _candle_row_total(conn),
            "quotes": _count("SELECT COUNT(*) FROM quotes_l1"),
            "orders": _count("SELECT COUNT(*) FROM orders"),
            "fills": _count("SELECT COUNT(*) FROM fills"),
            "backtests": _count("SELECT COUNT(*) FROM backtests"),
            "risk_events": _count("SELECT COUNT(*) FROM risk_events"),
            "audit": _count("SELECT COUNT(*) FROM audit_log"),
        })

    def _admin_audit(self, conn, h, qs):
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_admin(conn, h, user):
            return True
        try:
            limit = int(qs.get("limit", ["100"])[0])
        except (TypeError, ValueError):
            limit = 100
        limit = max(1, min(limit, 500))
        action = (qs.get("action", [""])[0] or "").strip()
        try:
            if action:
                rows = conn.execute("SELECT * FROM audit_log WHERE action = ?"
                                    " ORDER BY id DESC LIMIT ?", (action, limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
                                    (limit,)).fetchall()
            return _send_json(h, {"audit": [_row_to_dict(r) for r in rows]})
        except Exception as e:
            return _err(h, "audit failed: {}".format(e), 500)

    def _admin_users(self, conn, h):
        user, _s = self._require_user(conn, h)
        if user is None:
            return True
        if not self._require_admin(conn, h, user):
            return True
        try:
            rows = conn.execute(
                "SELECT id, email, tier, disabled, created_at FROM users ORDER BY id").fetchall()
            users = []
            for r in rows:
                d = _row_to_dict(r)
                users.append({
                    "id": d.get("id"), "email": d.get("email"),
                    "tier": d.get("tier"), "disabled": d.get("disabled"),
                    "created_at": d.get("created_at"),
                })
            return _send_json(h, {"users": users})
        except Exception as e:
            return _err(h, "users failed: {}".format(e), 500)
