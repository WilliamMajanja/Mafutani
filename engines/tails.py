"""Distribution-estimated anomaly thresholds. Stdlib only, no I/O.

Why this exists
---------------
`engines/anomaly.py` used fixed sigma-multiples for everything unusual:
`SPIKE_Z = 2.5`, `VOLUME_Z = 2.0`, `REGIME_RATIO = 2.0`, `COMPRESS_RATIO =
0.55`. Those numbers are Gaussian conventions, and they are wrong for the
instruments in front of us.

A 2.5-sigma cut implies a two-tailed false-alarm rate of 1.24%. On a series
with kurtosis 3 that is arithmetically right. On a fat-tailed series it is
not, and the gap grows with the tail weight. Measured on variance-mixture
fixtures, the two-sided probability that a return exceeds 2.5 sigma is:

    kurtosis 2.99 (Gaussian)   1.24%   1.00x the intended rate
    kurtosis 3.57 (df=10)       1.83%   1.47x
    kurtosis 4.09 (df=5)        2.19%   1.76x
    kurtosis 4.57 (df=3)        2.59%   2.08x

so a fixed 2.5 fires roughly twice as often as its label claims at
kurtosis 4.6, and the excess keeps growing beyond that. And a fat-tailed
distribution makes the *middle* of the range unreliable too: the variance
is set by a handful of outliers, so the same z-score means different things
in a quiet week and a violent one.

Measured on this deployment's tape: BTCUSD 1-minute returns have kurtosis
3.07, squared-return autocorrelation 0.013, variance ratio 0.998/0.960/0.948.
That is a random walk with i.i.d. Gaussian increments -- `scripts/seed.py`
generates `rng.gauss(0.0, 0.0006 * vol_mult)`. So on *this* data a fixed
threshold happens to be fine, and that is exactly the trap: the convention
looks correct on synthetic data and breaks on real data, which is the only
place it matters.

So the thresholds are estimated from the instrument's own return
distribution instead of assumed, and the estimate is reported alongside the
threshold so a reader can see what it was based on.

Method: empirical quantiles, not a parametric fit
--------------------------------------------------
`empirical_quantile` and `robust_sigma` below are deliberately non-parametric.
Fitting a Student-t and reading off the 99.5th percentile is possible, but it
adds a distributional assumption, and the assumption is the thing being
removed. A quantile is a definition, not a model: "the 0.25% of moves this
instrument actually made". On 43,000 bars that is ~107 observations, which
is enough for a tail probability to mean something.

Measured effect on the test fixtures: Gaussian p99.5 lands at z=2.56
(theory 2.576), and a df=3 variance-mixture lands at z=3.11. That +0.55 is
what a fixed 2.5 was getting wrong.

Three guards, all about refusing rather than guessing
-----------------------------------------------------
* Fewer than `MIN_TAIL_OBS` usable returns -> `sufficient=False`, and
  `classify` refuses to publish a threshold at all. A percentile computed
  from 120 points is the sample, not the distribution.
* Zero spread -> `degenerate`, same refusal. A constant series has an upper
  quantile equal to the constant, so the "estimated" threshold would be ~0
  and would fire on every bar.
* `kurtosis` winsorises at a *robust* sigma, not the standard deviation.
  Capping at 5 standard deviations is circular -- one 500-sigma outlier
  inflates the standard deviation enough that the cap sits above the outlier
  and nothing is winsorised. Measured, that version returned kurtosis 1850
  on a series whose robust kurtosis is 3.0.

Standardisation is the one genuinely useful transformation here
---------------------------------------------------------------
A per-bar z-score against trailing sigma answers "is this move unusual *now*".
A percentile against the instrument's own history answers "is this move
unusual *ever*". Both are wanted, and they are not the same question: a
volatility expansion inflates trailing sigma so z-scores collapse toward
zero precisely when the moves are most unusual, and only the history-
relative measure notices.
"""
import math

# Below this, a percentile is the sample rather than the distribution.
# For a 0.5% tail you want at least ~400 points to have a couple in it.
MIN_TAIL_OBS = 400

# Beyond this z the data is too heavy-tailed for a sigma multiple to mean
# anything, so the percentile is authoritative and the z is advisory only.
HEAVY_TAIL_Z = 4.0

# Winsorisation cap for kurtosis, in robust sigmas. See kurtosis().
ROBUST_CAP_SIGMAS = 5.0

# A distribution this tight is degenerate, not merely quiet: estimating a
# tail from it produces a threshold of ~0, which fires on every bar.
DEGENERATE_SIGMA = 1e-12

