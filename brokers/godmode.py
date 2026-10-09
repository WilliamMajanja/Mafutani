"""God Mode feature engine: a broad technical-indicator set, stdlib only.

The upstream "God Mode" idea (from a public XAUUSD RL bot) is a *feature
set*: 63 indicators fed to a learner, so the learner has many views of the
same bar and can pick whichever is informative. This module ports the
indicator half of that idea to the Vibe layer.

Why it is safe here when it would be dangerous elsewhere
------------------------------------------------------
A broad feature set is normally an overfitting machine: with 63 columns
and a small journal you can always find a combination that "predicts"
anything. The Vibe extractor already has the two controls that make it
usable regardless:

* a label must survive `min_support` roundtrips, and
* coverage is reported as a *union* over the journal, so a rule that
  matches many trades by luck is still one rule among many.

So the risk is bounded by the extractor, not by the number of features.
Adding features can only widen what a rule is allowed to key on.

Scope, stated plainly
---------------------
The upstream list is 63 indicators. This module implements a curated
subset -- the ones that are well-defined, cheap, and distinct from each
other. Indicators omitted are the ones that either duplicate an existing
feature (a second RSI period is not a second opinion) or need data this
application does not reliably have (tick-level order flow). Each
omission is recorded in `NOT_IMPLEMENTED` with the reason, so "God Mode"
here is an honest description rather than a claim of parity.

Contract with the rest of the layer
-----------------------------------
Every feature is a list aligned to `closes`. Entries before enough
history exists are None, never 0 and never NaN: None is what makes a rule
inapplicable, while 0 is a real number that a rule could match. The
evaluator treats None as "no signal", and a gate that always cried wolf
is a gate nobody reads.

No third-party code. `math` only.
"""
import math

from .base import BrokerError

__all__ = ["compute_god_features", "GOD_FEATURE_NAMES", "GOD_BUCKET_EDGES",
           "NOT_IMPLEMENTED", "god_bucket_label", "god_condition"]


class GodFeatureError(BrokerError):
    """Bad input to the God feature engine (short series, bad length)."""


#: Indicators on the upstream list that are deliberately not ported, and
#: why. An omission with a reason is a decision; an omission without one
#: is an accident that a later reader cannot tell apart from a gap.
NOT_IMPLEMENTED = {
    "renko": "needs brick-based resampling, not bar-aligned; a rule "
             "keyed on it would not evaluate on the live book",
    "heikin_ashi": "is a transform of the source candles, not an "
                   "independent view; it would correlate perfectly with "
                   "the features already present",
    "tick_order_flow": "needs L3 data this application does not have",
    "session_volume_profile": "needs per-session bars we do not segment",
}


# ------------------------------------------------------------------ helpers
def _sma(series, period):
    """Simple moving average, aligned, None until period closes."""
    out = [None] * len(series)
    total = 0.0
    for i, v in enumerate(series):
        total += v
        if i >= period:
            total -= series[i - period]
        if i >= period - 1:
            out[i] = total / period
    return out


def _ema(series, period):
    """Exponential moving average with a warm-up at the SMA.

    A pure `alpha*prev` seeding would make the first `period` values
    meaningless; anchoring to the SMA makes the series comparable across
    symbols with different history lengths.
    """
    out = [None] * len(series)
    if len(series) < period:
        return out
    total = 0.0
    for i in range(period):
        total += series[i]
    seed = total / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    for i in range(period, len(series)):
        out[i] = alpha * series[i] + (1.0 - alpha) * out[i - 1]
    return out


def _true_range(highs, lows, closes):
    out = [0.0]
    for i in range(1, len(closes)):
        h, l, prev = highs[i], lows[i], closes[i - 1]
        out.append(max(h - l, abs(h - prev), abs(l - prev)))
    return out


