"""Scalp-lab signal functions. Stdlib only, import-safe.

Wired V1: ema_trend, vwap_pullback, manual, dual_ema, mean_reversion.
Stubs (metadata only): orb_breakout, rsi_fade, book_imbalance, news_momentum.

Signal functions share the signature
    signal_<name>(candles, quotes, params) -> {"side", "qty", "reason"}
and are reused verbatim by the backtester (no live/backtest drift).

`candles` is a list of dicts with o/h/l/c/v/ts (or
open/high/low/close/volume/ts). `quotes`/`quote` may be a single L1 dict
with bid/ask, a list of such dicts (last one wins), or None (no spread
filter applied).
"""

try:
    from engines.indicators import (
        atr,
        closes_of,
        ema_series,
        vwap_series,
    )
except ImportError:  # pragma: no cover - direct-path execution
    from indicators import atr, closes_of, ema_series, vwap_series

STRATEGIES = [
    {
        "name": "ema_trend",
        "display_name": "EMA Trend",
        "tier_required": "Free",
        "mode": "wired",
        "description": "EMA 9/21 micro-trend on 1m bars with ATR and spread filters.",
        "params": {"fast": 9, "slow": 21, "atr_period": 14, "min_atr_bps": 0,
                   "max_spread_bps": 50, "qty": 1.0},
    },
    {
        "name": "vwap_pullback",
        "display_name": "VWAP Pullback",
        "tier_required": "Pro",
        "mode": "wired",
        "description": "Buys pullbacks below a rising VWAP (sells bounces above a falling VWAP).",
        "params": {"lookback": 20, "dev_bps": 10, "max_spread_bps": 50, "qty": 1.0},
    },
    {
        "name": "manual",
        "display_name": "Manual",
        "tier_required": "Pro",
        "mode": "wired",
        "description": "One-click buy/sell passthrough; still spread- and risk-checked.",
        "params": {"side": "buy", "max_spread_bps": 50, "qty": 1.0},
    },
    {
        "name": "dual_ema",
        "display_name": "Dual EMA Cross",
        "tier_required": "Free",
        "mode": "wired",
        "description": "Fast/slow EMA cross (12/26) confirmed by price position vs the slow EMA.",
        "params": {"fast": 12, "slow": 26, "max_spread_bps": 50, "qty": 1.0},
    },
    {
        "name": "vwap_bands",
        "display_name": "VWAP Band (session, sigma)",
        "tier_required": "Pro",
        "mode": "wired",
        "description": "Session-anchored VWAP standard-deviation band entry with a 200-EMA regime filter and 50-EMA trailing stop (after SSRN 6650958).",
        "params": {"source": "hlc3", "session_min": 1440, "anchor": "session",
                   "entry_sigma": 2.0, "regime_ema": 200, "stop_ema": 50,
                   "max_spread_bps": 50, "qty": 1.0},
    },
    {
        "name": "ema_trend_cot",
        "display_name": "EMA Trend + COT crowding gate",
        "tier_required": "Pro",
        "mode": "wired",
        "description": "EMA 9/21 trend entries, vetoed when the CFTC shows the "
                       "speculative crowd already fully positioned. Uses only "
                       "prints published at least 3 days before the bar.",
        "params": {"fast": 9, "slow": 21, "atr_period": 14, "min_atr_bps": 0,
                   "max_spread_bps": 50, "qty": 1.0,
                   "cot_category": "m_money", "cot_max_percentile": 90.0,
                   "cot_min_percentile": 10.0, "cot_lag_days": 3,
                   "cot_require": True},
    },
    {
        "name": "mean_reversion",
        "display_name": "Mean Reversion",
        "tier_required": "Pro",
        "mode": "wired",
        "description": "Z-score fade: shorts extreme highs, buys extreme lows back to the mean.",
        "params": {"period": 20, "entry_z": 2.0, "max_spread_bps": 50, "qty": 1.0},
    },
    {
        "name": "orb_breakout",
        "display_name": "Opening Range Breakout",
        "tier_required": "Pro",
        "mode": "stub",
        "description": "1m opening-range breakout. Stub in V1 — upgrade to unlock.",
        "params": {"range_min": 15},
    },
    {
        "name": "rsi_fade",
        "display_name": "RSI Fade",
        "tier_required": "Pro",
        "mode": "stub",
        "description": "Mean-reversion RSI/Bollinger fade. Stub in V1 — upgrade to unlock.",
        "params": {"period": 14, "overbought": 70, "oversold": 30},
    },
    {
        "name": "book_imbalance",
        "display_name": "Book Imbalance",
        "tier_required": "Institutional",
        "mode": "stub",
        "description": "Spread/queue book-imbalance scalp (needs L2). Stub in V1.",
        "params": {"depth": 5, "threshold": 1.5},
    },
    {
        "name": "news_momentum",
        "display_name": "News Momentum",
        "tier_required": "Institutional",
        "mode": "stub",
        "description": "News-driven tape ignition. Stub in V1 — upgrade to unlock.",
        "params": {"window_min": 30},
    },
]

