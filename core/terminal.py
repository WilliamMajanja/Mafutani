"""OpenTerminal command engine. Stdlib only.

Bloomberg-style command line for the trading app. All commands reuse the
exact server bridges from api.routes (_engine_risk_check, _apply_paper_fill,
_engine_preview, _engine_backtest, _latest_price), so terminal trades go
through the identical risk + fill + tier path as the REST API. No drift.

Commands (case-insensitive verb):
  help                          show command list
  quote SYM                     L1 quote + last close
  buy SYM QTY [limit PRICE]     paper buy (risk-checked)
  sell SYM QTY [limit PRICE]    paper sell (risk-checked)
  flatten [SYM]                 close paper position(s)
  positions                     paper positions + P&L
  orders [N]                    recent orders (default 5)
  signal KIND SYM               preview scalp signal
  backtest KIND SYM [N]         backtest last N 1m candles (def 2000)
  screener                      volume leaders
  arb [crypto|fx|commodity|all] arbitrage scanner (Pro tier)
  venues                       per-venue books for one symbol
  risk | tier                   limits / entitlements
"""

import json
import shlex
import time

HELP_TEXT = (
    "OpenTerminal — commands:\n"
    "  help                      this list\n"
    "  quote SYM                 e.g. quote AAPL\n"
    "  buy SYM QTY [limit P]     paper buy, risk-checked\n"
    "  sell SYM QTY [limit P]    paper sell, risk-checked\n"
    "  flatten [SYM]             close paper position(s)\n"
    "  positions                 positions + P&L\n"
    "  orders [N]                recent orders\n"
    "  signal KIND SYM           preview signal (ema_trend, vwap_pullback)\n"
    "  backtest KIND SYM [N]     backtest last N 1m candles (def 2000)\n"
    "  screener                  volume leaders\n"
    "  arb [CLASS]               arbitrage edges net of fees (Pro)\n"
    "  venues SYM                per-venue books for one symbol\n"
    "  status                    service health + feed freshness\n"
    "  risk | tier                limits / entitlements\n"
    "  broker [CMD...]           broker layer (default: paper)\n"
    "                             e.g. broker status | quote AAPL |\n"
    "                             buy AAPL 10 | use <venue> | arm yes"
)


def _bridges():
    """Lazy import of api.routes bridges (keeps stdlib-only, no cycles)."""
    try:
        mod = __import__("api.routes", fromlist=[
            "_engine_risk_check", "_apply_paper_fill", "_engine_preview",
            "_engine_backtest", "_engine_arbitrage", "_arbitrage_books",
            "_latest_price", "_audit", "_risk_event", "_row_to_dict"])
        return {n: getattr(mod, n) for n in (
            "_engine_risk_check", "_apply_paper_fill", "_engine_preview",
            "_engine_backtest", "_engine_arbitrage", "_arbitrage_books",
            "_latest_price", "_audit", "_risk_event",
            "_row_to_dict") if hasattr(mod, n)}
    except Exception:
        return {}


def _cmd_broker(conn, ud, args):
    """Bridge the terminal to the broker layer.

    The broker layer is the interactive seam to any trading API, so
    `broker <anything>` is passed straight to a `BrokerSession`. The
    session is built per command, which means the `arm` gate cannot carry
    over between terminal lines -- arming is an interactive-session
    concept, not a request concept.
    """
    try:
        from brokers import get_broker, BrokerSession
        from brokers.base import live_trading_enabled
    except Exception as e:
        return False, "broker layer unavailable: {}".format(e), None
    # `broker use <venue>` switches; everything after is the command.
    parts = list(args)
    name = "paper"
    if parts and parts[0].lower() == "use" and len(parts) > 1:
        name, parts = parts[1], parts[2:]
    elif parts and parts[0].lower() in ("use", "brokers", "help"):
        name, parts = (parts[1] if len(parts) > 1 else "paper"), parts[2:]
    rest = " ".join(parts).strip()
    if not rest:
        rest = "status"
    try:
        tier_live = False
        row = conn.execute("SELECT live_enabled FROM tiers WHERE name = ?",
                           (ud.get("tier", "Free"),)).fetchone()
        tier_live = bool(row["live_enabled"]) if row is not None else False
    except Exception:
        tier_live = False
    try:
        broker = get_broker(name, conn=conn, user_id=ud.get("id"), user=ud,
                            tier_live_enabled=tier_live)
    except Exception as e:
        return False, str(e), None
    events = []
    session = BrokerSession(broker, on_event=events.append)
    output = session.handle(rest)
    lowered = output.lower()
    ok = not (lowered.startswith(("broker error", "error:", "invalid:")))
    return ok, output, {"broker": broker.name, "venue": broker.venue,
                        "live": bool(broker.is_live), "events": events,
                        "health": broker.health()}


