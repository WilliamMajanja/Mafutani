"""Deterministic event-loop backtester. Stdlib only, import-safe.

run_backtest(candles, strategy_kind, params, fee_bps, slippage_bps)
  -> {"trades", "metrics": {...}, "equity": [...]}.

Uses engines.signals.preview_signal on a rolling window (the same
function the live board uses, so there is no live/backtest drift).
Flat -> signal side opens a fixed-qty long/short at close +/- slippage.
Exits: opposite signal, time-stop after max_hold_bars, or optional
tp_bps/sl_bps intrabar targets checked against bar high/low. Fees are
charged per side (notional x fee_bps). Fully deterministic: no RNG.
"""

import math

try:
    from engines.signals import preview_signal
except ImportError:  # pragma: no cover - direct-path execution
    from signals import preview_signal

try:
    from engines import cotgate
except ImportError:  # pragma: no cover - direct-path execution
    cotgate = None

try:
    from engines import significance as sig
except ImportError:  # pragma: no cover - direct-path execution
    import significance as sig

ANNUALIZATION_1M = math.sqrt(252 * 390)  # 1-minute bars
SEC_PER_YEAR = 365 * 24 * 3600
SEC_PER_DAY = 24 * 3600

# How far back a backtest may reach. One constant, enforced -- this
# replaces a per-tier `backtest_days` field that was declared in
# core/tiers.py, seeded into the tiers table, and read by nothing, so all
# four tiers silently got the same 20,000-bar cap (13.9 days) regardless
# of what their entitlement claimed.
BACKTEST_WINDOW_DAYS = 180
BARS_PER_DAY = 24 * 60                      # 1-minute candles
BACKTEST_MAX_BARS = BACKTEST_WINDOW_DAYS * BARS_PER_DAY   # 259,200

# Equity points persisted and returned per backtest. A six-month run
# produces ~259k; storing all of them would bloat every row for a curve
# nobody reads at that resolution.
EQUITY_MAX_POINTS = 5000


def _num(v, default=0.0):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(x) or math.isinf(x):
        return default
    return x


def _bar(c, key, aliases, default=0.0):
    if isinstance(c, dict):
        for k in (key,) + tuple(aliases):
            if k in c and c[k] is not None:
                return _num(c[k], default)
        return default
    return default


def _o(c):
    return _bar(c, "o", ("open",))


def _h(c):
    return _bar(c, "h", ("high",))


def _l(c):
    return _bar(c, "l", ("low",))


def _c(c):
    return _bar(c, "c", ("close",))


def _ts(c, fallback):
    v = _bar(c, "ts", ("time", "t"), default=None)
    if v is None:
        return fallback
    try:
        return int(v)
    except (TypeError, ValueError):
        return fallback


def _quote_for(close, ts, spread_bps=1.0):
    half = close * float(spread_bps) / 20000.0
    return {"bid": close - half, "ask": close + half, "last": close,
            "ts": ts}


def _sharpe_from_equity(equity):
    if len(equity) < 3:
        return 0.0
    rets = []
    for i in range(1, len(equity)):
        prev = equity[i - 1]
        if prev > 0:
            rets.append(equity[i] / prev - 1.0)
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / len(rets)
    sd = math.sqrt(var)
    if sd <= 0:
        return 0.0
    return (m / sd) * ANNUALIZATION_1M


def _trade_bps(t):
    """Per-trade return in bps, measured against the capital at risk.

    This is the series the significance tests need: a per-trade figure on
    a fixed notional, so a 1.0 mean is one basis point per trade
    regardless of the instrument's price level or the position size.

    Gross, not net. The loop below has already subtracted fee_bps on both
    sides and slipped the entry and the exit, so `pnl` is the return the
    venue would actually have given. `significance.net_returns` subtracts
    its own cost allowance on top of this, which is why the caller must
    pass cost_bps=0 when the trade list is already net -- see
    significance_for_trades.
    """
    entry = _num(t.get("entry"), 0.0)
    qty = abs(_num(t.get("qty"), 0.0))
    notional = entry * qty
    if entry <= 0 or qty <= 0 or notional <= 0:
        return None
    return _num(t.get("pnl"), 0.0) / notional * 10000.0