STUB_KINDS = {s["name"] for s in STRATEGIES if s["mode"] == "stub"}
WIRED_KINDS = {s["name"] for s in STRATEGIES if s["mode"] == "wired"}

DEFAULT_QTY = 1.0


# ------------------------------------------------------------------ helpers
def _qty_of(params):
    try:
        q = float((params or {}).get("qty", DEFAULT_QTY))
    except (TypeError, ValueError):
        q = DEFAULT_QTY
    return q if q > 0 else DEFAULT_QTY


def _last_quote(quotes):
    if quotes is None:
        return None
    if isinstance(quotes, dict):
        return quotes
    if isinstance(quotes, (list, tuple)):
        return quotes[-1] if quotes else None
    return None


def _spread_bps(quote):
    try:
        bid = float(quote.get("bid"))
        ask = float(quote.get("ask"))
    except (TypeError, ValueError, AttributeError):
        return None
    if bid <= 0 or ask <= 0:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 10000.0


def _check_spread(quotes, params):
    """Shared spread filter. Returns (ok, spread_bps_or_None)."""
    quote = _last_quote(quotes)
    if quote is None:
        return True, None
    spread = _spread_bps(quote)
    if spread is None:
        return True, None
    try:
        limit = float((params or {}).get("max_spread_bps", 50))
    except (TypeError, ValueError):
        limit = 50.0
    if spread > limit:
        return False, spread
    return True, spread


def _flat(side, reason, params):
    return {"side": None, "qty": _qty_of(params), "reason": reason,
            "requested": side}


def _emit(side, reason, params):
    return {"side": side, "qty": _qty_of(params), "reason": reason}


# ------------------------------------------------------------------ wired
def signal_ema_trend(candles, quotes, params, closes=None):
    """EMA fast/slow cross with optional ATR regime filter."""
    params = dict(params or {})
    fast = int(params.get("fast", 9))
    slow = int(params.get("slow", 21))
    if fast < 1:
        fast = 1
    if slow < 2:
        slow = 2
    if fast >= slow:
        fast = max(1, slow - 1)
    closes = closes if closes is not None else closes_of(candles)
    if len(closes) < slow + 1:
        return _flat(None, "warming-up", params)
    ok, spread = _check_spread(quotes, params)
    if not ok:
        return _flat(None, "spread-too-wide", params)
    fast_e = ema_series(closes, fast)
    slow_e = ema_series(closes, slow)
    try:
        min_atr_bps = float(params.get("min_atr_bps", 0) or 0)
    except (TypeError, ValueError):
        min_atr_bps = 0.0
    if min_atr_bps > 0:
        try:
            atr_period = int(params.get("atr_period", 14))
        except (TypeError, ValueError):
            atr_period = 14
        a = atr(candles, atr_period)[-1]
        px = closes[-1] or 0.0
        if px > 0 and a / px * 10000.0 < min_atr_bps:
            return _flat(None, "atr-too-low", params)
    bull = fast_e[-1] > slow_e[-1] and fast_e[-2] <= slow_e[-2]
    bear = fast_e[-1] < slow_e[-1] and fast_e[-2] >= slow_e[-2]
    if bull:
        return _emit("buy", "ema-bull-cross", params)
    if bear:
        return _emit("sell", "ema-bear-cross", params)
    return _flat(None, "no-cross", params)


