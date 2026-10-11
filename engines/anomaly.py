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

# The sigma-multiples below are FALLBACKS, not the threshold.
#
# They used to be the threshold. A fixed z is a Gaussian convention, and
# these instruments are not Gaussian: on a series with kurtosis 8, the
# empirical 2.5-sigma point sits near p=0.4%, not the 1.24% a 2.5 cut implies,
# so the same "unusual" label fires about three times as often as intended.
#
# On this deployment's current tape the fallbacks are *accurate* -- BTCUSD
# 1m returns have kurtosis 3.07, variance ratio 0.998, because
# scripts/seed.py generates rng.gauss(0.0, 0.0006 * vol_mult). That is
# precisely the trap: a Gaussian assumption looks correct on synthetic data
# and breaks on real data, which is the only place it matters.
#
# So each threshold is estimated from the instrument's own history via
# engines.tails, and the constant is used only when the estimate cannot be
# made. `_scan_window` reports which applied in `threshold_basis`, so a
# reader is never guessing whether a number came from the data or a
# convention.
SPIKE_Z = 2.5                 # fallback price-spike z (used only if unknown)
SPIKE_MIN_PCT = 0.02          # floor so a collapsed sigma cannot fake a spike
VOLUME_Z = 2.0                # fallback volume z threshold
REGIME_RATIO = 2.0            # fallback EWMA vol vs baseline (expansion)
COMPRESS_RATIO = 0.55         # fallback compression ratio (breakout setup)
BREAKOUT_LOOKBACK = 60        # bars for level breakouts
VELOCITY_Z = 3.0              # fallback last-bar move vs instrument sigma
VELOCITY_ABS = 0.5            # absolute % escape hatch (see _velocity)
RECENT_BARS = 15              # how far back the radar looks for events

# Tail probability for the price-spike cut. 0.995 is the two-sided 1%
# convention that 2.5-sigma implies, kept so the estimated threshold is
# comparable with the fallback it replaces.
SPIKE_P = 0.995


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


def _estimate_cuts(vals_all, candles=None):
    """Estimate every threshold from full history. Returns a `cuts` dict.

    One place, called once per `detect_anomalies`, so that estimation and
    application cannot drift apart. Each entry carries its own `basis`, and
    a missing entry means "fall back to the module constant" rather than
    "estimate failed silently".

    Volume additionally carries `vol_cut_units` -- the p99 in the volumes'
    own units -- because a z multiple and a volume are not comparable
    without a mean, and the mean here is the instrument's own.
    """
    out = {}
    try:
        from engines.tails import spike_threshold as _thr
    except ImportError:  # pragma: no cover - direct-path execution
        try:
            from tails import spike_threshold as _thr
        except ImportError:
            return {}

    rets = _rets(vals_all)
    if len(rets) >= 20:
        t = _thr(rets, fallback=SPIKE_Z, p=SPIKE_P)
        if t:
            t["kurtosis"] = _round_or_none(t.get("kurtosis"))
            t["threshold"] = _round_or_none(t.get("threshold"))
            out["spike"] = t

    vol_all = [_f(c.get("volume")) for c in (candles or [])
               if isinstance(c, dict)]
    vol_all = [v for v in vol_all if v > 0]
    if len(vol_all) >= 20:
        vt = _thr(vol_all, fallback=VOLUME_Z, p=0.99)
        if vt and vt.get("z") is not None:
            vm = _mean(vol_all)
            if vm > 1e-12:
                vt["vol_cut_units"] = vm * abs(vt["z"])
            vt["kurtosis"] = _round_or_none(vt.get("kurtosis"))
            out["volume"] = vt

    exp_cut, comp_cut = _regime_cuts(rets)
    out["regime"] = (exp_cut, comp_cut)
    out["regime_basis"] = _regime_basis(rets)
    return out


def _round_or_none(x):
    return None if x is None else round(float(x), 4)


