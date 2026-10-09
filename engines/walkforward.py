"""Walk-forward validation for the backtester. Stdlib only, no RNG.

Why this exists
---------------
`engines.backtest.run_backtest` runs one parameter set over one window and
reports what it found. That number is not a performance estimate. It is
the maximum of a search, and the maximum of a search is biased upward --
increasingly so with every extra parameter you are willing to try. The
way to make a backtest mean something is to stop asking "what did this
parameter set score?" and start asking "if I had chosen these parameters
without seeing this data, what would they have scored?".

So: split the tape into rolling in-sample / out-of-sample folds, pick
the best parameters on the in-sample slice alone, then score exactly
that pick on data it never saw. The aggregated out-of-sample figure is
the only number here worth quoting. The in-sample figure is included
only so the degradation between them is visible -- a strategy whose OOS
score collapses to nothing has memorised its history, whatever its
headline Sharpe says.

Robustness
----------
A parameter set whose immediate neighbours score far worse is sitting on
a spike, and a spike is an artefact. A set that sits on a plateau is
describing something real about the instrument. `_plateau_score`
measures the spread of the neighbourhood, so a caller can prefer a
mediocre-but-stable configuration over a spectacular-but-brittle one.

Everything is deterministic: no sampling, no seeds, no wall-clock.
Identical inputs produce identical output, which is what makes the
numbers comparable between runs.
"""

import math

try:
    from engines.backtest import run_backtest
except ImportError:  # pragma: no cover - direct-path execution
    from backtest import run_backtest

DEFAULT_FOLDS = 3
DEFAULT_IS_RATIO = 0.6
# Bound the search. A grid this size finishes in a couple of seconds on
# the 1m tape; the caller can raise it, but an unbounded sweep on a
# request thread is how you get a thread-pool exhaustion outage.
MAX_COMBOS = 36
# Bars fed to each fold's optimiser. The outer tape is already 20k bars;
# the sweep works on a slice, and a shorter slice is both faster and
# less prone to fitting one long regime. Measured on the 1m tape: this
# keeps a 9-combo x 3-fold sweep near 2-3s, which is the difference
# between a usable button and a request that looks hung.
SWEEP_LIMIT = 1200