def _rolling_stdev(series, period):
    out = [None] * len(series)
    for i in range(period - 1, len(series)):
        window = series[i - period + 1:i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        out[i] = math.sqrt(var)
    return out


def _rolling_max(series, period):
    out = [None] * len(series)
    for i in range(period - 1, len(series)):
        out[i] = max(series[i - period + 1:i + 1])
    return out


def _rolling_min(series, period):
    out = [None] * len(series)
    for i in range(period - 1, len(series)):
        out[i] = min(series[i - period + 1:i + 1])
    return out


def _avg_gain_loss(closes, period):
    """Wilder's average gain/loss over `period`, aligned to closes."""
    gains = [None] * len(closes)
    losses = [None] * len(closes)
    if len(closes) < period + 1:
        return gains, losses
    g = l = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        g += max(d, 0.0)
        l += max(-d, 0.0)
    gains[period], losses[period] = g / period, l / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains[i] = (gains[i - 1] * (period - 1) + max(d, 0.0)) / period
        losses[i] = (losses[i - 1] * (period - 1) + max(-d, 0.0)) / period
    return gains, losses


# ------------------------------------------------------------------ features
def compute_god_features(closes, highs=None, lows=None, volumes=None,
                         timestamps=None):
    """Feature series for a candle history, aligned to ``closes``.

    Every returned list has len(closes) entries. Entries before enough
    history exists are None -- never 0, never NaN -- because None is what
    makes a rule inapplicable while 0 is a number a rule could match.

    ``timestamps`` is required for the calendar features. Omitting it
    leaves hour_utc and weekday None for every bar rather than guessing;
    the alternative makes every trade look like it happened at midnight.
    """
    n = len(closes)
    empty = {name: [] for name in GOD_FEATURE_NAMES}
    if n == 0:
        return empty

    highs = highs if highs is not None else closes
    lows = lows if lows is not None else closes
    volumes = volumes if volumes is not None else [0.0] * n
    if not (len(highs) == len(lows) == len(volumes) == n):
        raise GodFeatureError(
            "length mismatch: closes={} highs={} lows={} volumes={}".format(
                n, len(highs), len(lows), len(volumes)))

    out = {name: [None] * n for name in GOD_FEATURE_NAMES}

    # -- trend -----------------------------------------------------------
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    for i in range(n):
        # MACD is normalised by price. Raw EMA differences are on the
        # price scale, so a BTC bar and a USDT bar would carry the same
        # "macd" label for wildly different moves; the bucket edges are
        # percentage-scale and the normalisation is what makes them mean
        # the same thing on every symbol.
        if ema12[i] is not None and ema26[i] is not None and closes[i]:
            out["macd"][i] = (ema12[i] - ema26[i]) / closes[i]
        if sma20[i]:
            out["price_vs_sma20"][i] = (closes[i] - sma20[i]) / sma20[i]
        if sma50[i]:
            out["price_vs_sma50"][i] = (closes[i] - sma50[i]) / sma50[i]
        if sma20[i] is not None and sma50[i] is not None and sma50[i]:
            out["sma20_vs_sma50"][i] = (sma20[i] - sma50[i]) / sma50[i]

    # MACD signal line and histogram, both on the same normalised scale.
    macd_clean = [v if v is not None else 0.0 for v in out["macd"]]
    macd_signal = _ema(macd_clean, 9)
    for i in range(n):
        if out["macd"][i] is not None and macd_signal[i] is not None:
            out["macd_signal"][i] = macd_signal[i]
            out["macd_hist"][i] = out["macd"][i] - macd_signal[i]

    # ADX: directional movement smoothed. Needs 2*period bars minimum.
    period = 14
    tr = _true_range(highs, lows, closes)
    pdm = [0.0] * n
    mdm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
    atr14 = _wilder_smooth(tr, period)
    sp = _wilder_smooth(pdm, period)
    sm = _wilder_smooth(mdm, period)
    dx = [None] * n
    for i in range(n):
        if atr14[i] and atr14[i] > 0:
            pdi = 100.0 * sp[i] / atr14[i]
            mdi = 100.0 * sm[i] / atr14[i]
            out["pdi"][i] = pdi
            out["mdi"][i] = mdi
            denom = pdi + mdi
            if denom > 0:
                dx[i] = 100.0 * abs(pdi - mdi) / denom

    # ADX is the Wilder-smoothed DX, not DX itself. Reporting raw DX
    # under the name "adx" makes the first bar read 100 whenever one
    # direction has no movement at all -- a certainty the smoothing
    # exists to prevent.
    adx = _wilder_smooth([v if v is not None else 0.0 for v in dx], period)
    for i in range(2 * period - 1, n):
        if dx[i] is not None:
            out["adx"][i] = adx[i]

    # -- momentum --------------------------------------------------------
    g14, l14 = _avg_gain_loss(closes, 14)
    g7, l7 = _avg_gain_loss(closes, 7)
    for i in range(n):
        if g14[i] is not None:
            if l14[i] == 0:
                out["rsi14"][i] = 100.0 if g14[i] > 0 else 50.0
            else:
                rs = g14[i] / l14[i]
                out["rsi14"][i] = 100.0 - 100.0 / (1.0 + rs)
        if g7[i] is not None:
            if l7[i] == 0:
                out["rsi7"][i] = 100.0 if g7[i] > 0 else 50.0
            else:
                rs = g7[i] / l7[i]
                out["rsi7"][i] = 100.0 - 100.0 / (1.0 + rs)

    # Stochastic %K/%D over 14 bars.
    hh = _rolling_max(highs, 14)
    ll = _rolling_min(lows, 14)
    for i in range(n):
        if hh[i] is not None and hh[i] != ll[i]:
            out["stoch_k"][i] = 100.0 * (closes[i] - ll[i]) / (hh[i] - ll[i])
    # %D is the 3-bar SMA of %K, computed only over real %K values.
    # Substituting 0 for the missing early bars would put a fabricated
    # 0 on the series -- a number a rule could match as though it had
    # been measured.
    k_series = out["stoch_k"]
    k3 = [None] * n
    for i in range(2, n):
        window = [k_series[j] for j in range(i - 2, i + 1)]
        if all(v is not None for v in window):
            k3[i] = sum(window) / 3.0
    out["stoch_d"] = k3

    # CCI: typical price vs its SMA, scaled by mean deviation.
    tp = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]
    tp_sma = _sma(tp, 20)
    for i in range(n):
        if tp_sma[i] is not None:
            window = tp[i - 19:i + 1] if i >= 19 else tp[:i + 1]
            md = sum(abs(x - tp_sma[i]) for x in window) / len(window)
            if md > 1e-12:
                out["cci20"][i] = (tp[i] - tp_sma[i]) / (0.015 * md)

    # Williams %R: inverted stochastic over 14 bars.
    for i in range(n):
        if hh[i] is not None and hh[i] != ll[i]:
            out["williams_r"][i] = -100.0 * (hh[i] - closes[i]) / (hh[i] - ll[i])

    # ROC: percentage change over 10 bars.
    for i in range(10, n):
        base = closes[i - 10]
        if base:
            out["roc10"][i] = (closes[i] - base) / base

    # -- volatility ------------------------------------------------------
    # ATR as a Wilder-smoothed true range.
    for i in range(n):
        if atr14[i] is not None:
            out["atr14"][i] = atr14[i]
            if closes[i]:
                out["atr_pct"][i] = atr14[i] / closes[i]

    # Bollinger Bands (20, 2 sigma) as width and position.
    sd20 = _rolling_stdev(closes, 20)
    for i in range(n):
        if sma20[i] is not None and sd20[i] is not None:
            mid, sd = sma20[i], sd20[i]
            out["bb_width"][i] = (2.0 * sd / mid) if mid else None
            if closes[i]:
                out["bb_pct"][i] = (closes[i] - (mid - 2 * sd)) / (4 * sd) if sd > 1e-12 else 0.5

    # Keltner Channels: EMA20 +/- 2*ATR.
    ema20 = _ema(closes, 20)
    for i in range(n):
        if ema20[i] is not None and out["atr14"][i] is not None:
            out["kc_width"][i] = (4.0 * out["atr14"][i] / ema20[i]) if ema20[i] else None

    # Donchian: distance from the 20-bar high/low.
    h20 = _rolling_max(highs, 20)
    l20 = _rolling_min(lows, 20)
    for i in range(n):
        if h20[i] is not None and closes[i]:
            out["dist_from_high20"][i] = (closes[i] - h20[i]) / closes[i]
        if l20[i] is not None and closes[i]:
            out["dist_from_low20"][i] = (closes[i] - l20[i]) / closes[i]

    # -- volume ---------------------------------------------------------
    obv = [0.0] * n
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            obv[i] = obv[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            obv[i] = obv[i - 1] - volumes[i]
        else:
            obv[i] = obv[i - 1]
    obv_sma = _sma(obv, 20)
    for i in range(n):
        if obv_sma[i] is not None and obv_sma[i] != 0:
            out["obv_slope"][i] = (obv[i] - obv_sma[i]) / abs(obv_sma[i])

    # Chaikin Money Flow over 20 bars.
    for i in range(19, n):
        num = den = 0.0
        for j in range(i - 19, i + 1):
            h, l, c = highs[j], lows[j], closes[j]
            rng = h - l
            mfm = ((c - l) - (h - c)) / rng if rng > 1e-12 else 0.0
            num += mfm * volumes[j]
            den += volumes[j]
        if den > 0:
            out["cmf20"][i] = num / den

    # VWAP (rolling 20-bar) distance.
    for i in range(19, n):
        pv = sum((highs[j] + lows[j] + closes[j]) / 3.0 * volumes[j]
                 for j in range(i - 19, i + 1))
        vv = sum(volumes[j] for j in range(i - 19, i + 1))
        if vv > 0 and closes[i]:
            vwap = pv / vv
            out["vwap_dist"][i] = (closes[i] - vwap) / vwap

    # -- price structure -------------------------------------------------
    for i in range(n):
        rng = highs[i] - lows[i]
        if rng > 1e-12:
            # Where the close sits inside the bar: 0 = at the low,
            # 1 = at the high. A close at the extreme is a statement
            # about who held the bar; a close in the middle is not.
            out["close_position"][i] = (closes[i] - lows[i]) / rng
            out["upper_shadow"][i] = (highs[i] - max(closes[i], closes[i])) / rng
            out["lower_shadow"][i] = (min(closes[i], closes[i]) - lows[i]) / rng

    # Close-to-close change. Named honestly: this application trades
    # 24/7 crypto, where there is no overnight session to gap across, so
    # "gap" would be a claim the data cannot support. What is measured
    # here is the one-bar return.
    for i in range(1, n):
        prev = closes[i - 1]
        if prev and closes[i]:
            out["close_change"][i] = (closes[i] - prev) / prev

    # Streak of consecutive up-closes ending at this bar. Down-streaks
    # report as negatives so one rule covers both and a "flat" day is
    # visibly neither.
    streak = 0
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            streak = streak + 1 if streak > 0 else 1
        elif closes[i] < closes[i - 1]:
            streak = streak - 1 if streak < 0 else -1
        else:
            streak = 0
        out["up_streak"][i] = float(streak)
    out["up_streak"][0] = 0.0

    # -- calendar --------------------------------------------------------
    import time as _time
    have_ts = bool(timestamps) and len(timestamps) == n
    if have_ts:
        for i in range(n):
            try:
                t = _time.gmtime(int(timestamps[i]))
                out["hour_utc"][i] = t.tm_hour
                out["weekday"][i] = t.tm_wday
            except (TypeError, ValueError, OSError, OverflowError):
                pass

    return out


def _wilder_smooth(series, period):
    """Wilder's smoothing: EMA with alpha = 1/period."""
    out = [None] * len(series)
    if len(series) < period:
        return out
    total = 0.0
    for i in range(period):
        total += series[i]
    out[period - 1] = total / period
    for i in range(period, len(series)):
        out[i] = out[i - 1] + (series[i] - out[i - 1]) / period
    return out


# ------------------------------------------------------------- feature names
#: The feature set the God mode extractor may key rules on. Order is
#: stable so that a profile serialised to disk stays comparable.
GOD_FEATURE_NAMES = (
    # trend
    "macd", "macd_signal", "macd_hist",
    "price_vs_sma20", "price_vs_sma50", "sma20_vs_sma50",
    "pdi", "mdi", "adx",
    # momentum
    "rsi7", "rsi14", "stoch_k", "stoch_d", "cci20", "williams_r",
    "roc10",
    # volatility
    "atr14", "atr_pct", "bb_width", "bb_pct", "kc_width",
    "dist_from_high20", "dist_from_low20",
    # volume
    "obv_slope", "cmf20", "vwap_dist",
    # price structure
    "close_position", "upper_shadow", "lower_shadow",
    "close_change", "up_streak",
    # calendar
    "hour_utc", "weekday",
)

#: Bucket edges per feature. Chosen to split a liquid 1m series into
#: five populated buckets; the label itself is only used as a key, so the
#: edges matter because a rule fires on the range they describe.
GOD_BUCKET_EDGES = {
    "macd": (-0.004, -0.001, 0.001, 0.004),
    "macd_signal": (-0.004, -0.001, 0.001, 0.004),
    "macd_hist": (-0.002, -0.0005, 0.0005, 0.002),
    "price_vs_sma20": (-0.01, -0.003, 0.003, 0.01),
    "price_vs_sma50": (-0.02, -0.006, 0.006, 0.02),
    "sma20_vs_sma50": (-0.02, -0.005, 0.005, 0.02),
    "pdi": (10.0, 18.0, 25.0, 35.0),
    "mdi": (10.0, 18.0, 25.0, 35.0),
    "adx": (10.0, 20.0, 30.0, 45.0),
    "rsi7": (20.0, 30.0, 50.0, 70.0),
    "rsi14": (20.0, 30.0, 50.0, 70.0),
    "stoch_k": (20.0, 35.0, 65.0, 80.0),
    "stoch_d": (20.0, 35.0, 65.0, 80.0),
    "cci20": (-200.0, -100.0, 100.0, 200.0),
    "williams_r": (-80.0, -65.0, -35.0, -20.0),
    "roc10": (-0.02, -0.005, 0.005, 0.02),
    "atr14": (0.0, 0.0005, 0.002, 0.006),
    "atr_pct": (0.0, 0.002, 0.006, 0.02),
    "bb_width": (0.0, 0.004, 0.012, 0.03),
    "bb_pct": (0.0, 0.25, 0.75, 1.0),
    "kc_width": (0.0, 0.004, 0.012, 0.03),
    "dist_from_high20": (-0.05, -0.02, -0.005, 0.0),
    "dist_from_low20": (0.0, 0.005, 0.02, 0.05),
    "obv_slope": (-0.05, -0.01, 0.01, 0.05),
    "cmf20": (-0.4, -0.15, 0.15, 0.4),
    "vwap_dist": (-0.01, -0.002, 0.002, 0.01),
    "close_position": (0.0, 0.25, 0.75, 1.0),
    "upper_shadow": (0.0, 0.15, 0.5, 0.85),
    "lower_shadow": (0.0, 0.15, 0.5, 0.85),
    "close_change": (-0.01, -0.002, 0.002, 0.01),
    "up_streak": (1.0, 2.0, 4.0, 8.0),
}

_GOD_CATEGORICAL = {
    "hour_utc": lambda v: "h{:02d}".format(int(v) // 3),
    "weekday": lambda v: "d{}".format(int(v)),
}


def god_bucket_label(feature, value):
    """Coarse label for a God feature value, or None when unusable.

    None for the same reasons as the base layer: a missing or unparseable
    value must not be bucketed, because a bucket it landed in would be a
    bucket nothing was counted under.
    """
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    edges = GOD_BUCKET_EDGES.get(feature)
    if edges:
        if v < edges[0]:
            return "very_low"
        if v < edges[1]:
            return "low"
        if v < edges[2]:
            return "mid"
        if v < edges[3]:
            return "high"
        return "very_high"
    fn = _GOD_CATEGORICAL.get(feature)
    if fn:
        try:
            return fn(v)
        except (TypeError, ValueError):
            return None
    return None


def god_condition(feature, label):
    """The condition a God bucket label stands for, as a node spec.

    Interior buckets become a half-open `between` node so the condition
    reproduces the range rather than summarising it: a one-sided
    comparison would admit bars above the bucket's upper edge and the
    rule would fire on bars its own support never contained.
    """
    from .vibe import between as _between, cmp as _cmp
    edges = GOD_BUCKET_EDGES.get(feature)
    if not edges:
        return None
    e0, e1, e2, e3 = edges
    if label == "very_low":
        return _cmp(feature, "<", e0)
    if label == "low":
        return _between(feature, e0, e1)
    if label == "mid":
        return _between(feature, e1, e2)
    if label == "high":
        return _between(feature, e2, e3)
    if label == "very_high":
        return _cmp(feature, ">=", e3)
    return None


def _god_hour_condition(feature, label):
    """hour_utc labels are 3-hour blocks: h00 = 00:00-02:59 UTC."""
    from .vibe import member
    block = int(label[1:])
    return member("hour_utc", "in",
                  [h for h in range(24) if h // 3 == block])


def _god_weekday_condition(feature, label):
    from .vibe import cmp
    return cmp("weekday", "==", float(label[1:]))


def _god_condition_for(feature, label):
    """Dispatch to the right condition builder for this feature."""
    if feature == "hour_utc":
        return _god_hour_condition(feature, label)
    if feature == "weekday":
        return _god_weekday_condition(feature, label)
    return god_condition(feature, label)


#: The God feature space, for ``extract_profile`` and ``scan``.
from .vibe import FeatureSpace as _FeatureSpace  # noqa: E402

GOD_SPACE = _FeatureSpace(
    "god", GOD_FEATURE_NAMES, god_bucket_label, _god_condition_for)