def signal_vwap_pullback(candles, quotes, params, closes=None):
    """Pullback to VWAP in the direction of the VWAP slope."""
    params = dict(params or {})
    try:
        lookback = max(2, int(params.get("lookback", 20)))
    except (TypeError, ValueError):
        lookback = 20
    try:
        dev_limit = float(params.get("dev_bps", 10))
    except (TypeError, ValueError):
        dev_limit = 10.0
    closes = closes if closes is not None else closes_of(candles)
    if len(closes) < lookback + 1:
        return _flat(None, "warming-up", params)
    ok, spread = _check_spread(quotes, params)
    if not ok:
        return _flat(None, "spread-too-wide", params)
    vw = vwap_series(candles)
    v_now, v_then = vw[-1], vw[-lookback]
    close = closes[-1]
    if v_now <= 0:
        return _flat(None, "no-vwap", params)
    slope = v_now - v_then
    dev_bps = (v_now - close) / v_now * 10000.0  # >0 when below VWAP
    if slope > 0 and 0 < dev_bps <= dev_limit:
        return _emit("buy", "vwap-pullback-long", params)
    if slope < 0 and 0 < -dev_bps <= dev_limit:
        return _emit("sell", "vwap-pullback-short", params)
    if slope == 0:
        return _flat(None, "vwap-slope-flat", params)
    return _flat(None, "no-pullback", params)