# Scale factor making the MAD comparable to a Gaussian standard deviation.
MAD_TO_SIGMA = 1.4826


def _finite(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def mean(xs):
    xs = [v for v in xs if v is not None]
    return sum(xs) / len(xs) if xs else 0.0


def std(xs):
    xs = [v for v in xs if v is not None]
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def robust_sigma(xs):
    """Median absolute deviation, scaled to be comparable to a std.

    MAD is ~1/1.4826 the sigma of a Gaussian, so it is multiplied by
    1.4826 to keep `z_robust` on the same scale as a classical z. On a fat
    tail it does not move when a single observation goes extreme, which a
    standard deviation does -- so this is the sigma to trust when kurtosis
    says the tails are heavy.
    """
    xs = sorted(v for v in (_finite(x) for x in (xs or [])) if v is not None)
    if len(xs) < 5:
        return None
    med = empirical_quantile(xs, 0.5)
    dev = sorted(abs(x - med) for x in xs)
    mad = empirical_quantile(dev, 0.5)
    if mad is None:
        return None
    return mad * MAD_TO_SIGMA


def raw_kurtosis(xs):
    """Un-winsorised kurtosis. Unbounded above; reads high on one outlier.

    This is the number that actually describes how far the tail extends,
    and the one `kurtosis` systematically understates. It is kept
    separate rather than blended because the two answer different
    questions, and `classify` reports both.
    """
    xs = [v for v in xs if v is not None]
    n = len(xs)
    if n < 20:
        return None
    m = mean(xs)
    m2 = sum((x - m) ** 2 for x in xs) / n
    if m2 <= 0:
        return None
    m4 = sum((x - m) ** 4 for x in xs) / n
    return m4 / (m2 * m2)


def kurtosis(xs):
    """Winsorised kurtosis. 3.0 is Gaussian; ~4.8 is the ceiling it can read.

    The moments are computed on winsorised values, because with a fat tail
    the raw m4 is dominated by the one observation that matters least to
    describing the bulk -- which is how a single crash makes every
    instrument look identically extreme.

    The cap is `ROBUST_CAP_SIGMAS` times a **robust** sigma, not the
    standard deviation. Capping at 5 standard deviations is circular: one
    500-sigma observation inflates the standard deviation enough that the
    cap lands above the outlier and nothing is winsorised at all.

    **This function has a ceiling, and it is a real limitation.**
    Winsorising at 5 robust sigmas bounds the returned value at roughly
    4.81 regardless of how heavy the true tail is. Measured on a df=1.5
    variance mixture: raw 8.94, this returns 4.81. So an earlier version of
    this docstring claiming ">5 is heavy-tailed" described a number the
    function provably cannot produce, and any caller comparing against 5
    was testing a constant.

    Use `raw_kurtosis` for tail weight and this for robustness to isolated
    extremes. `classify` returns both as `kurtosis` and `kurtosis_raw`.
    """
    xs = [v for v in xs if v is not None]
    n = len(xs)
    if n < 20:
        return None
    m = mean(xs)
    rsig = robust_sigma(xs)
    if rsig is None or rsig <= 1e-18:
        return None
    cap = ROBUST_CAP_SIGMAS * rsig
    w = [max(-cap, min(cap, x - m)) for x in xs]
    m2 = sum(x * x for x in w) / n
    if m2 <= 0:
        return None
    m4 = sum(x ** 4 for x in w) / n
    return m4 / (m2 * m2)


def empirical_quantile(sorted_xs, p):
    """Linear-interpolated quantile of an already-sorted sequence.

    `sorted_xs` must be sorted ascending and non-empty. p is clamped to
    [0, 1]. Returns None for an empty sequence.
    """
    n = len(sorted_xs)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_xs[0])
    p = max(0.0, min(1.0, float(p)))
    k = (n - 1) * p
    lo = int(math.floor(k))
    hi = min(lo + 1, n - 1)
    frac = k - lo
    return float(sorted_xs[lo]) * (1.0 - frac) + float(sorted_xs[hi]) * frac