def _n_trials(params):
    """How many trials this project is implicitly running.

    Taken from params when the caller states it, otherwise derived from
    the strategy registry via significance.count_trials. Under-counting
    this is the single most common way to make a DSR meaningless -- the
    whole point of deflating is to account for the other things that were
    tried against the same tape.
    """
    n = _num((params or {}).get("n_trials"), 0.0)
    if n >= 1:
        return int(n)
    try:
        from engines.signals import STRATEGIES
    except ImportError:  # pragma: no cover - direct-path execution
        try:
            from signals import STRATEGIES
        except ImportError:
            return 1
    grid = _num((params or {}).get("n_param_combos"), 1.0)
    n_trials = sig.count_trials(STRATEGIES, max(1, int(grid)))
    # Every COT-gated configuration is another thing tried against the
    # same tape: a different percentile threshold is a different
    # hypothesis, exactly as a different EMA period would be. Counting
    # only the registry would score a sweep over {70, 80, 90, 95} as one
    # trial, which is precisely the under-counting deflation exists to
    # correct.
    n_trials += _cot_grid_trials(params)
    return max(1, n_trials)


def _cot_grid_trials(params):
    """Extra trials contributed by sweeping COT gate parameters.

    Counted only when the operator actually varies them. A run at the
    defaults is one more strategy, already counted by the registry; a
    sweep over four thresholds is four more.
    """
    p = params or {}
    varied = 0
    for key in ("cot_max_percentile", "cot_min_percentile",
                "cot_category", "cot_lag_days"):
        raw = p.get(key)
        if raw is None:
            continue
        if isinstance(raw, (list, tuple)):
            varied = max(varied, len(raw))
        elif isinstance(raw, str) and "," in raw:
            varied = max(varied,
                         len([x for x in raw.split(",") if x.strip()]))
    # `ema_trend_cot` itself is already in STRATEGIES, so only what
    # exceeds the single default configuration adds trials here.
    return max(0, varied - 1)


def significance_for_trades(trades, params=None, label="",
                             periods_per_year=252):
    """Attach a significance verdict to a finished backtest.

    Returns the report dict from `significance.edge_report`, or a refusal
    dict when the module cannot be imported. Never raises: a backtest that
    priced a real tape should still return its trades if the statistics
    are unavailable, and it must never return a *confident* answer it
    could not compute.

    Cost handling is the subtle part. The event loop already charges
    fee_bps on entry and exit and slips both prices, so the trade list is
    net of costs. Passing that same fee schedule into edge_report as
    `cost_bps` would subtract it twice and report a profitable strategy
    as untradeable. So the trades are passed as gross-equivalent with
    cost_bps=0.0, and the realised fee+slippage drag is reported
    separately as `cost_drag_bps` -- that is the honest number, because
    it is what the backtest actually modelled rather than an assumption
    layered on top of it.
    """
    params = params or {}
    label = label or str(params.get("strategy") or "")
    try:
        returns = [r for r in (_trade_bps(t) for t in (trades or []))
                   if r is not None]
        rep = sig.edge_report(returns, cost_bps=0.0,
                              n_trials=_n_trials(params),
                              periods_per_year=periods_per_year,
                              label=label)
    except Exception as e:  # pragma: no cover - defensive
        return {"ok": False, "verdict": "not evaluable",
                "reasons": ["significance unavailable: {}".format(e)],
                "trades": 0, "dsr": None, "psr": None}
    rep["ok"] = True

    # What the modelled fees and slippage actually cost, in bps per trade.
    # Reported next to the verdict because "no edge after costs" should
    # be traceable to a number rather than to a flag.
    fee_bps = abs(_num(params.get("fee_bps"), 0.0))
    slip_bps = abs(_num(params.get("slippage_bps"), 0.0))
    rep["realised_net_bps"] = round(_mean(returns), 4) if returns else None
    rep["cost_drag_bps"] = round(2.0 * (fee_bps + slip_bps), 4)
    return rep


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _sig_params(params, fee_bps, slippage_bps, strategy_kind):
    """Params plus the cost schedule the loop actually charged.

    `significance_for_trades` needs the fee and slippage the run used, and
    they arrive as arguments to run_backtest rather than in params. Threading
    them through here keeps the significance helper's signature about
    verdicts rather than about plumbing.
    """
    out = dict(params or {})
    out["fee_bps"] = fee_bps
    out["slippage_bps"] = slippage_bps
    out.setdefault("strategy", str(strategy_kind or ""))
    return out


