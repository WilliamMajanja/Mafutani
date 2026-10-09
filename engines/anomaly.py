"""Zero-dependency anomaly detection + market-regime intelligence.

Pure stdlib (math only). These are statistical, not learned, models —
appropriate for a zero-pip codebase and fully deterministic:

  - rolling z-score price spikes        (price moves beyond expected σ)
  - single-bar range vs ATR             (volatility shocks)
  - volume z-score                      (unusual activity)
  - EWMA volatility regime shifts       (lambda=0.94, RiskMetrics-style)
  - spread stress vs asset cap          (liquidity anomalies)
  - level breakouts                     (support/resistance violations)
  - rescaled-range Hurst exponent       (trending vs mean-reverting regime)
  - multi-timeframe trend consensus     (1m/5m/15m alignment)

Every function is pure. `detect_anomalies` never raises.
"""

import math

from engines.indicators import atr, zscore, ema_series

EWMA_LAMBDA = 0.94            # RiskMetrics volatility smoothing
SPIKE_Z = 2.5                 # price-spike z threshold
SPIKE_MIN_PCT = 0.02          # floor so a collapsed sigma cannot fake a spike
VOLUME_Z = 2.0                # volume z threshold
REGIME_RATIO = 2.0            # EWMA vol vs baseline ratio (expansion)
COMPRESS_RATIO = 0.55         # below this = compression (breakout setup)
BREAKOUT_LOOKBACK = 60        # bars for level breakouts
VELOCITY_Z = 3.0              # last-bar move vs the instrument's own sigma
VELOCITY_ABS = 0.5            # absolute % escape hatch (see _velocity)
RECENT_BARS = 15              # how far back the radar looks for events