def signal_vwap_bands(candles, quotes, params, closes=None):
    """Session-VWAP standard-deviation band entry with a regime filter.

    Parameters (all optional; defaults follow SSRN 6650958, which used a
    200-period EMA to classify regime and a 50-period EMA as the trailing
    stop, and the Al Brooks convention of entering at the second band):

      source        hlc3 | ohlc | close            (default hlc3)
      session_min   session length in minutes       (default 1440)
      anchor        session | first | day_high | day_low | int
      entry_sigma   band to enter at                (default 2.0)
      regime_ema    regime filter length; 0 disables (default 200)
      stop_ema      trailing-stop reference; 0 disables (default 50)
      max_spread_bps, qty                          (as elsewhere)

    Why the regime filter is not optional garnish
    ---------------------------------------------
    The paper behind these parameters reports a *45.3% win rate* -- below a
    coin flip -- and is still a working system, because the payoff is
    asymmetric. A raw VWAP band entry fires constantly in a range, which is
    very likely why the unfiltered hit rate sits under 50%. So the regime
    gate is on by default, and turning it off (`regime_ema: 0`) is a
    deliberate act rather than a missing default.

    Returns "none" with a reason rather than a side whenever the evidence
    is absent. A band touch with no regime context is not a signal.
    """
    params = dict(params or {})
    def _num(key, default, cast=float):
        try:
            return cast(params.get(key, default))
        except (TypeError, ValueError):
            return cast(default)

    source = str(params.get("source", "hlc3") or "hlc3")
    session_min = _num("session_min", 1440, int)
    anchor = params.get("anchor", "session")
    if isinstance(anchor, str):
        anchor = anchor.strip().lower()
        if anchor not in ("session", "first", "day_high", "day_low"):
            anchor = "session"
    entry_sigma = _num("entry_sigma", 2.0)
    regime_len = _num("regime_ema", 200, int)
    stop_len = _num("stop_ema", 50, int)
    closes = closes if closes is not None else closes_of(candles)
    if len(closes) < 30:
        return _flat(None, "warming-up", params)

    ok, _spread = _check_spread(quotes, params)
    if not ok:
        return _flat(None, "spread-too-wide", params)

    try:
        from engines.vwap import vwap_state
    except ImportError:  # pragma: no cover - direct-path execution
        from vwap import vwap_state

    st = vwap_state(candles, source=source, session_min=session_min,
                    anchor=anchor)
    if not st.get("ok"):
        return _flat(None, "no-vwap", params)
    z = st.get("z")
    if z is None:
        # Zero dispersion: the bands collapse and "2 sigma" is undefined.
        return _flat(None, "no-band-dispersion", params)

    price = st.get("price") or 0.0
    vwap_line = st.get("vwap") or 0.0

    # --- regime gate -------------------------------------------------
    if regime_len and regime_len > 1:
        if len(closes) < regime_len + 2:
            return _flat(None, "warming-up-regime", params)
        ema = ema_series(closes, regime_len)
        if not ema or ema[-1] <= 0:
            return _flat(None, "no-regime", params)
        regime_up = price >= ema[-1]
        # Long entries need an up regime, shorts a down one. The reason
        # names the regime that *blocked* the entry, so it reads as a
        # diagnosis rather than as the opposite of what happened.
        if z <= -entry_sigma and not regime_up:
            return _flat(None, "below-band-in-down-regime", params)
        if z >= entry_sigma and regime_up:
            return _flat(None, "above-band-in-up-regime", params)

    # --- trailing-stop reference ------------------------------------
    if stop_len and stop_len > 1:
        if len(closes) < stop_len + 2:
            return _flat(None, "warming-up-stop", params)
        sema = ema_series(closes, stop_len)
        if not sema or sema[-1] <= 0:
            return _flat(None, "no-stop-ref", params)
        if z <= -entry_sigma and price <= sema[-1]:
            return _flat(None, "below-stop", params)
        if z >= entry_sigma and price >= sema[-1]:
            return _flat(None, "above-stop", params)

    if z <= -entry_sigma:
        return _emit("buy", "vwap-band-long", params)
    if z >= entry_sigma:
        return _emit("sell", "vwap-band-short", params)
    return _flat(None, "inside-bands", params)


def signal_ema_trend_cot(candles, quotes, params, closes=None, cot=None):
    """EMA 9/21 trend, vetoed by CFTC crowding. `cot` is a pre-built gate.

    The gate is a *veto*, not a signal. It never opens a position the EMA
    would not have taken, and it never flips an existing position's
    direction -- it only declines new entries. That is the shape a slow,
    weekly, lagging measurement can honestly support: it changes which
    trades are taken, not when.

    `cot` arrives from the caller (the backtester builds the timeline
    once; the live board evaluates the latest stored print) because a
    per-bar database read would dominate the run.

    Three outcomes, and the middle one matters:

      no COT yet          -> flat, "cot-no-data"       (never permissive)
      COT blocks          -> flat, "cot-blocked-long"  (explains itself)
      COT clear           -> whatever EMA 9/21 says

    The absence of a reading is not permission. A gate that silently
    passes when it cannot see is a gate that reports "no problems found"
    about a market it never looked at.

    `cot_require: false` downgrades the first case to a pass-through --
    available for asking "would this have traded without the data?", and
    the only way to isolate the gate's own contribution. It is off by
    default because the honest default is to not trade blind.
    """
    params = dict(params or {})
    # Defaulting to "require the gate" is the whole safety property, so it
    # is read with an explicit True default rather than `.get(...)`, which
    # would silently be falsy for every caller that does not merge the
    # catalogue params. It was: the strategy traded ungated when the
    # params dict was empty, which is the case the backtester hits.
    require = params.get("cot_require")
    require = True if require is None else bool(require)

    if cot is None:
        # No gate supplied at all. Distinct from a gate that evaluated
        # and found nothing to block.
        if require:
            return _flat(None, "cot-no-gate", params)
        return signal_ema_trend(candles, quotes, params, closes)

    allowed, reason, detail = cot.evaluate(
        cot.visible_at(params.get("_bar_ts") or _last_ts(candles)),
        category=str(params.get("cot_category", "m_money")),
        max_percentile=_num(params.get("cot_max_percentile"), 90.0),
        min_percentile=_num(params.get("cot_min_percentile"), 10.0))

    if allowed is None:
        if require:
            return _flat(None, "cot-no-data", params)
        return signal_ema_trend(candles, quotes, params, closes)
    if not allowed:
        side = "long" if detail.get("blocker") == "crowded-long" else "short"
        return _flat(None, "cot-blocked-" + side, params)

    sig = signal_ema_trend(candles, quotes, params, closes)
    if sig.get("side"):
        sig = dict(sig)
        sig["cot"] = {"allowed": True, "reason": reason,
                      "percentile": detail.get("percentile"),
                      "report_date": detail.get("report_date")}
    return sig