def _max_drawdown(equity):
    if not equity:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for e in equity[1:]:
        if e > peak:
            peak = e
        elif peak > 0:
            dd = (peak - e) / peak
            if dd > mdd:
                mdd = dd
    return mdd


def run(conn=None, strategy=None, symbol=None, params=None, from_ts=0, to_ts=0,
        limit=None):
    """DB-entry wrapper used by api/routes._engine_backtest.

    Loads 1m candles for the symbol/range window from the sqlite connection,
    then delegates to run_backtest() so the API and the live board share the
    exact same signal loop. Returns {"trades", "metrics", "equity"} plus a
    "coverage" block saying what tape the run actually saw.
    """
    limit = int(limit or BACKTEST_MAX_BARS)
    candles = []
    if conn is not None and symbol:
        try:
            q = "SELECT * FROM candles_1m WHERE symbol = ?"
            args = [symbol]
            if from_ts:
                q += " AND ts >= ?"
                args.append(int(from_ts))
            if to_ts:
                q += " AND ts <= ?"
                args.append(int(to_ts))
            q += " ORDER BY ts ASC LIMIT ?"
            args.append(limit)
            rows = conn.execute(q, args).fetchall()
            for r in rows:
                try:
                    candles.append({k: r[k] for k in r.keys()})
                except Exception:
                    candles.append(r)
        except Exception:
            candles = []
    res = run_backtest(candles, str(strategy or ""), params or {},
                       cot_timeline=_cot_timeline_for(conn, params))
    res["coverage"] = coverage(candles, from_ts, to_ts)
    return res


def _cot_timeline_for(conn, params):
    """Build the COT publication timeline once per run, or None.

    Built once and bisected per bar. The alternative -- reading a
    percentile out of SQLite 259,200 times -- is the reason this is not
    done inline.

    Returns None when the strategy is not COT-gated or the store is
    unavailable, and the strategy then refuses with "cot-no-gate" rather
    than trading without the veto it was configured to have.
    """
    if cotgate is None or conn is None:
        return None
    p = params or {}
    contract = str(p.get("cot_contract") or "").strip()
    report = str(p.get("cot_report") or "disagg_comb").strip()
    category = str(p.get("cot_category") or cotgate.DEFAULT_CATEGORY)
    if not contract:
        return None
    try:
        from data import cotstore as S
        rows = S.positions(conn, report=report, contract_code=contract,
                           category=category, limit=4000)
        if not rows:
            return None
        return cotgate.build_timeline(
            rows, category=category,
            lag_days=int(p.get("cot_lag_days", cotgate.DEFAULT_LAG_DAYS)))
    except Exception:
        return None


def coverage(candles, from_ts=0, to_ts=0):
    """What tape the run actually saw, against what was asked for.

    The window is a permission, not a promise. Requesting six months on a
    symbol that only has thirty days of stored history returns thirty days
    of tape -- and a chart that renders as if it were six months would be
    lying by omission. So the realised span is reported next to the
    requested one and a shortfall is stated outright.
    """
    out = {
        "requested_days": BACKTEST_WINDOW_DAYS,
        "requested_from_ts": int(from_ts or 0),
        "requested_to_ts": int(to_ts or 0),
        "bars": len(candles or []),
        "max_bars": BACKTEST_MAX_BARS,
        "from_ts": None,
        "to_ts": None,
        "days": 0.0,
        "truncated_by_cap": False,
        "short_of_window": False,
        "note": None,
    }
    bars = list(candles or [])
    if not bars:
        out["note"] = "no candles stored for this symbol"
        return out
    out["from_ts"] = _ts(bars[0], 0)
    out["to_ts"] = _ts(bars[-1], 0)
    span_s = max(0, out["to_ts"] - out["from_ts"])
    # Calendar days, straight from the day length. SEC_PER_YEAR is a
    # 365-day year for annualising a CAGR, so reusing it here and scaling
    # by 365.25 landed the span at 180.12 days for a 180-day window.
    out["days"] = round(span_s / float(SEC_PER_DAY), 2)
    out["truncated_by_cap"] = len(bars) >= out["max_bars"]
    if out["truncated_by_cap"]:
        out["note"] = ("tape is longer than the {}-day window; the run used "
                       "the most recent {} bars".format(
                           BACKTEST_WINDOW_DAYS, out["max_bars"]))
    elif out["days"] < out["requested_days"] - 0.5:
        out["short_of_window"] = True
        out["note"] = ("only {:.1f} days of history stored for this symbol, "
                       "not the full {} requested".format(
                           out["days"], out["requested_days"]))
    return out