def _f(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return v


def _rets(vals):
    out = []
    for i in range(1, len(vals)):
        p = vals[i - 1]
        if p > 0:
            out.append(vals[i] / p - 1.0)
    return out


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _velocity(rets, pct):
    """How extreme is the last bar's move for *this* instrument?

    Returns {"z": sigma_multiple, "sigma": per-bar sigma in %}.
    A non-positive or unavailable sigma yields z=0.0, which leaves the
    absolute VELOCITY_ABS gate in sole charge rather than inventing a
    signal from a degenerate distribution.
    """
    out = {"z": 0.0, "sigma": 0.0}
    if len(rets) < 20:
        return out
    sigma = _std(rets[-20:]) * 100.0      # returns are fractions -> percent
    out["sigma"] = sigma
    if sigma > 1e-9:
        out["z"] = abs(pct) / sigma
    return out


def ewma_volatility(rets, lamb=EWMA_LAMBDA):
    """Exponentially weighted (co)vol from returns, RiskMetrics style."""
    if not rets:
        return 0.0, None
    var = rets[0] ** 2
    for r in rets[1:]:
        var = lamb * var + (1 - lamb) * r ** 2
    return math.sqrt(var), var


def hurst_exponent(vals):
    """Rescaled-range Hurst estimate (pure python, log-log slope over
    halving window sizes). H>0.55 trending, <0.45 mean-reverting."""
    vals = [_f(v) for v in (vals or []) if v is not None]
    n = len(vals)
    if n < 16:
        return None
    max_k = 1
    while (1 << (max_k + 1)) <= n // 2:
        max_k += 1
    if max_k < 2:
        return None
    points = []
    for k in range(1, max_k + 1):
        win = 1 << k
        rs = []
        for start in range(0, n - win + 1, win):
            seg = vals[start:start + win]
            m = sum(seg) / win
            devs = [x - m for x in seg]
            cum = []
            acc = 0.0
            for d in devs:
                acc += d
                cum.append(acc)
            r = max(cum) - min(cum)
            sd = math.sqrt(sum(d * d for d in devs) / win)
            if sd > 1e-12:
                rs.append(r / sd)
        if rs:
            points.append((math.log(win), math.log(_mean(rs))))
    if len(points) < 2:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx, my = _mean(xs), _mean(ys)
    num = sum((x - mx) * (y - my) for x, y in points)
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 0:
        return None
    slope = num / den
    return max(0.0, min(1.0, slope))


def classify_regime(closes):
    """Regime: 'trending' | 'mean-reverting' | 'random'. Returns dict."""
    h = hurst_exponent(closes)
    if h is None:
        return {"hurst": None, "regime": "unknown",
                "description": "not enough data to classify regime"}
    if h >= 0.55:
        reg = "trending"
        desc = "H={:.2f} — trend persistence: follow direction, trail stops".format(h)
    elif h <= 0.45:
        reg = "mean-reverting"
        desc = "H={:.2f} — oscillatory: fade extremes, tight stops".format(h)
    else:
        reg = "random"
        desc = "H={:.2f} — near random walk: reduce size, wait for edge".format(h)
    return {"hurst": round(h, 2), "regime": reg, "description": desc}


def resample_closes(candles, minutes):
    """Aggregate 1m bars to `minutes` bars -> asc list of closes.
    Accepts candle dicts ({ts, close}) or plain floats (treated as 1m
    closes on a synthetic timeline)."""
    if not candles:
        return []
    buckets = {}
    for i, c in enumerate(candles):
        if isinstance(c, dict):
            ts = _f(c.get("ts") if isinstance(c, dict) else 0.0)
            cl = _f(c.get("close") if isinstance(c, dict) else 0.0)
        else:
            ts = float(i * 60)
            cl = _f(c)
        if cl <= 0:
            continue
        key = int(ts // (minutes * 60))
        buckets.setdefault(key, []).append(cl)
    out = []
    for k in sorted(buckets):
        out.append(buckets[k][-1])
    return out


def trend_of(closes, fast=9, slow=21):
    """'up'|'down'|'flat' from EMA fast/slow gap on ascending closes."""
    vals = [_f(v) for v in (closes or []) if v is not None]
    if len(vals) < slow:
        return "flat"
    e_f = ema_series(vals, fast)[-1]
    e_s = ema_series(vals, slow)[-1]
    gap = (e_f - e_s) / (abs(e_s) + 1e-9) * 100.0
    if gap > 0.05:
        return "up"
    if gap < -0.05:
        return "down"
    return "flat"


def tf_consensus(closes_1m):
    """Multi-timeframe alignment across 1m/5m/15m EMA trends."""
    tfs = [("1m", trend_of(closes_1m)),
           ("5m", trend_of(resample_closes(closes_1m, 5))),
           ("15m", trend_of(resample_closes(closes_1m, 15)))]
    states = [t[1] for t in tfs]
    if all(s == "up" for s in states):
        align = "aligned-up"
        verdict = "all timeframes up — strong continuation context"
    elif all(s == "down" for s in states):
        align = "aligned-down"
        verdict = "all timeframes down — strong bearish context"
    elif "up" in states and "down" in states:
        align = "conflicted"
        verdict = "timeframes disagree — reduce conviction, tighter risk"
    else:
        align = "mixed"
        verdict = "some timeframes flat — wait for alignment before entries"
    return {"timeframes": tfs, "alignment": align, "verdict": verdict}


def _spread_cap(meta):
    from engines.analysis import SPREAD_CAPS  # import-safe, pure
    at = (meta or {}).get("asset_type", "equity")
    return SPREAD_CAPS.get(at, 30.0)


def detect_anomalies(symbol, closes, candles=None, quote=None, meta=None,
                     recent_bars=None):
    """Return {anomaly_score, anomalies:[{kind,severity,message,ts?,bars_ago}]}.
    severity: 'high'|'medium'|'low'. Never raises.

    A radar is only useful if it remembers. The detectors are written
    against the *final* bar, so on 1-minute data any event was invisible
    one minute later -- the radar went blank between scans instead of
    holding the last RECENT_BARS of tape. This sweeps the trailing
    window, keeps the most recent occurrence of each kind, and records
    bars_ago so the operator can tell "happening now" from "happened
    nine minutes ago".

    recent_bars=1 restores the old single-bar behaviour.
    """
    out = {"symbol": str(symbol or ""), "anomaly_score": 0.0, "anomalies": []}
    window = RECENT_BARS if recent_bars is None else max(1, int(recent_bars))

    def _clean(xs):
        return [_f(c) for c in (xs or []) if c is not None and _f(c) > 0]

    vals_all = _clean(closes)
    if len(vals_all) < 20:
        return out
    # The quote is the current one; it stands in for the whole sweep.
    # That is exact for the last bar and approximate for older ones,
    # which is acceptable for a spread-stress reading and noted here
    # rather than silently assumed.
    seen = {}
    for k in range(min(window, max(1, len(vals_all) - 20))):
        v = vals_all[:-k] if k else vals_all
        c = (candles[:-k] if k else candles) if candles else None
        if len(v) < 20:
            break
        res = _scan_window(symbol, v, c, quote, meta)
        for a in res["anomalies"]:
            a["bars_ago"] = k
            # first sighting wins: the sweep runs newest-first
            seen.setdefault(a["kind"], a)

    anomalies = sorted(seen.values(),
                       key=lambda a: (a.get("bars_ago", 0), a["kind"]))
    out["anomalies"] = anomalies[:6]
    weights = {"high": 12.0, "medium": 6.0, "low": 2.0}
    out["anomaly_score"] = round(
        min(100.0, sum(weights.get(a["severity"], 0.0) for a in anomalies)), 1)
    out["bars_scanned"] = window
    return out


def _scan_window(symbol, vals, candles=None, quote=None, meta=None):
    """Single-bar detection pass. See detect_anomalies for the sweep."""
    out = {"symbol": str(symbol or ""), "anomaly_score": 0.0, "anomalies": []}
    meta = meta or {}
    vals = [x for x in (_f(c) for c in (vals or [])) if x > 0]
    n = len(vals)
    if n < 20:
        return out

    # -- price spike (rolling z of 1-bar returns)
    rets = _rets(vals)
    if len(rets) >= 15:
        zs = zscore(rets[-(len(rets)):], 20)[-1] if len(rets) >= 20 else 0.0
        pct = (vals[-1] / vals[-2] - 1.0) * 100.0 if vals[-2] > 0 else 0.0
        # A z-score against a near-zero sigma is degenerate: on a flat
        # series sigma collapses and *any* tick wiggle reads as a
        # 5-sigma event. Require a materially non-zero move as well, so
        # "unusual for this instrument" cannot manufacture a signal out
        # of rounding. This is the same guard _velocity applies.
        if abs(zs) >= SPIKE_Z and abs(pct) >= SPIKE_MIN_PCT:
            sev = "high" if abs(zs) >= 4.0 else "medium"
            out["anomalies"].append({
                "kind": "price_spike", "severity": sev,
                "message": "single-bar return {:+.3f}% (z={:.1f}) — {} standard deviations outside expected move".format(
                    pct, zs, "4+" if abs(zs) >= 4.0 else "2.5+")})
        # Velocity is measured against the instrument's OWN recent
        # per-bar volatility, not a fixed percentage. The old gate was a
        # hardcoded abs(pct) > 0.5, which is meaningless across a
        # universe spanning $0.21 sugar to $84,000 BTC -- and it was
        # simply dead: the largest single-bar move in the stored tape
        # was 0.198% (BTCUSD), so this branch could never fire and one
        # of eight detectors was dead code. A 0.2% bar is a 5-sigma
        # event on BTC and unremarkable on a quiet commodity, which is
        # exactly the distinction a fixed threshold throws away.
        # VELOCITY_ABS stays as an escape hatch so a genuinely large
        # move still registers even if trailing sigma is unavailable.
        vel = _velocity(rets, pct)
        if vel["z"] >= VELOCITY_Z or abs(pct) >= VELOCITY_ABS:
            if vel["z"] >= 4.5:
                sev = "high"
            elif vel["z"] >= VELOCITY_Z or abs(pct) >= VELOCITY_ABS * 2:
                sev = "medium"
            else:
                sev = "low"
            out["anomalies"].append({
                "kind": "velocity", "severity": sev,
                "message": "last bar moved {}{}% ({:.1f}x the instrument's "
                           "recent per-bar sigma)".format(
                               "+" if pct >= 0 else "", round(pct, 3), vel["z"])})

    # -- single-bar range vs ATR (volatility shock)
    if candles:
        atr_vals = atr(candles, 14)
        last = candles[-1]
        rng = _f(last.get("high")) - _f(last.get("low"))
        a = atr_vals[-1] if atr_vals else 0.0
        if a > 0 and rng > 3.0 * a:
            out["anomalies"].append({
                "kind": "range_shock", "severity": "high",
                "message": "last bar range {:.2f}x ATR({:.2f}) — volatility shock".format(
                    round(rng / a, 2), round(a, 4))})

    # -- volume z (unusual activity)
    vols = [_f(c.get("volume") if isinstance(c, dict) else 0.0) for c in (candles or [])]
    vols = [v for v in vols if v > 0]
    if len(vols) >= 20:
        vz = zscore(vols, 20)[-1]
        if vz >= VOLUME_Z:
            ratio = vols[-1] / (_mean(vols) or 1.0)
            out["anomalies"].append({
                "kind": "volume", "severity": "medium" if vz >= 3.5 else "low",
                "message": "volume {:.1f}x average (z={:.1f}) — unusual participation".format(
                    ratio, vz)})

    # -- EWMA vol regime shift vs baseline
    if len(rets) >= 60:
        base = _std(rets[-60:])
        reg_var, _v = ewma_volatility(rets[-60:])
        ratio = (reg_var / base) if base > 1e-12 else 1.0
        if ratio >= REGIME_RATIO:
            out["anomalies"].append({
                "kind": "vol_expansion", "severity": "medium",
                "message": "volatility regime expanding {:.1f}x baseline — sizing down advised".format(ratio)})
        elif ratio <= COMPRESS_RATIO:
            out["anomalies"].append({
                "kind": "vol_compression", "severity": "low",
                "message": "volatility compressed {:.2f}x baseline — breakout setup forming".format(ratio)})

    # -- spread stress vs asset cap
    bid, ask = _f((quote or {}).get("bid")), _f((quote or {}).get("ask"))
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        if mid > 0:
            sp = (ask - bid) / mid * 10000.0
            cap = _spread_cap(meta)
            if sp > cap * 2.0:
                out["anomalies"].append({
                    "kind": "spread_stress", "severity": "medium",
                    "message": "spread {:.1f}bps is {:.1f}x the {} asset cap — thin liquidity".format(
                        sp, sp / cap, meta.get("asset_type", "?"))})

    # -- level breakout
    if n >= BREAKOUT_LOOKBACK:
        prev = vals[-BREAKOUT_LOOKBACK - 1:-1]
        hi, lo = max(prev), min(prev)
        lastc = vals[-1]
        if lastc > hi:
            out["anomalies"].append({
                "kind": "breakout", "severity": "medium",
                "message": "close {} above {}-bar high — upside level break".format(
                    round(lastc, 4), BREAKOUT_LOOKBACK)})
        elif lastc < lo:
            out["anomalies"].append({
                "kind": "breakdown", "severity": "medium",
                "message": "close {} below {}-bar low — downside level break".format(
                    round(lastc, 4), BREAKOUT_LOOKBACK)})

    # -- composite anomaly score (0-100, weighted by severity)
    weights = {"high": 12.0, "medium": 6.0, "low": 2.0}
    score = min(100.0, sum(weights.get(a["severity"], 0.0) for a in out["anomalies"]))
    out["anomaly_score"] = round(score, 1)
    out["anomalies"] = out["anomalies"][:6]
    return out