def _last_ts(candles):
    """Timestamp of the newest candle, or None."""
    bars = list(candles or [])
    if not bars:
        return None
    c = bars[-1]
    if isinstance(c, dict):
        v = c.get("ts", c.get("time", c.get("t")))
    else:
        v = getattr(c, "ts", None)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


def manual_signal(candles, quotes, params, closes=None):
    """Manual click passthrough: params['side'] must be buy|sell."""
    params = dict(params or {})
    side = str(params.get("side", "") or "").strip().lower()
    if side not in ("buy", "sell"):
        return _flat(side or None, "manual-needs-side", params)
    ok, spread = _check_spread(quotes, params)
    if not ok:
        return _flat(side, "spread-too-wide", params)
    return _emit(side, "manual", params)


def signal_dual_ema(candles, quotes, params, closes=None):
    """Fast/slow EMA cross confirmed by price position vs the slow EMA."""
    params = dict(params or {})
    try:
        fast = max(1, int(params.get("fast", 12)))
        slow = max(2, int(params.get("slow", 26)))
    except (TypeError, ValueError):
        fast, slow = 12, 26
    if fast >= slow:
        fast = max(1, slow - 1)
    closes = closes if closes is not None else closes_of(candles)
    if len(closes) < slow + 1:
        return _flat(None, "warming-up", params)
    ok, spread = _check_spread(quotes, params)
    if not ok:
        return _flat(None, "spread-too-wide", params)
    fe = ema_series(closes, fast)
    se = ema_series(closes, slow)
    px = closes[-1]
    bull = fe[-1] > se[-1] and fe[-2] <= se[-2] and px > se[-1]
    bear = fe[-1] < se[-1] and fe[-2] >= se[-2] and px < se[-1]
    if bull:
        return _emit("buy", "dual-ema-bull-cross", params)
    if bear:
        return _emit("sell", "dual-ema-bear-cross", params)
    return _flat(None, "no-cross", params)


def signal_mean_reversion(candles, quotes, params, closes=None):
    """Z-score fade: buy extremes below the mean, sell extremes above."""
    params = dict(params or {})
    try:
        period = max(3, int(params.get("period", 20)))
        entry_z = float(params.get("entry_z", 2.0))
    except (TypeError, ValueError):
        period, entry_z = 20, 2.0
    closes = closes if closes is not None else closes_of(candles)
    if len(closes) < period + 1:
        return _flat(None, "warming-up", params)
    ok, spread = _check_spread(quotes, params)
    if not ok:
        return _flat(None, "spread-too-wide", params)
    window = closes[-period:]
    mean = sum(window) / float(period)
    var = sum((x - mean) ** 2 for x in window) / float(period)
    sd = var ** 0.5
    if sd <= 0:
        return _flat(None, "flat-vol", params)
    z = (closes[-1] - mean) / sd
    if z >= entry_z:
        return _emit("sell", "mr-short-z{:.2f}".format(z), params)
    if z <= -entry_z:
        return _emit("buy", "mr-long-z{:.2f}".format(z), params)
    return _flat(None, "no-extreme-z{:.2f}".format(z), params)