def downsample_equity(equity, max_points=EQUITY_MAX_POINTS):
    """Strided equity curve that keeps the first and last point.

    A six-month run yields ~259k points. Truncating to the first 5,000 --
    which is what the API did before -- silently drops 98% of the curve
    and leaves the chart showing three weeks of a six-month backtest with
    nothing on screen saying so. Striding keeps the whole span visible at
    5,000 points, which is far more resolution than a 1,000px canvas has.

    Returns (points, was_downsampled).
    """
    pts = list(equity or [])
    if len(pts) <= max_points:
        return pts, False
    step = int(math.ceil(len(pts) / float(max_points)))
    out = pts[::step]
    if out and out[-1] != pts[-1]:
        out.append(pts[-1])
    return out, True


def run_backtest(candles, strategy_kind, params=None, fee_bps=2.0,
                 slippage_bps=2.0, cot_timeline=None):
    """Run the event loop. Returns {"trades", "metrics", "equity"}.

    `cot_timeline` is an `engines.cotgate` timeline, pre-built. Passing
    one is what lets `ema_trend_cot` evaluate the gate at each bar with a
    bisect instead of a database read; passing None makes that strategy
    refuse rather than trade ungated.
    """
    candles = list(candles or [])
    params = dict(params or {})
    fee_bps = _num(fee_bps, 0.0)
    slippage_bps = max(0.0, _num(slippage_bps, 0.0))
    qty = _num(params.get("qty", 1.0), 1.0)
    if qty <= 0:
        qty = 1.0
    max_hold = int(_num(params.get("max_hold_bars", 20), 20))
    if max_hold < 1:
        max_hold = 1
    tp_bps = params.get("tp_bps")
    sl_bps = params.get("sl_bps")
    tp_bps = _num(tp_bps, 0.0) if tp_bps is not None else 0.0
    sl_bps = _num(sl_bps, 0.0) if sl_bps is not None else 0.0
    start_eq = _num(params.get("starting_equity", 10000.0), 10000.0)
    if start_eq <= 0:
        start_eq = 10000.0
    warmup = int(_num(params.get("warmup_bars", 30), 30))
    if warmup < 1:
        warmup = 1
    quote_spread = _num(params.get("quote_spread_bps", 1.0), 1.0)

    n = len(candles)
    closes = [_c(c) for c in candles]
    if n == 0 or any(x <= 0 for x in closes):
        metrics = {"trades": 0, "wins": 0, "win_rate": 0.0, "pf": 0.0,
                   "maxDD": 0.0, "sharpe": 0.0, "cagr": 0.0, "avg_hold": 0.0}
        return {"trades": [], "metrics": metrics, "equity": [],
                "significance": significance_for_trades(
                    [], _sig_params(params, fee_bps, slippage_bps,
                                    strategy_kind))}

    slip = slippage_bps / 10000.0
    trades = []
    equity = []
    realized = 0.0
    pos = None  # {side, entry, qty, entry_idx, entry_ts, fees}
    # Only COT-gated strategies get a gate. Handing one to `ema_trend`
    # would be harmless (it ignores the argument) but it would attach a
    # `cot_gate` report to a run that never consulted COT, which is a
    # claim the result has not earned.
    cot_gated = bool(cot_timeline) and "cot" in str(strategy_kind or "")
    cot = cotgate.Gate(cot_timeline) if (cotgate and cot_gated) else None
    cot_blocks = 0
    cot_no_data = 0
    cot_no_gate = 0

    def fee_for(notional):
        return abs(notional) * fee_bps / 10000.0

    def close_pos(exit_px, exit_idx, exit_ts, reason):
        nonlocal realized, pos
        direction = 1.0 if pos["side"] == "buy" else -1.0
        gross = (exit_px - pos["entry"]) * direction * pos["qty"]
        fee_x = fee_for(exit_px * pos["qty"])
        pnl = gross - pos["fees"] - fee_x
        realized += pnl
        trades.append({
            "side": pos["side"],
            "qty": pos["qty"],
            "entry": pos["entry"],
            "exit": exit_px,
            "entry_idx": pos["entry_idx"],
            "exit_idx": exit_idx,
            "entry_ts": pos["entry_ts"],
            "exit_ts": exit_ts,
            "hold_bars": exit_idx - pos["entry_idx"],
            "pnl": pnl,
            "reason": reason,
        })
        pos = None

    start = min(warmup, n - 1)
    # Rolling signal window: indicators (EMA/ATR/VWAP) only need the most
    # recent bars to converge, so cap the window to keep runs O(n) instead
    # of O(n^2) on 30-day/large candle sets.
    lookback = max(warmup, int(_num(params.get("signal_lookback_bars", 250), 250)))
    for i in range(start, n):
        lo = max(0, i - lookback + 1)
        window = candles[lo: i + 1]
        ts = _ts(candles[i], i)
        quote = _quote_for(closes[i], ts, quote_spread)
        # Hand the signal function the matching slice of the close
        # series. The window is a contiguous slice of the tape, so its
        # closes are exactly closes[lo:i+1] -- re-deriving them from
        # candle dicts on every bar was the dominant cost in a run
        # (measured: 800k float coercions per 1200 bars).
        # `cot` is passed only when there is a gate. Passing `cot=None`
        # unconditionally would change the call for every existing
        # strategy -- including for the parity harness, which patches
        # preview_signal with a reference implementation that predates
        # the argument.
        if cot is not None:
            sig = preview_signal(strategy_kind, window, quote, params,
                                 closes=closes[lo: i + 1], cot=cot)
        else:
            sig = preview_signal(strategy_kind, window, quote, params,
                                 closes=closes[lo: i + 1])
        side = sig.get("side")

        # Count what the gate did, so the result can say how many entries
        # it removed and how many independent observations it had. A gate
        # that silently blocks everything looks identical to a strategy
        # that simply never fires.
        _r = str(sig.get("reason") or "")
        if _r.startswith("cot-blocked-"):
            cot_blocks += 1
        elif _r == "cot-no-data":
            cot_no_data += 1
        elif _r == "cot-no-gate":
            cot_no_gate += 1

        if pos is not None:
            direction = 1.0 if pos["side"] == "buy" else -1.0
            exited = False
            # intrabar TP/SL targets
            if tp_bps > 0:
                tp_px = pos["entry"] * (1.0 + direction * tp_bps / 10000.0)
                hit = (_h(candles[i]) >= tp_px) if direction > 0 \
                    else (_l(candles[i]) <= tp_px)
                if hit:
                    close_pos(tp_px, i, ts, "take-profit")
                    exited = True
            if not exited and sl_bps > 0:
                sl_px = pos["entry"] * (1.0 - direction * sl_bps / 10000.0)
                hit = (_l(candles[i]) <= sl_px) if direction > 0 \
                    else (_h(candles[i]) >= sl_px)
                if hit:
                    close_pos(sl_px, i, ts, "stop-loss")
                    exited = True
            if not exited and side is not None and side != pos["side"]:
                exit_px = closes[i] * (1.0 - direction * slip)
                close_pos(exit_px, i, ts, "opposite-signal")
                exited = True
            if not exited and (i - pos["entry_idx"]) >= max_hold:
                exit_px = closes[i] * (1.0 - direction * slip)
                close_pos(exit_px, i, ts, "time-stop")
                exited = True

        if pos is None and side in ("buy", "sell"):
            direction = 1.0 if side == "buy" else -1.0
            entry_px = closes[i] * (1.0 + direction * slip)
            fee_e = fee_for(entry_px * qty)
            pos = {"side": side, "entry": entry_px, "qty": qty,
                   "entry_idx": i, "entry_ts": ts, "fees": fee_e}

        if pos is None:
            equity.append(start_eq + realized)
        else:
            direction = 1.0 if pos["side"] == "buy" else -1.0
            mtm = (closes[i] - pos["entry"]) * direction * pos["qty"]
            equity.append(start_eq + realized + mtm - pos["fees"])

    if pos is not None:  # liquidate at final close
        direction = 1.0 if pos["side"] == "buy" else -1.0
        exit_px = closes[-1] * (1.0 - direction * slip)
        close_pos(exit_px, n - 1, _ts(candles[-1], n - 1), "liquidate")
        equity.append(start_eq + realized)

    n_trades = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] < 0)
    if n_trades == 0:
        pf = 0.0
    elif gross_loss > 0:
        pf = gross_profit / gross_loss
    else:
        pf = 10.0  # wins only; JSON-safe stand-in for infinity
    end_eq = equity[-1] if equity else start_eq
    try:
        years = (_ts(candles[-1], n - 1) - _ts(candles[0], 0)) / SEC_PER_YEAR
    except Exception:
        years = 0.0
    if years > 0 and start_eq > 0 and end_eq > 0:
        cagr = (end_eq / start_eq) ** (1.0 / years) - 1.0
    else:
        cagr = 0.0
    avg_hold = sum(t["hold_bars"] for t in trades) / n_trades if n_trades else 0.0

    metrics = {
        "trades": n_trades,
        "wins": wins,
        "win_rate": wins / n_trades if n_trades else 0.0,
        "pf": pf,
        "maxDD": _max_drawdown(equity),
        "sharpe": _sharpe_from_equity(equity),
        "cagr": cagr,
        "avg_hold": avg_hold,
    }
    # The verdict rides alongside the metrics rather than inside them, so
    # a caller reading `metrics.sharpe` gets the number it always got,
    # and a caller that wants to know whether that number means anything
    # has one obvious place to look. `sharpe` here is the annualised
    # equity-curve ratio and is NOT significance-tested: the tests run on
    # per-trade bps, on a different sample, with deflation applied.
    out = {"trades": trades, "metrics": metrics, "equity": equity,
           "significance": significance_for_trades(
               trades,
               _sig_params(params, fee_bps, slippage_bps, strategy_kind))}
    if cot is not None:
        out["cot_gate"] = _cot_gate_report(
            cot, trades, cot_blocks, cot_no_data, cot_no_gate, params)
    return out