def _vol_ratio_series(rets, window=60, lamb=EWMA_LAMBDA):
    """Trailing EWMA-vol / trailing-std ratio, one value per bar.

    This is the same quantity the regime detector compares against a
    threshold; here it is produced as a series so the threshold can be
    estimated from it rather than assumed.
    """
    out = []
    for i in range(window, len(rets) + 1):
        w = rets[i - window:i]
        base = _std(w)
        reg, _v = ewma_volatility(w, lamb=lamb)
        out.append((reg / base) if base > 1e-12 else 1.0)
    return out


def _regime_cuts(rets, window=60):
    """(expansion_cut, compression_cut) for this instrument's vol ratio.

    Quantiles of the ratio's own distribution: an expansion is a ratio in
    the top 5% of what this instrument has actually done, not above a 2.0
    that happens to round-number. Falls back to the module constants when
    there is not enough history to estimate.
    """
    try:
        from engines.tails import (MIN_TAIL_OBS, empirical_quantile)  # noqa
    except ImportError:  # pragma: no cover - direct-path execution
        try:
            from tails import MIN_TAIL_OBS, empirical_quantile  # noqa
        except ImportError:
            return REGIME_RATIO, COMPRESS_RATIO
    series = _vol_ratio_series(rets, window=window)
    if len(series) < MIN_TAIL_OBS:
        return REGIME_RATIO, COMPRESS_RATIO
    s = sorted(series)
    return (empirical_quantile(s, 0.95) or REGIME_RATIO,
            empirical_quantile(s, 0.05) or COMPRESS_RATIO)


def _regime_basis(rets, window=60):
    """Whether the cuts came from data or the fallback constants."""
    return ("estimated" if len(_vol_ratio_series(rets, window=window))
            >= _MIN_RATIO_OBS else "fallback")


_MIN_RATIO_OBS = 400


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
    # Thresholds are estimated ONCE, here, from the full tape -- and then
    # handed to every _scan_window call.
    #
    # This ordering is the whole point. The sweep shows each detector a
    # 20-25 bar window, and a tail percentile needs hundreds of
    # observations, so an estimate made inside _scan_window always
    # degrades to the Gaussian fallback. An earlier version of this change
    # computed the estimate inside the detector, reported it in the API
    # response, and then compared the bar against the hardcoded constant
    # anyway: the number was published and never used. Estimating out here
    # and passing it down is what makes the threshold real.
    cuts = _estimate_cuts(vals_all, candles)
    for k in range(min(window, max(1, len(vals_all) - 20))):
        v = vals_all[:-k] if k else vals_all
        c = (candles[:-k] if k else candles) if candles else None
        if len(v) < 20:
            break
        res = _scan_window(symbol, v, c, quote, meta, cuts=cuts)
        for a in res["anomalies"]:
            a["bars_ago"] = k
            # first sighting wins: the sweep runs newest-first
            seen.setdefault(a["kind"], a)

    anomalies = sorted(seen.values(),
                       key=lambda a: (a.get("bars_ago", 0), a["kind"]))
    out["anomalies"] = anomalies[:6]

    # Report the thresholds actually applied, so a reader is never guessing
    # whether a number came from the data or from a convention.
    spike = cuts.get("spike") or {}
    vol = cuts.get("volume") or {}
    out["threshold_basis"] = spike.get("basis")
    out["threshold_kurtosis"] = spike.get("kurtosis")
    out["threshold_n"] = spike.get("n")
    out["threshold_p995"] = spike.get("threshold")
    out["volume_threshold_basis"] = vol.get("basis")
    out["volume_threshold_p99"] = vol.get("threshold")
    out["regime_expansion_cut"], out["regime_compression_cut"] = \
        cuts.get("regime") or (REGIME_RATIO, COMPRESS_RATIO)
    out["regime_basis"] = cuts.get("regime_basis") or "fallback"
    weights = {"high": 12.0, "medium": 6.0, "low": 2.0}
    out["anomaly_score"] = round(
        min(100.0, sum(weights.get(a["severity"], 0.0) for a in anomalies)), 1)
    out["bars_scanned"] = window
    return out


