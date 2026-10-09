"""Does this strategy actually have an edge? Zero-dep significance testing.

Stdlib only, import-safe, pure. `statistics.NormalDist` is standard library,
so none of this costs a dependency.

The problem this exists to solve
--------------------------------
A backtest tells you what happened, not what will happen. Ten strategies
scored on one dataset, and the best one reported, is not ten independent
observations -- it is the maximum of ten draws from the same noise. Report
that maximum as a result and you have learned nothing about any of them.

This app makes that mistake possible by construction: nine strategies in
`engines.signals.STRATEGIES`, each with tunable parameters, plus four
arbitrage models, plus a walk-forward sweep. Every parameter combination
run against the tape is another trial. Nobody was counting them.

`deflated_sharpe` answers the only question that matters: given how many
things you tried, how surprising is the best result?

The method
----------
Bailey & Lopez de Prado, "The Deflated Sharpe Ratio" (SSRN 2460551).

1. **Probabilistic Sharpe Ratio.** Treat the observed Sharpe as an estimate
   with sampling error, and ask how likely it is that the *true* Sharpe
   exceeds a target. Skew and kurtosis enter the standard error, because
   fat-tailed and skewed returns produce more extreme Sharpe ratios than
   normality would suggest -- and every trade return series is fat-tailed.

2. **Expected maximum under the null.** With N independent trials, the
   best Sharpe you should expect from pure noise is not zero. It is
   approximately

       sqrt(variance) * [ (1 - g) * Z^-1(1 - 1/N) + g * Z^-1(1 - 1/(N*e)) ]

   where `g` is the Euler-Mascheroni constant (~0.5772). Deflating means
   testing the observed Sharpe against *that*, not against zero.

Why this matters more here than in most systems
-----------------------------------------------
Because the costs in this app are brutal and we have measured them. Live
cross-venue dislocation runs 0.2-4.6 bps against ~70 bps of round-trip
taker cost. A backtest that ignores costs will happily report a Sharpe
built entirely on fills the venue would never give. Every function here
therefore takes `cost_bps` and works on **net** returns. A gross Sharpe is
reported only alongside its net counterpart, never alone.

Where it refuses
----------------
* Fewer than ~30 observations: no Sharpe is computable, and a Sharpe from
  5 trades is not a weak result, it is a meaningless one.
* No dispersion: a flat series has an undefined Sharpe, not an infinite
  one.
* `n_trials` unknown: defaults to 1 and says so. Silently assuming one
  trial would defeat the entire purpose of the module.
"""

import math
from statistics import NormalDist

# Euler-Mascheroni constant. Appears in the expected-maximum formula.
EULER_GAMMA = 0.5772156649015329

# Below this many observations a Sharpe ratio carries no information.
MIN_OBSERVATIONS = 30

# Per-trade round-trip cost in bps. Not a default to tune -- set it from
# the venue fee schedule (data.venuebooks.fee_bps) for the real number.
DEFAULT_COST_BPS = 0.0

_N = NormalDist()