def parse_command(text):
    """Split raw input into (verb, args). Raises ValueError on empty."""
    if text is None:
        raise ValueError("empty command")
    try:
        parts = shlex.split(str(text).strip())
    except Exception:
        parts = str(text).strip().split()
    if not parts:
        raise ValueError("empty command")
    return parts[0].lower(), parts[1:]


def _row(conn, sql, args=()):
    try:
        r = conn.execute(sql, args).fetchone()
        if r is None:
            return None
        try:
            return {k: r[k] for k in r.keys()}
        except Exception:
            return dict(r)
    except Exception:
        return None


def _quote_row(conn, symbol):
    q = _row(conn, "SELECT * FROM quotes_l1 WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
             (symbol,))
    if q:
        return q
    return None


def _ref_price(conn, symbol, default=0.0):
    br = _bridges()
    fn = br.get("_latest_price")
    if fn is not None:
        try:
            return float(fn(conn, symbol, default))
        except Exception:
            pass
    q = _quote_row(conn, symbol)
    if q:
        for k in ("last", "ask", "bid"):
            try:
                v = float(q.get(k) or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0:
                return v
    c = _row(conn, "SELECT * FROM candles_1m WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
             (symbol,))
    if c:
        try:
            return float(c.get("close") or 0)
        except (TypeError, ValueError):
            pass
    return float(default)


def _place_paper_order(conn, user, symbol, side, qty, otype="market", price=None):
    br = _bridges()
    uid = int(user.get("id"))
    tier = str(user.get("tier", "Free"))
    order = {"symbol": symbol, "side": side, "qty": float(qty), "type": otype,
             "price": price, "mode": "paper"}
    risk_fn = br.get("_engine_risk_check")
    if risk_fn is not None:
        try:
            allow, reason = risk_fn(conn, uid, order)
        except Exception as e:
            allow, reason = False, "risk error: {}".format(e)
    else:
        allow, reason = True, "ok"
    if not allow:
        if br.get("_risk_event"):
            br["_risk_event"](conn, uid, "reject", reason, order)
        if br.get("_audit"):
            br["_audit"](conn, uid, "terminal.order.rejected", tier,
                         {"order": order, "reason": reason})
        return False, "RISK REJECT: {}".format(reason), None
    import secrets as _s
    coid = _s.token_hex(8)
    try:
        cur = conn.execute(
            "INSERT INTO orders (user_id, symbol, side, type, qty, price, stop_price, status,"
            " mode, client_order_id) VALUES (?, ?, ?, ?, ?, ?, NULL, 'open', 'paper', ?)",
            (uid, symbol, side, otype, float(qty), price, coid))
    except Exception as e:
        return False, "order insert failed: {}".format(e), None
    oid = cur.lastrowid
    conn.commit()
    fill_fn = br.get("_apply_paper_fill")
    try:
        fill = fill_fn(conn, uid, oid, order) if fill_fn else None
    except Exception as e:
        try:
            conn.execute("UPDATE orders SET status = 'rejected' WHERE id = ?", (oid,))
            conn.commit()
        except Exception:
            pass
        return False, "fill failed: {}".format(e), None
    if br.get("_audit"):
        br["_audit"](conn, uid, "terminal.order", tier, {"id": oid, "order": order})
    px = price
    if isinstance(fill, dict) and fill.get("price"):
        px = fill.get("price")
    if px is None:
        px = _ref_price(conn, symbol)
    return True, "{} {} {} @ {} (order #{})".format(
        side.upper(), qty, symbol, px, oid), {"id": oid, "fill": fill}


def _cmd_quote(conn, args):
    if not args:
        return False, "usage: quote SYM", None
    sym = args[0].upper()
    q = _quote_row(conn, sym)
    px = _ref_price(conn, sym)
    if px <= 0:
        return False, "no data for {}".format(sym), None
    if q:
        def f(k):
            try:
                return float(q.get(k) or 0)
            except (TypeError, ValueError):
                return 0.0
        return True, "{}  bid {}  ask {}  last {}".format(sym, f("bid"), f("ask"), px), \
            {"symbol": sym, "price": px}
    return True, "{}  last {}".format(sym, px), {"symbol": sym, "price": px}


def _cmd_trade(conn, user, verb, args):
    if len(args) < 2:
        return False, "usage: {} SYM QTY [limit PRICE]".format(verb), None
    sym = args[0].upper()
    try:
        qty = float(args[1])
    except (TypeError, ValueError):
        return False, "QTY must be numeric", None
    if qty <= 0:
        return False, "QTY must be > 0", None
    otype, price = "market", None
    low = [str(a).lower() for a in args[2:]]
    if "limit" in low:
        i = low.index("limit")
        try:
            price = float(args[2 + i + 1])
        except (IndexError, TypeError, ValueError):
            return False, "limit requires a price", None
        otype = "limit"
    return _place_paper_order(conn, user, sym, verb, qty, otype, price)


def _cmd_flatten(conn, user, args):
    uid = int(user.get("id"))
    try:
        rows = conn.execute("SELECT * FROM positions WHERE user_id = ? AND qty != 0",
                            (uid,)).fetchall()
    except Exception as e:
        return False, "flatten failed: {}".format(e), None
    items = []
    for r in rows:
        try:
            items.append({k: r[k] for k in r.keys()})
        except Exception:
            items.append(dict(r))
    if args:
        want = args[0].upper()
        items = [d for d in items if str(d.get("symbol")).upper() == want]
        if not items:
            return True, "no position in {}".format(want), {"closed": []}
    if not items:
        return True, "flat — no positions to close", {"closed": []}
    closed = []
    for d in items:
        sym = str(d.get("symbol"))
        qty = abs(float(d.get("qty") or 0))
        side = "sell" if float(d.get("qty") or 0) > 0 else "buy"
        ok, msg, _f = _place_paper_order(conn, user, sym, side, qty)
        closed.append({"symbol": sym, "qty": qty, "ok": ok, "msg": msg})
    n = sum(1 for c in closed if c["ok"])
    return True, "flattened {}/{} position(s)".format(n, len(closed)), {"closed": closed}


def _cmd_positions(conn, user):
    uid = int(user.get("id"))
    try:
        rows = conn.execute("SELECT * FROM positions WHERE user_id = ? AND qty != 0",
                            (uid,)).fetchall()
    except Exception as e:
        return False, "positions failed: {}".format(e), None
    if not rows:
        return True, "flat — no open positions", {"positions": []}
    lines, out = [], []
    for r in rows:
        try:
            d = {k: r[k] for k in r.keys()}
        except Exception:
            d = dict(r)
        px = _ref_price(conn, str(d.get("symbol")))
        qty = float(d.get("qty") or 0)
        avg = float(d.get("avg_price") or 0)
        pnl = (px - avg) * qty if px and avg else 0.0
        out.append({"symbol": d.get("symbol"), "qty": qty, "avg": avg,
                    "last": px, "unrealized": round(pnl, 2)})
        lines.append("{}  {:+.4g} @ {}  last {}  uPnL {:+.2f}".format(
            d.get("symbol"), qty, avg, px, pnl))
    return True, "\n".join(lines), {"positions": out}


def _cmd_orders(conn, user, args):
    uid = int(user.get("id"))
    try:
        n = int(args[0]) if args else 5
    except (TypeError, ValueError):
        n = 5
    n = max(1, min(n, 50))
    try:
        rows = conn.execute("SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                            (uid, n)).fetchall()
    except Exception as e:
        return False, "orders failed: {}".format(e), None
    if not rows:
        return True, "no orders yet", {"orders": []}
    lines, out = [], []
    for r in rows:
        try:
            d = {k: r[k] for k in r.keys()}
        except Exception:
            d = dict(r)
        out.append({"id": d.get("id"), "symbol": d.get("symbol"), "side": d.get("side"),
                    "qty": d.get("qty"), "status": d.get("status")})
        lines.append("#{} {} {} {} [{}]".format(d.get("id"), d.get("side"),
                                                d.get("qty"), d.get("symbol"), d.get("status")))
    return True, "\n".join(lines), {"orders": out}


def _cmd_signal(conn, args):
    if len(args) < 2:
        return False, "usage: signal KIND SYM", None
    kind, sym = args[0].lower(), args[1].upper()
    br = _bridges()
    fn = br.get("_engine_preview")
    if fn is None:
        return False, "signal engine unavailable", None
    try:
        res = fn(conn, kind, sym, {})
    except Exception as e:
        return False, "signal error: {}".format(e), None
    res = res or {}
    sig = str(res.get("signal", "flat")).upper()
    return True, "{} signal: {} {} ({})".format(kind, sig, sym, res.get("reason", "")), res


def _cmd_backtest(conn, user, args):
    if len(args) < 2:
        return False, "usage: backtest KIND SYM [N]", None
    kind, sym = args[0].lower(), args[1].upper()
    try:
        n = int(args[2]) if len(args) > 2 else 2000
    except (TypeError, ValueError):
        n = 2000
    n = max(50, min(n, 20000))
    tier = str(user.get("tier", "Free"))
    try:
        from core.tiers import require_strategy
        require_strategy(tier, kind)
    except PermissionError as e:
        return False, "tier blocked: {}".format(e), None
    except Exception:
        pass
    br = _bridges()
    fn = br.get("_engine_backtest")
    if fn is None:
        return False, "backtest engine unavailable", None
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM candles_1m WHERE symbol = ?",
                           (sym,)).fetchone()[0]
    except Exception:
        cnt = 0
    if cnt < 50:
        return False, "not enough candles for {} (have {})".format(sym, cnt), None
    from_ts = 0
    if cnt > n:
        try:
            r = conn.execute("SELECT ts FROM candles_1m WHERE symbol = ? ORDER BY ts DESC"
                             " LIMIT 1 OFFSET ?", (sym, n - 1)).fetchone()
            from_ts = int(r[0]) if r else 0
        except Exception:
            from_ts = 0
    try:
        metrics, _equity = fn(conn, kind, sym, {}, from_ts, 0)
    except Exception as e:
        return False, "backtest error: {}".format(e), None
    m = metrics or {}
    line = ("{} {} n={} trades={} win={:.0%} pf={} maxDD={} ret={}".format(
        kind, sym, m.get("candles", n), m.get("trades"),
        float(m.get("win_rate") or 0), m.get("profit_factor"),
        m.get("max_drawdown"), m.get("total_return")))
    # The verdict rides on the summary line. A one-line report of a
    # Sharpe with no statement about whether that Sharpe survives
    # deflation is the exact failure the significance module exists to
    # stop, and the terminal is where someone reads a backtest and
    # decides to trade it.
    sig = m.get("significance") or {}
    verdict = str(sig.get("verdict") or "")
    if verdict:
        line += "  [{}]".format(verdict)
        why = sig.get("reasons") or []
        if why:
            line += "  -- " + str(why[0])
    # ...and so does the tape the run actually saw. `n` is a bar count,
    # so `backtest 30` on a symbol with 30 days stored reads as a short
    # backtest otherwise -- indistinguishable from "it only had 30 days".
    cov = m.get("coverage") or {}
    if cov:
        line += "  tape={:.1f}d".format(float(cov.get("days") or 0))
        note = str(cov.get("note") or "")
        if note:
            line += " ({})".format(note)
    if _bridges().get("_audit"):
        _bridges()["_audit"](conn, int(user.get("id")), "terminal.backtest", tier,
                             {"strategy": kind, "symbol": sym, "metrics": m})
    return True, line, {"metrics": m}


def _cmd_arb(conn, ud, args):
    """`arb [crypto|fx|commodity|all]` — scan arbitrage edges.

    Tier-gated in *both* directions that matter. The gate is re-derived
    from the user row rather than trusted from a cached flag, exactly as
    the live-trading path does: a stale "allowed" is the one thing that
    must not reach a venue map.

    Output is deliberately explicit that edges are net of fees, because
    a scanner printing gross numbers invites the reader to act on a
    spread the fees already consumed.
    """
    want = str(args[0]).strip().lower() if args else "all"
    if want in ("c", "crypto"):
        want = "crypto"
    elif want in ("f", "fx", "forex", "currency"):
        want = "fx"
    elif want in ("c", "cmd", "commodity", "commodities"):
        want = "commodity"
    elif want in ("a", "all", "*"):
        want = "all"
    else:
        return False, "usage: arb [crypto|fx|commodity|all]", None

    scan = _bridges().get("_engine_arbitrage")
    if scan is None:
        # Fail closed and say why. Returning an empty scan here would
        # read as "no opportunities found", which is a very different
        # statement from "the scanner is not available".
        return False, "arbitrage engine unavailable", None
    try:
        from core.tiers import require_entitlement
        require_entitlement(str(ud.get("tier", "Free")), "arbitrage")
    except PermissionError as e:
        return False, "TIER BLOCKED: {} — upgrade to Pro".format(e), None
    except ImportError:
        return False, "arbitrage unavailable: tier check failed", None

    try:
        payload = scan(conn, ud, asset_class=want)
    except Exception as e:
        return False, "arbitrage scan failed: {}".format(e), None

    rows = payload.get("opportunities") or []
    if not rows:
        return True, ("no {} opportunities scanned — venue books may be"
                      " stale or unavailable".format(want)), payload

    lines = ["{:<10} {:<26} {:>10}  {}".format(
        "model", "instrument", "edge bps", "detail")]
    for r in sorted(rows, key=lambda x: -(_edge_of(x) or 0.0)):
        model = str(r.get("model", "?"))
        name = _arb_label(r)
        edge = _edge_of(r)
        lines.append("{:<10} {:<26} {:>10}  {}".format(
            model, name[:26],
            "-" if edge is None else "{:+.1f}".format(edge),
            str(r.get("reason", ""))[:52]))
    priced = payload.get("priced_count") or 0
    lines.append("")
    lines.append("{} priced · {} net-positive after fees · scanned {}".format(
        priced, payload.get("profitable_count") or 0,
        payload.get("scanned", len(rows))))
    lines.append("edges are net of two legs of taker fees; observations, "
                 "not executions")
    return True, "\n".join(lines), payload


def _edge_of(row):
    """edge_bps as a float, or None. Keeps 'unpriceable' distinct from 0."""
    v = (row or {}).get("edge_bps")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _arb_label(row):
    """Human label for an opportunity row, per model."""
    model = str(row.get("model", ""))
    if model == "cross_venue":
        buy = row.get("buy_venue") or "?"
        sell = row.get("sell_venue") or "?"
        # An unpriced cross-venue row has no sell venue (only one book
        # came back). Rendering "BTCUSDT ->" read as a truncated value
        # rather than as "there was nothing to compare against".
        if row.get("sell_venue"):
            return "{} {}->{}".format(row.get("symbol", "?"), buy, sell)
        return "{} (no second venue)".format(row.get("symbol", "?"))
    if model == "triangular":
        return " x ".join(str(s) for s in (row.get("symbols") or []))
    if model == "fx_cross":
        return "{} vs {}".format(row.get("direct_symbol", "?"),
                                 row.get("cross_symbol", "?"))
    if model == "spread":
        return str(row.get("label", "?"))
    return str(row.get("symbol", "?"))


def _cmd_venues(conn, ud, args):
    """`venues SYM` — every venue book we hold for one symbol.

    Answers "why does the scanner think there is no dislocation here?"
    directly: a symbol quoted on one venue cannot have a cross-venue
    edge, and showing the actual books is more use than a bare refusal.
    """
    if not args:
        return False, "usage: venues SYM", None
    sym = str(args[0]).strip().upper()
    books = _bridges().get("_arbitrage_books")
    if books is None:
        return False, "venue registry unavailable", None
    try:
        from core.tiers import require_entitlement
        require_entitlement(str((ud or {}).get("tier", "Free")), "arbitrage")
    except PermissionError as e:
        return False, "TIER BLOCKED: {}".format(e), None
    except ImportError:
        return False, "arbitrage unavailable: tier check failed", None
    try:
        found = books(conn, [sym]) or {}
    except Exception as e:
        return False, "venue lookup failed: {}".format(e), None
    node = found.get(sym) or {}
    if not node:
        return (False, "no venue books for {} (feed idle, or symbol unrouted)"
                .format(sym), None)
    lines = ["{:<10} {:>16} {:>16} {:>8}  {}".format(
        "venue", "bid", "ask", "age_s", "source")]
    now = int(time.time())
    for venue, book in sorted(node.items()):
        try:
            age = now - int(book.get("ts") or now)
        except (TypeError, ValueError):
            age = 0
        lines.append("{:<10} {:>16.8f} {:>16.8f} {:>8}  {}".format(
            venue, float(book.get("bid") or 0), float(book.get("ask") or 0),
            age, str(book.get("source", ""))))
    return True, "\n".join(lines), {"symbol": sym, "venues": node}


def _cmd_screener(conn, args):
    try:
        rows = conn.execute("SELECT DISTINCT symbol FROM candles_1m LIMIT 50").fetchall()
        syms = [r[0] for r in rows]
    except Exception as e:
        return False, "screener failed: {}".format(e), None
    scored = []
    for sname in syms:
        try:
            r = conn.execute(
                "SELECT COALESCE(AVG(volume),0) FROM (SELECT volume FROM candles_1m"
                " WHERE symbol = ? ORDER BY ts DESC LIMIT 60)", (sname,)).fetchone()
            v = float(r[0] or 0) if r else 0.0
            px = _ref_price(conn, sname)
            scored.append((v, sname, px))
        except Exception:
            continue
    scored.sort(reverse=True)
    top = scored[:10]
    if not top:
        return True, "no symbols with data", {"leaders": []}
    lines = ["{:>12}  {:<8}  last".format("avgVol60", "sym")]
    for v, s, px in top:
        lines.append("{:>12.0f}  {:<8}  {}".format(v, s, px))
    return True, "\n".join(lines), {"leaders": [{"symbol": s, "vol": v} for v, s, _p in top]}


def _cmd_status(conn):
    try:
        mod = __import__("api.routes", fromlist=["_status_payload"])
        fn = getattr(mod, "_status_payload", None)
        if fn is None:
            return False, "status service unavailable", None
        st = fn(conn) or {}
    except Exception as e:
        return False, "status failed: {}".format(e), None
    lines = ["{} v{}  up {}s  t={}".format(
        st.get("name") or st.get("service"), st.get("version"), st.get("uptime_s"), st.get("time"))]
    lines.append("tables: " + ", ".join(
        "{}={}".format(k, v) for k, v in (st.get("tables") or {}).items()))
    for f in st.get("feeds") or []:
        lines.append("  {} last={} quote_age={}s candles={}".format(
            f.get("symbol"), f.get("last"), f.get("quote_age_s"), f.get("candles")))
    if st.get("kill_switches"):
        lines.append("KILL-SWITCH ENGAGED on {} account(s)".format(st.get("kill_switches")))
    return True, "\n".join(lines), st


def _cmd_risk(conn, user):
    try:
        from core.risk import get_limits
        lim = get_limits(conn, int(user.get("id")))
    except Exception as e:
        return False, "risk failed: {}".format(e), None
    keys = ("max_position_qty", "max_notional", "max_daily_loss",
            "max_trades_per_hour", "kill_switch")
    lines = ["{} = {}".format(k, lim.get(k)) for k in keys if k in lim]
    return True, "\n".join(lines) or str(lim), {"limits": dict(lim)}


def _cmd_tier(user):
    tier = str(user.get("tier", "Free"))
    try:
        from core.tiers import get_entitlements
        ent = get_entitlements(tier)
    except Exception:
        ent = {"tier": tier}
    return True, "tier {}  live={}  methods={}".format(
        tier, ent.get("live_enabled"), ent.get("methods_allowed")), dict(ent)


def execute(conn, user, text):
    """Run a terminal command. Returns (ok: bool, output: str, data: dict|None)."""
    try:
        verb, args = parse_command(text)
    except ValueError:
        return False, "empty command — type 'help'", None
    ud = dict(user or {})
    try:
        if verb in ("help", "?"):
            return True, HELP_TEXT, None
        if verb == "quote":
            return _cmd_quote(conn, args)
        if verb in ("buy", "sell"):
            return _cmd_trade(conn, ud, verb, args)
        if verb in ("flatten", "close"):
            return _cmd_flatten(conn, ud, args)
        if verb in ("positions", "portfolio", "pos"):
            return _cmd_positions(conn, ud)
        if verb == "orders":
            return _cmd_orders(conn, ud, args)
        if verb in ("signal", "sig"):
            return _cmd_signal(conn, args)
        if verb in ("backtest", "bt"):
            return _cmd_backtest(conn, ud, args)
        if verb in ("screener", "scan"):
            return _cmd_screener(conn, args)
        if verb in ("arb", "arbitrage"):
            return _cmd_arb(conn, ud, args)
        if verb in ("venues", "venue", "books"):
            return _cmd_venues(conn, ud, args)
        if verb == "status":
            return _cmd_status(conn)
        if verb == "risk":
            return _cmd_risk(conn, ud)
        if verb in ("tier", "me"):
            return _cmd_tier(ud)
        if verb == "broker":
            return _cmd_broker(conn, ud, args)
        return False, "unknown command '{}' — type 'help'".format(verb), None
    except Exception as e:
        return False, "error: {}".format(e), None
