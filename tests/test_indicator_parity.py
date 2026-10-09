"""Parity check: the indicator fast paths must not change a single number.

Compares the current (optimised) engine against a reference
reimplementation that does the naive thing everywhere, across several
strategies and deterministic fixtures. Any divergence is a regression:
the point of the optimisation was speed, not different answers.
"""
import math
import random
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engines.backtest import run_backtest
from engines import signals as S
from engines import indicators as I


def mk(closes):
    return [{"ts": i, "o": c, "h": c * 1.001, "l": c * 0.999, "c": c, "v": 1000.0}
            for i, c in enumerate(closes)]


def naive_ema(values, period):
    """Reference: always coerce, no fast path."""
    vals = [I._f(v) for v in (values or [])]
    n = len(vals)
    if n == 0:
        return []
    if period is None or period <= 1:
        return list(vals)
    k = 2.0 / (float(period) + 1.0)
    m = min(n, int(period))
    seed = sum(vals[:m]) / m
    out = [seed]
    for x in vals[1:]:
        out.append(x * k + out[-1] * (1.0 - k))
    return out


def naive_closes(candles):
    return [I._f(c.get("c") if isinstance(c, dict) else c) for c in (candles or [])]


FAIL = []


def check(label, ok, detail=""):
    print("  %-52s %s %s" % (label, "PASS" if ok else "FAIL", detail))
    if not ok:
        FAIL.append(label + " " + detail)


print("=== 1. ema_series fast path == naive ===")
rnd = random.Random(1234)          # fixed seed: deterministic
for period in (2, 5, 9, 12, 21, 26, 55, 200):
    for n in (1, 2, 5, 20, 60, 250, 900):
        series = [100.0 + rnd.uniform(-1, 1) for _ in range(n)]
        a = I.ema_series(list(series), period)
        b = naive_ema(list(series), period)
        same = len(a) == len(b) and all(abs(x - y) < 1e-12 for x, y in zip(a, b))
        if not same:
            check("period=%d n=%d" % (period, n), False)
            break
    else:
        continue
    break
else:
    check("27 (period x length) float series cases", True)

# dirty inputs must still be coerced exactly as before
dirty_cases = [
    ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12"],
    [None, 1.0, 2.0, 3.0, float("nan"), 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0],
    [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],          # ints
    [1.0, 2.0, 3.0, float("inf"), 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0],
    [], [5.0], [float("nan")], [None, None],
]
ok = True
for d in dirty_cases:
    a = I.ema_series(list(d), 5)
    b = naive_ema(list(d), 5)
    if len(a) != len(b) or not all(abs(x - y) < 1e-12 for x, y in zip(a, b)):
        ok = False
        print("     dirty mismatch:", d, a, b)
check("dirty/nan/inf/int/str inputs unchanged", ok)

print("\n=== 2. closes hint == closes_of (per strategy) ===")
CASES = {
    "ema_trend": {"fast": 9, "slow": 21},
    "dual_ema": {"fast": 5, "slow": 20},
    "vwap_pullback": {"lookback": 20, "dev_bps": 10},
    "mean_reversion": {"period": 20, "entry_z": 2.0},
}
for n in (30, 120, 400):
    base = [100.0 + rnd.uniform(-2, 2) + i * 0.05 for i in range(n)]
    cd = mk(base)
    quote = {"bid": base[-1] * 0.9999, "ask": base[-1] * 1.0001, "last": base[-1]}
    for name, params in CASES.items():
        hinted = S.preview_signal(name, cd, quote, params, closes=list(base))
        plain = S.preview_signal(name, cd, quote, params)
        same = (hinted.get("side") == plain.get("side")
                and hinted.get("reason") == plain.get("reason")
                and hinted.get("qty") == plain.get("qty"))
        if not same:
            check("%s n=%d" % (name, n), False,
                  "hinted=%r plain=%r" % (hinted, plain))
            break
    else:
        continue
    break
else:
    check("4 strategies x 3 lengths: side/reason/qty identical", True)

# mismatched length must be ignored, not trusted
bad = S.preview_signal("ema_trend", mk(base), quote, CASES["ema_trend"],
                       closes=[1.0, 2.0])
good = S.preview_signal("ema_trend", mk(base), quote, CASES["ema_trend"])
check("mismatched closes length is ignored, not used",
      bad.get("reason") == good.get("reason"),
      "%r vs %r" % (bad.get("reason"), good.get("reason")))

print("\n=== 3. full backtest metrics unchanged ===")
# Reference run with the closes hint suppressed entirely, by calling the
# signal path the naive way: temporarily force mismatched length so
# preview_signal discards the hint and recomputes.
import engines.backtest as B

for name, params, n in (
        ("dual_ema", {"fast": 9, "slow": 21}, 900),
        ("ema_trend", {"fast": 9, "slow": 21}, 900),
        ("mean_reversion", {"period": 20, "entry_z": 2.0}, 900),
        ("vwap_pullback", {"lookback": 20, "dev_bps": 10}, 900),
):
    base = [100.0 + rnd.uniform(-2, 2) + i * 0.05 for i in range(n)]
    cd = mk(base)
    fast = run_backtest(cd, name, dict(params), 2.0, 2.0)

    # Force the slow path: patch preview_signal to drop the hint.
    orig = B.preview_signal

    def no_hint(kind, candles, quote, p, closes=None):
        return orig(kind, candles, quote, p)      # hint discarded

    B.preview_signal = no_hint
    try:
        slow = run_backtest(cd, name, dict(params), 2.0, 2.0)
    finally:
        B.preview_signal = orig

    fm, sm = fast["metrics"], slow["metrics"]
    same_metrics = all(
        abs(float(fm.get(k, 0)) - float(sm.get(k, 0))) < 1e-9
        for k in ("trades", "wins", "win_rate", "pf", "maxDD", "sharpe", "cagr")
    )
    same_eq = (len(fast["equity"]) == len(slow["equity"]) and
               all(abs(a - b) < 1e-9 for a, b in zip(fast["equity"], slow["equity"])))
    same_trades = len(fast["trades"]) == len(slow["trades"]) and all(
        abs(a["pnl"] - b["pnl"]) < 1e-9 and a["entry_idx"] == b["entry_idx"]
        for a, b in zip(fast["trades"], slow["trades"]))
    check("%-15s metrics+equity+trades identical" % name,
          same_metrics and same_eq and same_trades,
          "" if (same_metrics and same_eq and same_trades)
          else "fast=%r slow=%r" % (fm, sm))

print("\n=== 4. speed ===")
import time
base = [100.0 + rnd.uniform(-2, 2) + i * 0.05 for i in range(1200)]
cd = mk(base)
params = {"fast": 9, "slow": 21}

t0 = time.time(); run_backtest(cd, "dual_ema", dict(params), 2.0, 2.0)
fast_t = time.time() - t0

orig = B.preview_signal
B.preview_signal = lambda k, c, q, p, closes=None: orig(k, c, q, p)
t0 = time.time(); run_backtest(cd, "dual_ema", dict(params), 2.0, 2.0)
slow_t = time.time() - t0
B.preview_signal = orig

print("  optimised: %.3fs   reference path: %.3fs   speedup: %.2fx"
      % (fast_t, slow_t, (slow_t / fast_t) if fast_t else 0))
check("optimised path is not slower", fast_t <= slow_t * 1.02,
      "%.3f vs %.3f" % (fast_t, slow_t))

print("\n" + "=" * 62)
print("FAILURES: %d" % len(FAIL))
for f in FAIL:
    print("  -", f)
if not FAIL:
    print("PARITY CONFIRMED — optimisation is numerically inert")