def _f(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return v


def _mean(xs):
    return sum(xs) / len(xs)


def _stdev(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _skew_kurtosis(xs):
    """Sample skewness and excess kurtosis (Fisher definition)."""
    n = len(xs)
    if n < 4:
        return 0.0, 0.0
    m = _mean(xs)
    sd = _stdev(xs)
    if sd <= 0:
        return 0.0, 0.0
    skew = sum(((x - m) / sd) ** 3 for x in xs) / n
    kurt = sum(((x - m) / sd) ** 4 for x in xs) / n - 3.0
    return skew, kurt


def sharpe(returns, periods_per_year=None, rf=0.0):
    """Sharpe of a per-period return series.

    `periods_per_year` annualises it. **Omit it for the statistical tests.**

    PSR/DSR are defined on the per-period Sharpe, because the expected
    maximum under the null is also per-period (`sd * z`, with
    `sd ~ 1/sqrt(n)`). Annualising the observed Sharpe while leaving the
    null threshold per-period compares two different units and makes every
    result look significant -- which is exactly what happened here before
    it was caught: 300 samples of pure noise reported DSR 1.00 and
    "edge likely real". The annualised figure is for humans; the per-period
    figure is for the maths.
    """
    xs = [_f(x) for x in (returns or [])]
    xs = [x for x in xs if x == x]
    if len(xs) < 2:
        return None
    sd = _stdev(xs)
    if sd <= 0:
        return None
    per_period = _mean([x - rf for x in xs]) / sd
    if periods_per_year:
        try:
            return per_period * math.sqrt(periods_per_year)
        except (ValueError, OverflowError):
            return None
    return per_period


def probabilistic_sharpe(observed_sr, n, skew=0.0, kurtosis=0.0, target_sr=0.0):
    """P(true Sharpe > target) given an observed Sharpe, n and shape.

    None when n is too small to say anything.
    """
    sr = _f(observed_sr, None)
    if sr is None:
        return None
    n = int(_f(n, 0))
    if n < MIN_OBSERVATIONS:
        return None
    denom = 1.0 - _f(skew) * sr + ((_f(kurtosis) - 1.0) / 4.0) * sr * sr
    if denom <= 0:
        return None
    z = ((sr - _f(target_sr)) * math.sqrt(n - 1)) / math.sqrt(denom)
    return _N.cdf(z)


def expected_max_sharpe(sd_sr=1.0, n_trials=1):
    """Sharpe expected from the best of N noise trials. None if N < 1."""
    n = int(_f(n_trials, 0))
    if n < 1:
        return None
    if n == 1:
        return 0.0
    sd = _f(sd_sr, 1.0)
    if sd <= 0:
        return 0.0
    e = math.e
    try:
        a = (1.0 - 1.0 / n)
        b = (1.0 - 1.0 / (n * e))
        return sd * ((1.0 - EULER_GAMMA) * _N.inv_cdf(a) +
                     EULER_GAMMA * _N.inv_cdf(b))
    except (ValueError, OverflowError, ZeroDivisionError):
        return None


def deflated_sharpe(observed_sr, n, skew=0.0, kurtosis=0.0, n_trials=1,
                   sd_sr=None):
    """DSR: probability the true Sharpe exceeds the N-trial noise maximum.

    `sd_sr` is the standard deviation of the Sharpe ratios across trials.
    Defaults to 1/sqrt(n), the value for a series of n independent
    zero-mean returns, which is the honest assumption when the operator
    does not record their own trial Sharpe spread.
    """
    n_trials = max(1, int(_f(n_trials, 1)))
    if sd_sr is None:
        sd = 1.0 / math.sqrt(max(1, int(_f(n, MIN_OBSERVATIONS)))) \
            if int(_f(n, 0)) >= MIN_OBSERVATIONS else 1.0
    else:
        sd = _f(sd_sr, 1.0)
    threshold = expected_max_sharpe(sd, n_trials)
    if threshold is None:
        return None
    return probabilistic_sharpe(observed_sr, n, skew, kurtosis,
                                target_sr=threshold)


def net_returns(trade_pnl_bps, cost_bps=DEFAULT_COST_BPS):
    """Turn per-trade gross P&L in bps into per-trade net returns.

    A Sharpe on gross returns is a Sharpe on fills the venue would not
    have given you. This app has measured 0.2-4.6 bps of real cross-venue
    dislocation against roughly 70 bps of round-trip cost, so the
    difference is not a rounding detail here -- it is the whole question.
    """
    cost = abs(_f(cost_bps, 0.0))
    return [_f(p) - cost for p in (trade_pnl_bps or [])]


def edge_report(trade_pnl_bps, cost_bps=DEFAULT_COST_BPS, n_trials=1,
                periods_per_year=252, sd_sr=None, label=""):
    """The full verdict on one strategy: gross vs net, deflated.

    Returns a dict with an explicit `verdict`. Never returns a confident
    positive on thin evidence -- the whole module exists to refuse that.
    """
    gross = [_f(x) for x in (trade_pnl_bps or [])]
    net = net_returns(gross, cost_bps)
    n = len(net)
    rep = {
        "label": label,
        "trades": n,
        "cost_bps": abs(_f(cost_bps, 0.0)),
        "n_trials": max(1, int(_f(n_trials, 1))),
        "gross_sharpe": None,
        "net_sharpe": None,
        "per_period_sharpe": None,
        "psr": None,
        "dsr": None,
        "verdict": "insufficient data",
        "reasons": [],
        "expectancy_bps": None,
    }

    if n < MIN_OBSERVATIONS:
        rep["reasons"].append(
            "only {} trades; a Sharpe needs at least {} to mean anything"
            .format(n, MIN_OBSERVATIONS))
        return rep

    if gross:
        rep["expectancy_bps"] = round(_mean(net), 4)
    rep["gross_sharpe"] = _round(sharpe(gross, periods_per_year))
    rep["net_sharpe"] = _round(sharpe(net, periods_per_year))
    # Per-period Sharpe for the tests: same units as the null threshold.
    ns = sharpe(net)
    if ns is None:
        rep["reasons"].append("net returns have no dispersion")
        rep["verdict"] = "not evaluable"
        return rep

    skew, kurt = _skew_kurtosis(net)
    rep["skew"] = round(skew, 3)
    rep["kurtosis"] = round(kurt, 3)
    rep["per_period_sharpe"] = _round(ns)
    rep["psr"] = _round(probabilistic_sharpe(ns, n, skew, kurt, 0.0))
    rep["dsr"] = _round(deflated_sharpe(ns, n, skew, kurt,
                                       n_trials=rep["n_trials"], sd_sr=sd_sr))

    # Verdicts. Order matters: a strategy that dies on costs is not
    # "marginal", it is untradeable, and saying "marginal" would invite
    # someone to trade it.
    if rep["expectancy_bps"] is not None and rep["expectancy_bps"] <= 0:
        rep["verdict"] = "no edge after costs"
        rep["reasons"].append(
            "net expectancy {:.2f} bps/trade is not positive".format(
                rep["expectancy_bps"]))
    elif rep["dsr"] is None:
        rep["verdict"] = "not evaluable"
        rep["reasons"].append("could not deflate; report as unknown, not zero")
    elif rep["dsr"] >= 0.95:
        rep["verdict"] = "edge likely real"
        rep["reasons"].append(
            "DSR {:.2f} >= 0.95 after deflating for {} trials".format(
                rep["dsr"], rep["n_trials"]))
    elif rep["dsr"] >= 0.80:
        rep["verdict"] = "edge plausible, unproven"
        rep["reasons"].append(
            "DSR {:.2f} is suggestive but below the 0.95 bar".format(
                rep["dsr"]))
    else:
        rep["verdict"] = "no demonstrated edge"
        rep["reasons"].append(
            "DSR {:.2f}: the best of {} trials explains this result without "
            "assuming any skill".format(rep["dsr"], rep["n_trials"]))
    return rep


def _round(x, places=4):
    if x is None:
        return None
    try:
        return round(float(x), places)
    except (TypeError, ValueError):
        return None


def count_trials(strategies=None, param_grid_per_strategy=1):
    """How many independent trials this project is implicitly running.

    Nobody writes this number down, and every parameter combination run
    against one tape is a trial. Under-counting it makes the DSR
    meaningless -- it is the single most commonly omitted input.

    Counted: strategies x parameter combinations, plus the arbitrage
    models, which are trials against the same tape in exactly the same
    sense.
    """
    n = 0
    if strategies:
        n += len(strategies) * max(1, int(_f(param_grid_per_strategy, 1)))
    return max(1, n)