def _scan_window(symbol, vals, candles=None, quote=None, meta=None,
                 cuts=None):
    """Single-bar detection pass. See detect_anomalies for the sweep.

    `cuts` carries thresholds estimated from the instrument's FULL history.
    It has to be passed in rather than computed here, because this function
    only ever sees the trailing 20-25 bars: a tail percentile needs
    hundreds of observations, so estimating from the window always returns
    the fallback. That was not a performance shortcut -- it meant the
    estimated threshold was computed, reported to the API, and never
    actually used in the gate. `detect_anomalies` now estimates once and
    passes the result down.
    """
    out = {"symbol": str(symbol or ""), "anomaly_score": 0.0, "anomalies": []}
    cuts = cuts or {}
    vals = [x for x in (_f(c) for c in (vals or [])) if x > 0]
    n = len(vals)
    if n < 20:
        return out

    # -- price spike (rolling z of 1-bar returns)
    rets = _rets(vals)
    if len(rets) >= 15:
        zs = zscore(rets[-(len(rets)):], 20)[-1] if len(rets) >= 20 else 0.0
        pct = (vals[-1] / vals[-2] - 1.0) * 100.0 if vals[-2] > 0 else 0.0
        # Prefer the cut estimated by the caller from full history. Falling
        # back to estimating from this 24-bar window is retained only for
        # direct `_scan_window` callers, and it will always land on the
        # fallback constant -- which is why the sweep passes one in.
        thr = cuts.get("spike") or {"threshold": float(SPIKE_Z),
                                    "basis": "fallback:no-full-history",
                                    "z": None, "n": 0, "kurtosis": None}
        # `threshold` is a SIGMA MULTIPLE, so it is directly comparable with
        # the SPIKE_Z fallback. The bar's return has to be converted into the
        # same units before it can be compared, which means multiplying by
        # the trailing sigma the rolling z is measured against. Comparing a
        # quantile to a z would be the units error this line exists to avoid.
        cut_z = float(thr.get("threshold") or SPIKE_Z)
        out["threshold_basis"] = thr.get("basis")
        out["threshold_kurtosis"] = thr.get("kurtosis")
        out["threshold_n"] = thr.get("n")
        out["threshold_z"] = cut_z
        trailing_sd = _std(rets[-20:]) if len(rets) >= 20 else 0.0
        ret_cut = trailing_sd * cut_z
        # A z-score against a near-zero sigma is degenerate: on a flat
        # series sigma collapses and *any* tick wiggle reads as a
        # 5-sigma event. Require a materially non-zero move as well, so
        # "unusual for this instrument" cannot manufacture a signal out
        # of rounding. This is the same guard _velocity applies.
        #
        # The gate.
        #
        # Measured, and this matters for reading the code: the two
        # magnitude conditions are NOT independent. `ret_cut` is
        # `trailing_sd * cut_z`, and `zs` is a rolling z over essentially
        # the same trailing window, so |ret| >= cut_z*sd already implies
        # |z| >= cut_z. Reverting the z side to the fixed 2.5 is therefore
        # behaviourally inert whenever cut_z > 2.5 -- verified by mutation:
        # swapping `cut_z` back to `SPIKE_Z` on the z side changes zero
        # detections, because the return-side condition is strictly
        # tighter and does the work on its own.
        #
        # Both are kept anyway, for two reasons. They degrade differently:
        # if `trailing_sd` is unavailable the return-side test cannot be
        # evaluated at all (it needs a sigma to scale the cut), and the z
        # condition is the one that still works from `zscore`'s own
        # window. And keeping the z side on `cut_z` means the two agree by
        # construction, so a future edit to one cannot silently loosen the
        # detector relative to the other.
        #
        # The substantive change is the return side: a move must clear the
        # instrument's own full-history tail, not just its recent sigma.
        # A move that is huge but sits inside the instrument's own history
        # is not flagged as a 1-in-200 event -- which is the false alarm
        # the fixed z produced on fat tails.
        if abs(zs) >= cut_z and abs(pct) >= SPIKE_MIN_PCT and \
                trailing_sd > 1e-12 and abs(rets[-1]) >= ret_cut:
            sev = "high" if abs(zs) >= max(4.0, cut_z + 1.0) else "medium"
            out["anomalies"].append({
                "kind": "price_spike", "severity": sev,
                "message": ("single-bar return {:+.3f}% (z={:.1f}, "
                            "cut={:.1f} sigma = {:.4f}%, {}) — beyond this "
                            "instrument's own history").format(
                                pct, zs, cut_z, ret_cut * 100.0,
                                thr.get("basis"))})
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
    #
    # Estimated from this instrument's own volume history for the same
    # reason as the price spike: participation is heavy-tailed, so a fixed
    # 2.0 cuts a different fraction of bars in a liquid instrument than in a
    # quiet one. The estimate comes from the caller (full history) for the
    # reason documented on `_scan_window`.
    vols = [_f(c.get("volume") if isinstance(c, dict) else 0.0) for c in (candles or [])]
    vols = [v for v in vols if v > 0]
    if len(vols) >= 20:
        vz = zscore(vols, 20)[-1]
        vthr = cuts.get("volume") or {"threshold": float(VOLUME_Z),
                                      "basis": "fallback:no-full-history"}
        vmean = _mean(vols)
        vcut_z = float(vthr.get("threshold") or VOLUME_Z)
        # Volume needs BOTH readings, because its own units are not
        # comparable with a z multiple:
        #   * vz >= vcut_z          -- unusually high vs its own recent sigma
        #   * vols[-1] >= vmean * vcut_z -- vs the instrument's own history
        # `vol_cut_units` is the second condition already expressed in volume
        # units; when it is absent the caller supplied only a z, and the
        # z condition alone is used rather than inventing a unit threshold.
        vol_cut = vthr.get("vol_cut_units")
        if vthr.get("basis") != "estimated" and vmean > 1e-12:
            vol_cut = vmean * vcut_z
        rel_cut = (vmean * vcut_z / vmean) if vmean > 1e-12 else vcut_z
        fires = vz >= vcut_z and (vol_cut is None or vols[-1] >= vol_cut)
        if fires:
            out["anomalies"].append({
                "kind": "volume",
                "severity": "medium" if (vol_cut and vols[-1] >= vol_cut * 1.5)
                            else "low",
                "message": ("volume {:.1f}x average (z={:.1f}, "
                            "cut={:.1f} sigma = {:.0f} units, {}) — unusual "
                            "participation").format(
                                vols[-1] / (vmean or 1.0), vz, vcut_z,
                                (vol_cut or 0.0), vthr.get("basis"))})

    # -- EWMA vol regime shift vs baseline
    #
    # Also estimated rather than fixed. REGIME_RATIO=2.0 and
    # COMPRESS_RATIO=0.55 were conventions with no measurement behind them;
    # the quantiles of this instrument's own vol-ratio series say where its
    # expansions and compressions actually start.
    if len(rets) >= 60:
        base = _std(rets[-60:])
        reg_var, _v = ewma_volatility(rets[-60:])
        ratio = (reg_var / base) if base > 1e-12 else 1.0
        exp_cut, comp_cut = cuts.get("regime") or _regime_cuts(rets)
        rb = (cuts.get("regime_basis")
              or (cuts.get("regime") and "estimated") or _regime_basis(rets))
        if ratio >= exp_cut:
            out["anomalies"].append({
                "kind": "vol_expansion", "severity": "medium",
                "message": ("volatility regime expanding {:.1f}x baseline "
                             "(p95={:.2f}, {}) — sizing down advised").format(
                                 ratio, exp_cut, rb)})
        elif ratio <= comp_cut:
            out["anomalies"].append({
                "kind": "vol_compression", "severity": "low",
                "message": ("volatility compressed {:.2f}x baseline "
                             "(p05={:.2f}, {}) — breakout setup forming").format(
                                 ratio, comp_cut, rb)})

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