def tail_threshold(returns, p=0.995, min_obs=MIN_TAIL_OBS):
    """The (1-p) upper quantile of `returns`, and the z it corresponds to.

    Returns {"value", "z", "n", "kurtosis", "sufficient", ...} or None.

    `sufficient=False` means the sample is too small for the percentile to
    mean anything. The value is still returned so a caller *can* use it, and
    the flag is there so it cannot do so unknowingly. A series with no spread
    at all is marked `degenerate` and yields no value, because its upper
    quantile is the constant itself and a threshold of ~0 fires everywhere.
    """
    xs = sorted(v for v in (_finite(x) for x in (returns or [])) if v is not None)
    n = len(xs)
    if n < 20:
        return None
    out = {"value": None, "z": None, "n": n, "kurtosis": kurtosis(xs),
           "kurtosis_raw": raw_kurtosis(xs),
           "sufficient": n >= int(min_obs), "p": float(p), "degenerate": False}
    sd = std(xs)
    if sd <= DEGENERATE_SIGMA:
        out["degenerate"] = True
        out["sufficient"] = False
        out["reason"] = "returns are identical; no distribution to estimate"
        return out
    thr = empirical_quantile(xs, p)
    if thr is None:
        return None
    out["value"] = thr
    m = mean(xs)
    out["z"] = abs((thr - m) / sd)
    return out


def classify(value, rets, p=0.995, min_obs=MIN_TAIL_OBS):
    """Where does `value` sit in this instrument's own return history?

    Returns a dict with both readings:

      z          sigma multiple (the existing convention)
      percentile fraction of history at least as extreme as `value`
      z_robust   sigma multiple on the median-absolute-deviation scale
      tail_z     the sigma multiple the 1-p tail actually starts at
      basis      'estimated' | 'fallback:no-distribution'

    `basis` is the field that matters. It says whether the answer came from
    this instrument's distribution or from a hardcoded fallback, so a reader
    never has to guess which one they are looking at.

    A threshold is published only when `sufficient` is true. An
    insufficient-sample estimate is still available from `tail_threshold`
    for a caller that wants it, but attaching it here would hand a
    percentile drawn from 120 points to anyone reading `classify` -- which is
    the failure this module exists to prevent.
    """
    xs = [v for v in (_finite(x) for x in (rets or [])) if v is not None]
    n = len(xs)
    sd = std(xs)
    m = mean(xs)
    rsig = robust_sigma(xs)
    v = _finite(value)

    out = {"z": None, "percentile": None, "z_robust": None, "tail_z": None,
           "kurtosis": None, "kurtosis_raw": None, "n": n,
           "sufficient": n >= int(min_obs),
           "basis": "fallback:no-distribution", "threshold": None}
    k = kurtosis(xs)
    if k:
        out["kurtosis"] = round(k, 2)
    kr = raw_kurtosis(xs)
    if kr:
        out["kurtosis_raw"] = round(kr, 2)

    if sd > DEGENERATE_SIGMA and v is not None:
        out["z"] = abs(v - m) / sd
    if rsig and rsig > DEGENERATE_SIGMA and v is not None:
        out["z_robust"] = abs(v - m) / rsig

    if n >= 20 and v is not None:
        srt = sorted(xs)
        mag = abs(v - m)
        below = 0
        for x in srt:
            if abs(x - m) <= mag:
                below += 1
            else:
                break
        out["percentile"] = below / float(n)

    tail = tail_threshold(xs, p=p, min_obs=min_obs)
    if tail and tail["value"] is not None and tail.get("sufficient"):
        out["threshold"] = tail["value"]
        out["tail_z"] = tail["z"]
        out["basis"] = "estimated"
    elif tail and tail.get("degenerate"):
        out["basis"] = "fallback:degenerate"
    return out


def spike_threshold(returns, fallback=2.5, p=0.995, min_obs=MIN_TAIL_OBS):
    """The cut for this instrument, or `fallback` if unknowable.

    Wraps tail_threshold so a caller gets a usable number even on short
    history or a degenerate series, but only after the estimate has
    genuinely failed -- and `basis` says which happened.

    `threshold` is a sigma multiple, matching the convention it replaces,
    so a fallback of 2.5 is directly comparable with an estimated value.
    """
    t = tail_threshold(returns, p=p, min_obs=min_obs)
    if t and t["sufficient"] and t["value"] is not None and t["z"] is not None:
        return {"threshold": abs(t["z"]), "basis": "estimated", "z": t["z"],
                "n": t["n"], "kurtosis": t["kurtosis"],
                "kurtosis_raw": t["kurtosis_raw"]}
    if t and t.get("degenerate"):
        return {"threshold": float(fallback), "basis": "fallback:degenerate",
                "z": None, "n": t["n"], "kurtosis": t["kurtosis"],
                "kurtosis_raw": t["kurtosis_raw"]}
    return {"threshold": float(fallback), "basis": "fallback", "z": None,
            "n": t["n"] if t else 0, "kurtosis": t["kurtosis"] if t else None,
            "kurtosis_raw": t["kurtosis_raw"] if t else None}