def _f(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return v


def param_grid(spec):
    """Expand {fast: [5, 9], slow: [21, 34]} into a list of dicts.

    Cartesian product, capped at MAX_COMBOS and truncated in a stable
    order so a truncated sweep is still reproducible.
    """
    if not spec:
        return [{}]
    keys = sorted(spec.keys())
    combos = [{}]
    for k in keys:
        vals = spec[k]
        if not isinstance(vals, (list, tuple)):
            vals = [vals]
        nxt = []
        for base in combos:
            for v in vals:
                d = dict(base)
                d[k] = v
                nxt.append(d)
        combos = nxt
        if len(combos) > MAX_COMBOS:
            combos = combos[:MAX_COMBOS]
            break
    return combos


def objective(metrics, key="sharpe"):
    """Score a metrics dict. Higher is better, always finite.

    A backtest that made no trades scores 0 rather than an undefined
    value, so a parameter set that never fires cannot win the sweep by
    accident.
    """
    m = metrics or {}
    if key == "profit_factor":
        pf = _f(m.get("pf"))
        # Unbounded profit factor is meaningless: one lucky trade makes
        # it enormous. Cap so a 1-trade run cannot outrank a real edge.
        return min(pf, 10.0)
    if key == "net_pnl":
        return _f(m.get("net_pnl"))
    v = _f(m.get(key))
    return v if math.isfinite(v) else 0.0


def _folds(n, folds, is_ratio):
    """Yield (is_start, is_end, oos_start, oos_end) bar-index windows.

    Folds advance through the tape; each fold's out-of-sample slice is
    the region the next fold will treat as history, so every bar is
    eventually tested out-of-sample except the warmup remainder.
    """
    if n < 60 or folds < 1:
        return []
    span = n // folds
    if span < 40:
        return []
    out = []
    for i in range(folds):
        start = i * span
        is_len = max(30, int(span * is_ratio))
        is_end = start + is_len
        oos_end = min(n, start + span)
        if oos_end - is_end < 10:
            continue
        out.append((start, is_end, is_end, oos_end))
    return out


def _plateau_score(scored, chosen):
    """How much worse is the neighbourhood than the peak?

    Returns 0.0 for a lone winner (knife-edge) up to 1.0 for a broad
    plateau where neighbours score nearly as well as the peak. Callers
    that care about robustness can rank on this.
    """
    if not scored:
        return 0.0
    vals = [s for _p, s in scored]
    best = max(vals)
    if best <= 0:
        return 0.0
    others = [v for v in vals if v < best]
    if not others:
        return 1.0
    second = max(others)
    return max(0.0, min(1.0, second / best))


def walk_forward(candles, strategy_kind, grid=None, folds=DEFAULT_FOLDS,
                 is_ratio=DEFAULT_IS_RATIO, objective_key="sharpe",
                 fee_bps=2.0, slippage_bps=2.0, warmup=60,
                 params_base=None, sweep_limit=SWEEP_LIMIT):
    """Run rolling in-sample optimisation with out-of-sample scoring.

    Returns a dict containing:
      folds       per-fold chosen params + both scores
      oos         aggregate out-of-sample metrics (the honest number)
      is          aggregate in-sample metrics (for comparison only)
      degradation oos_score / is_score, and how to read it
      robustness  plateau measure for the overall best configuration
      combos      how many parameter sets were actually tried

    Never raises on bad input: a tape too short to fold returns a
    `skipped` result rather than an exception, so the UI can say why.
    """
    candles = list(candles or [])
    n = len(candles)
    combos = param_grid(grid)
    base = dict(params_base or {})

    result = {
        "strategy": str(strategy_kind or ""),
        "objective": objective_key,
        "folds": [],
        "combos": len(combos),
        "folds_requested": int(folds),
        "oos": {}, "is": {}, "degradation": None, "robustness": 0.0,
        "verdict": "", "skipped": None, "candles": n,
    }
    windows = _folds(n, int(folds), float(is_ratio))
    if not windows:
        result["skipped"] = (
            "need at least 60 candles and a fold span of 40+; have {}".format(n))
        result["verdict"] = "not enough data to validate"
        return result
    if not combos:
        result["skipped"] = "no parameter grid supplied"
        result["verdict"] = "no parameters to validate"
        return result

    is_scores, oos_scores, all_scored = [], [], []
    for (s0, s1, o0, o1) in windows:
        is_slice = candles[s0:s1]
        oos_slice = candles[o0:o1]
        if len(is_slice) < warmup + 5 or len(oos_slice) < 5:
            continue
        # Score the whole grid on in-sample data only. The slice is
        # capped so fold cost stays bounded regardless of tape length.
        if len(is_slice) > sweep_limit:
            is_slice = is_slice[-int(sweep_limit):]
        scored = []
        for combo in combos:
            p = dict(base)
            p.update(combo)
            try:
                r = run_backtest(is_slice, strategy_kind, p, fee_bps, slippage_bps)
            except Exception:
                continue
            sc = objective(r.get("metrics", {}), objective_key)
            scored.append((combo, sc, r.get("metrics", {})))
        if not scored:
            continue
        scored.sort(key=lambda x: (-x[1], sorted(x[0].items())))
        best_combo, best_is, best_metrics = scored[0]
        all_scored.extend([(p, s) for p, s, _m in scored])

        # Now -- and only now -- look at the unseen data.
        p = dict(base)
        p.update(best_combo)
        try:
            oos_res = run_backtest(oos_slice, strategy_kind, p, fee_bps, slippage_bps)
        except Exception:
            continue
        oos_m = oos_res.get("metrics", {})
        oos_sc = objective(oos_m, objective_key)

        is_scores.append(best_is)
        oos_scores.append(oos_sc)
        result["folds"].append({
            "is_bars": [s0, s1], "oos_bars": [o0, o1],
            "params": best_combo,
            "is_score": round(best_is, 4),
            "oos_score": round(oos_sc, 4),
            "is_trades": int(_f(best_metrics.get("trades"))),
            "oos_trades": int(_f(oos_m.get("trades"))),
            "oos_sharpe": round(_f(oos_m.get("sharpe")), 4),
            "oos_pf": round(_f(oos_m.get("pf")), 4),
            "oos_maxDD": round(_f(oos_m.get("maxDD")), 4),
        })

    if not result["folds"]:
        result["skipped"] = "no fold had enough bars to run"
        result["verdict"] = "not enough data to validate"
        return result

    is_avg = sum(is_scores) / len(is_scores)
    oos_avg = sum(oos_scores) / len(oos_scores)
    result["is"] = {"avg_score": round(is_avg, 4), "folds": len(is_scores)}
    result["oos"] = {"avg_score": round(oos_avg, 4), "folds": len(oos_scores)}
    result["robustness"] = round(_plateau_score(all_scored, None), 3)
    result["degradation"] = round(oos_avg / is_avg, 3) if is_avg > 0 else None

    # Verdict. These thresholds are deliberately blunt: the point is to
    # stop an operator reading a good in-sample number as a good system.
    d = result["degradation"]
    if d is None:
        result["verdict"] = "no profitable in-sample fold -- nothing to validate"
    elif d >= 0.7 and result["robustness"] >= 0.5:
        result["verdict"] = "holds up out-of-sample on a broad plateau"
    elif d >= 0.7:
        result["verdict"] = "holds up out-of-sample but sits on a narrow spike"
    elif d >= 0.3:
        result["verdict"] = "degrades out-of-sample -- partly curve-fit"
    else:
        result["verdict"] = "does not survive out-of-sample -- treat as noise"
    return result