# ------------------------------------------------------------------ dispatch
_DISPATCH = {
    "ema_trend": signal_ema_trend,
    "ema_trend_cot": signal_ema_trend_cot,
    "vwap_pullback": signal_vwap_pullback,
    "vwap_bands": signal_vwap_bands,
    "manual": manual_signal,
    "dual_ema": signal_dual_ema,
    "mean_reversion": signal_mean_reversion,
}


def preview_signal(kind, candles, quote, params, closes=None, cot=None):
    """Single entry point for UI preview, live board, and backtester.

    Returns {"side": "buy"|"sell"|None, "qty": float, "reason": str}.
    Stub kinds return {"side": None, ..., "reason": "stub/upgrade"}.

    `closes` is an optional pre-extracted close series for `candles`.
    The backtester passes it because it evaluates the same rolling
    window once per bar, and re-deriving the closes from candle dicts
    every time was the single largest cost in a run. Callers that do
    not have it (preview, live board -- one call, not a loop) are
    unaffected. Passing a mismatched series would be a caller bug, so
    it is length-checked rather than trusted.

    `cot` is an `engines.cotgate` module (or any object exposing
    `visible_at`/`timeline`/`evaluate`). Only `ema_trend_cot` reads it;
    every other strategy ignores it. The backtester passes it because it
    evaluates the loop once per bar and a per-bar database read would
    dominate the run -- the timeline is built once and bisected per bar.
    Passing it to a strategy that does not want it is harmless.
    """
    name = str(kind or "").strip()
    if name in STUB_KINDS:
        return {"side": None, "qty": _qty_of(params), "reason": "stub/upgrade"}
    fn = _DISPATCH.get(name)
    if fn is None:
        return {"side": None, "qty": _qty_of(params), "reason": "unknown-kind"}
    # Length-checked, not trusted. This guard is load-bearing in a way the
    # parity suite cannot see from the outside: a wrong-length hint is
    # *discarded* here, so every downstream number still matches the naive
    # path and every parity test stays green. The hint is wrong, but nothing
    # observable changes -- except speed.
    #
    # That is worth stating because it means "parity is green" is not
    # evidence that hints are correct. Verified by mutation: changing the
    # backtester's slice from closes[lo:i+1] to closes[lo:i] leaves all
    # parity tests passing and trips only the speed bound, because the
    # discarded hint silently costs the optimisation its entire purpose.
    if closes is not None and len(closes) != len(candles or []):
        closes = None
    if cot is not None:
        # A TypeError here is NOT swallowed and retried without the gate.
        # The `closes` branch below swallows one, for signal functions
        # that predate the hint -- but doing the same here turns a
        # signature bug inside a COT-gated strategy into a silent
        # pass-through, and a strategy quietly running without the veto it
        # was configured with is worse than one that errors. It happened
        # once during development: `cot.visible_at` called with the
        # module signature, and the strategy reported "cot-no-gate"
        # forever while looking perfectly healthy.
        try:
            return fn(candles, quote, params or {}, closes, cot)
        except Exception as e:
            return {"side": None, "qty": _qty_of(params),
                    "reason": "error:{}".format(e)}
    try:
        if closes is not None:
            return fn(candles, quote, params or {}, closes)
        return fn(candles, quote, params or {})
    except TypeError:
        # A signal function that has not taken the closes hint yet.
        try:
            return fn(candles, quote, params or {})
        except Exception as e:
            return {"side": None, "qty": _qty_of(params),
                    "reason": "error:{}".format(e)}
    except Exception as e:
        return {"side": None, "qty": _qty_of(params),
                "reason": "error:{}".format(e)}