def _cot_gate_report(gate, trades, blocked, no_data, no_gate, params):
    """What the COT gate actually did, and how much it could have known.

    `observations` is the number that matters: it is the count of
    independent COT prints in the run, not the bar count. A six-month
    backtest holds ~26 of them, so a Sharpe over those trades is bounded
    by 26 observations no matter how many bars produced it.

    `bars_vetoed` is deliberately not called `blocked_entries`. The gate
    runs before the EMA, so it declines on every bar where the crowd is
    crowded -- including the overwhelming majority where the EMA had no
    cross and was never going to trade. It is an upper bound on vetoed
    entries, not a count of them. Counting exactly would mean evaluating
    the EMA twice per bar to find out what would have happened after
    being told no.
    """
    tl = gate.timeline
    trials = len(tl.get("available_from") or [])
    return {
        "used": True,
        "category": (tl.get("category")
                     or params.get("cot_category")
                     or cotgate.DEFAULT_CATEGORY),
        "lag_days": tl.get("lag_days", cotgate.DEFAULT_LAG_DAYS),
        "bars_vetoed": blocked,
        "bars_with_no_cot_data": no_data,
        "bars_with_no_gate": no_gate,
        "observations": trials,
        "trades": len(trades),
        "first_publication": tl.get("first_publication"),
        "last_publication": tl.get("last_publication"),
        "note": ("the gate can only change its mind once per publication, so "
                 "the effective sample is {} COT observations, not the bar "
                 "count; bars_vetoed is an upper bound on vetoed entries, "
                 "not a count of them".format(trials)),
    }
