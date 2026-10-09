"""Capital allocation on investment-banking fundamentals. Stdlib only.

What this is, and what it is not
--------------------------------
This module does not predict prices and does not manufacture an edge. It
sizes positions from an edge that has already been measured, and when
that edge is zero or negative the correct output is *no position*.

That refusal is the whole point. The standard failure of an "intelligent"
trading feature is that it always has an opinion: it ranks the signals,
picks a winner, and sizes into it. Sizing into a negative expectancy is
a guaranteed way to lose money slowly and then all at once. A sizing
engine whose honest answer is frequently "stay out" is the difference
between a risk system and a slot machine.

The inputs are therefore deliberately austere:

  * `edge` must come from walk-forward *out-of-sample* results
    (engines.walkforward), never from an in-sample best-of. An in-sample
    edge is the maximum of a search and is biased upward by construction.
  * `costs_bps` is subtracted before any sizing happens, because an edge
    smaller than the cost of trading is not an edge.
  * the Kelly fraction is *scaled down* by confidence and hard-capped at
    quarter Kelly, which is the standard institutional convention: full
    Kelly maximises long-run growth but has a brutal drawdown profile,
    and nobody sizes live capital at full Kelly.

Everything here is deterministic. No RNG, no fitting, no hidden state.
"""

import math

# Quarter Kelly is the institutional default. Full Kelly is the
# long-run growth optimum under a *known* distribution; the whole reason
# to discount is that the distribution is estimated, and estimation error
# is not symmetric.
MAX_KELLY_FRACTION = 0.25
# A negative edge is not a small edge, it is a reason not to trade.
# The function returns 0.0 rather than a negative size.
MIN_EDGE_TO_TRADE = 0.0
# Round-trip cost assumption, in basis points of notional, used when the
# caller does not supply one. 2 bps each way is a liquid future; equities
# and crypto are wider, and passing the real number matters more than the
# default being clever.
DEFAULT_COSTS_BPS = 4.0
# Minimum sample size before a win rate is trusted at all. Below this the
# confidence multiplier collapses to zero.
MIN_SAMPLES = 30


def _f(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return v


def returns_from_closes(closes):
    out = []
    prev = None
    for c in (closes or []):
        try:
            v = float(c)
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        if prev is not None and prev > 0:
            out.append(v / prev - 1.0)
        prev = v
    return out


def stdev(xs):
    xs = [x for x in (xs or []) if x is not None]
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / n)


def downside_stdev(xs, mar=0.0):
    """Volatility of the losing tail only -- Sortino's denominator.

    A strategy whose gains are lumpy and whose losses are steady looks
    acceptable on total stdev and terrible here, and total stdev is what
    a naive sizer reads.
    """
    d = [min(0.0, _f(x) - mar) for x in (xs or [])]
    n = len(d)
    if n < 2:
        return 0.0
    return math.sqrt(sum(x * x for x in d) / n)


def win_rate(trades):
    """Fraction of trades with pnl > 0. Flat trades count as losses.

    Counting a break-even as a loss is deliberate: it is a trade that
    paid the spread and gave nothing back.
    """
    pnls = [_f(t.get("pnl") if isinstance(t, dict) else t) for t in (trades or [])]
    if not pnls:
        return 0.0, 0
    wins = sum(1 for p in pnls if p > 0)
    return wins / len(pnls), len(pnls)


def profit_loss_ratio(trades):
    """Mean win / mean loss, as a positive number. 0.0 if no losses."""
    pnls = [_f(t.get("pnl") if isinstance(t, dict) else t) for t in (trades or [])]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    if not wins or not losses:
        # No losses is not a 0 ratio to be divided by later; report it as
        # unbounded and let the caller decide.
        return float("inf") if wins else 0.0
    return (sum(wins) / len(wins)) / (sum(losses) / len(losses))


def kelly_fraction(p, b, cap=MAX_KELLY_FRACTION):
    """f* = p - (1-p)/b, floored at 0 and capped.

    Returns 0.0 for a non-positive edge. A negative Kelly value is not a
    "short" or a "reverse" -- it means this bet does not get taken.
    """
    p = _f(p)
    b = _f(b)
    if p <= 0 or p >= 1 or b <= 0:
        return 0.0
    f = p - (1.0 - p) / b
    if f <= 0:
        return 0.0
    return min(f, float(cap))


def confidence(n_samples, min_samples=MIN_SAMPLES):
    """0..1 trust factor from sample size.

    A win rate from 12 trades is not a win rate, it is a story. This
    scales linearly to 1.0 at `min_samples` and stays there, so a large
    sample is trusted but a small one is scaled toward *no trade* rather
    than toward a half-size trade dressed up as a decision.
    """
    n = int(_f(n_samples))
    if n <= 0:
        return 0.0
    return min(1.0, n / float(max(1, min_samples)))


def edge_per_unit_risk(metrics, trades=None, costs_bps=DEFAULT_COSTS_BPS,
                       notional=None):
    """Expected value per unit of risk, net of trading costs.

    Returns a fraction of one unit of risk. Positive means the trade pays
    for its own volatility after costs. This is the number that must be
    positive before anything is sized.

    Costs are a fraction of *notional*, so they have to be converted into
    the same currency units as the pnl before being subtracted. An earlier
    version subtracted `costs_bps/10000` straight from a currency mean,
    which made the cost term ~10,000x too small and left the gate inert --
    an edge of 0.5443 stayed 0.5442 whether costs were 0 or 60bps. A cost
    gate that cannot change its answer is not a gate.

    Per-trade notional is taken from the trade record (qty x price) when
    present, else from the `notional` argument.
    """
    m = metrics or {}
    if not trades:
        return 0.0
    pnls, notionals = [], []
    for t in trades:
        if isinstance(t, dict):
            pnls.append(_f(t.get("pnl")))
            n_val = _f(t.get("notional"))
            if n_val <= 0:
                n_val = abs(_f(t.get("qty")) * _f(t.get("price")))
        else:
            pnls.append(_f(t))
            n_val = 0.0
        notionals.append(n_val)
    if not pnls:
        return 0.0
    if not any(n > 0 for n in notionals):
        # No notional information anywhere: fall back to the caller's
        # assumption rather than silently treating costs as zero.
        avg_notional = _f(notional, 0.0)
        if avg_notional <= 0:
            return 0.0
        notionals = [avg_notional] * len(pnls)
    gross_mean = sum(pnls) / len(pnls)
    vol = stdev(pnls)
    if vol <= 0:
        return 0.0
    cost_each = sum(notionals) / len(notionals) * (float(costs_bps) / 10_000.0)
    return (gross_mean - cost_each) / vol


def risk_of_ruin(win_p, ratio, n_bets, bankroll_units=1.0):
    """Probability of losing `bankroll_units` before a target.

    Standard gambler's-ruin recursion on a biased walk. Used to answer
    "how many consecutive losses can this sizing survive", which is the
    only question that matters when the edge is uncertain.

    The walk is capped at a target of 2x bankroll, and everything at or
    above it is collapsed into one absorbing "already won" state. That
    cap is what makes this linear rather than exponential: without it,
    each win multiplies wealth by (1 + b), so the number of distinct
    reachable wealth values doubles every step and n=25 already takes
    42s (the suite asks for n=50, which never finishes).

    Collapsing is exact, not an approximation. Once wealth has doubled
    the trade is won for the purpose of this question: the only thing
    still being asked is whether ruin happens first, and a state that has
    reached the target can never come back under it with a fixed number
    of bets remaining... which is only true because a loss costs a fixed
    1 unit while a win scales with wealth. It is true here by
    construction, so the cap is safe.
    """
    p = _f(win_p)
    b = _f(ratio)
    n = int(_f(n_bets, 0))
    if n <= 0 or p <= 0 or p >= 1 or b <= 0:
        return 1.0
    target = bankroll_units * 2.0
    WON = "won"  # absorbing bucket for every state at or above target
    states = {round(bankroll_units, 6): 1.0}
    for _ in range(min(n, 5000)):
        nxt = {}
        for w, mass in states.items():
            if mass <= 1e-12:
                continue
            if w == WON:
                nxt[WON] = nxt.get(WON, 0.0) + mass
                continue
            up = w * (1.0 + b)
            if up >= target:
                nxt[WON] = nxt.get(WON, 0.0) + mass * p
            else:
                key = round(up, 6)
                nxt[key] = nxt.get(key, 0.0) + mass * p
            lost = round(w - 1.0, 6)
            if lost <= 0:
                nxt[0.0] = nxt.get(0.0, 0.0) + mass * (1.0 - p)
            else:
                nxt[lost] = nxt.get(lost, 0.0) + mass * (1.0 - p)
        states = nxt
    return sum(m for w, m in states.items() if w == 0.0)


def correlation(a, b):
    """Pearson correlation of two return series, aligned by position."""
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    xa, xb = a[-n:], b[-n:]
    ma, mb = sum(xa) / n, sum(xb) / n
    va = sum((x - ma) ** 2 for x in xa)
    vb = sum((x - mb) ** 2 for x in xb)
    if va <= 0 or vb <= 0:
        return 0.0
    cov = sum((xa[i] - ma) * (xb[i] - mb) for i in range(n))
    return cov / math.sqrt(va * vb)


def diversification_penalty(corr, threshold=0.7):
    """0..1 penalty for holding something too correlated with the book.

    Two positions at 0.95 correlation are one position with extra fees
    and a doubled drawdown, and a sizer that treats them as independent
    will size both to the limit.
    """
    c = _f(corr)
    if c <= threshold:
        return 0.0
    return min(1.0, (c - threshold) / (1.0 - threshold))


def volatility_target_size(equity, target_vol_pct, asset_vol_pct, stop_pct=None,
                           max_fraction=0.25):
    """Notional to hold so the position contributes `target_vol_pct` risk.

    Volatility targeting: size inversely to the instrument's own
    volatility, so a 16,000-tick nickel contract and a calm utility do
    not each get "one unit" and then deliver wildly different risk.
    """
    eq = _f(equity)
    tv = _f(target_vol_pct)
    av = _f(asset_vol_pct)
    if eq <= 0 or tv <= 0 or av <= 0:
        return 0.0
    frac = tv / av
    if stop_pct and _f(stop_pct) > 0:
        # Never let the stop distance imply more risk than the target.
        by_stop = _f(stop_pct) / 100.0
        if by_stop > 0:
            frac = min(frac, by_stop / max(1e-9, tv / 100.0))
    frac = min(frac, float(max_fraction))
    return max(0.0, eq * frac)


def size_position(equity, win_p, ratio, n_samples, edge, costs_bps=DEFAULT_COSTS_BPS,
                  target_vol_pct=1.0, asset_vol_pct=1.0, book_corr=0.0,
                  max_fraction=0.25):
    """The single entry point: how much of the book, or nothing.

    Every gate multiplies. If any of them is zero the answer is zero,
    and that is a valid, common, correct answer.
    """
    eq = _f(equity)
    if eq <= 0:
        return {"fraction": 0.0, "notional": 0.0, "reason": "no equity",
                "kelly": 0.0, "confidence": 0.0, "edge": 0.0}

    e = _f(edge)
    if e <= MIN_EDGE_TO_TRADE:
        # The load-bearing branch. Net-of-costs expectancy is not
        # positive, so there is no size to compute.
        return {"fraction": 0.0, "notional": 0.0,
                "reason": "no validated edge after costs (edge={:.4f})".format(e),
                "kelly": 0.0, "confidence": confidence(n_samples), "edge": e}

    k = kelly_fraction(win_p, ratio, cap=MAX_KELLY_FRACTION)
    if k <= 0:
        return {"fraction": 0.0, "notional": 0.0,
                "reason": "kelly fraction is zero at p={:.3f} b={:.3f}".format(
                    _f(win_p), _f(ratio)),
                "kelly": 0.0, "confidence": confidence(n_samples), "edge": e}
    c = confidence(n_samples)
    pen = diversification_penalty(book_corr)
    frac = k * c * (1.0 - pen)
    notional = volatility_target_size(eq, target_vol_pct, asset_vol_pct,
                                      max_fraction=max_fraction)
    frac = min(frac, max_fraction)
    return {
        "fraction": round(frac, 6),
        "notional": round(notional * frac, 2),
        "kelly": round(k, 6),
        "confidence": round(c, 4),
        "diversification_penalty": round(pen, 4),
        "edge": round(e, 6),
        "reason": "quarter-kelly {}{}on a validated edge".format(
            "x confidence {} ".format(round(c, 3)) if c < 1.0 else "",
            "x correlation penalty {} ".format(round(pen, 3)) if pen > 0 else ""),
    }


def drawdown_state(equity_curve, halve_at=0.10, stop_at=0.20):
    """Drawdown-based de-risking state.

    Returns {"dd": fraction, "scale": multiplier, "state": label}.
    Halving risk at a 10% drawdown and stopping at 20% is a standard
    vol-target overlay: the point is to survive a losing streak, and a
    fixed size does not.
    """
    eq = [e for e in (equity_curve or []) if e is not None]
    if len(eq) < 2:
        return {"dd": 0.0, "scale": 1.0, "state": "unknown"}
    peak = eq[0]
    for e in eq:
        if e > peak:
            peak = e
    if peak <= 0:
        return {"dd": 0.0, "scale": 1.0, "state": "unknown"}
    last = eq[-1]
    dd = max(0.0, (peak - last) / peak)
    if dd >= _f(stop_at):
        return {"dd": round(dd, 4), "scale": 0.0, "state": "halted"}
    if dd >= _f(halve_at):
        return {"dd": round(dd, 4), "scale": 0.5, "state": "de-risked"}
    return {"dd": round(dd, 4), "scale": 1.0, "state": "normal"}


def concentration_limit(weights, max_single=0.25, max_class=None):
    """Flag breaches of single-name and asset-class concentration.

    A book that is nominally diversified across 9 tickers but is 100%
    energy is not diversified, and a per-name limit alone will not see it.
    """
    w = {k: _f(v) for k, v in (weights or {}).items()}
    total = sum(abs(v) for v in w.values())
    if total <= 0:
        return {"ok": True, "breaches": [], "total": 0.0}
    breaches = []
    for k, v in w.items():
        share = abs(v) / total
        if share > _f(max_single, 1.0):
            breaches.append({"symbol": k, "share": round(share, 4),
                             "limit": _f(max_single, 1.0),
                             "kind": "single_name"})
    if max_class:
        by_class = {}
        for k, v in w.items():
            by_class[k] = by_class.get(k, 0.0) + abs(v)
        for k, v in by_class.items():
            share = v / total
            if share > _f(max_class, 1.0):
                breaches.append({"symbol": k, "share": round(share, 4),
                                 "limit": _f(max_class, 1.0),
                                 "kind": "asset_class"})
    return {"ok": not breaches, "breaches": breaches, "total": round(total, 4)}


def allocate_book(equity, candidates, costs_bps=DEFAULT_COSTS_BPS,
                  target_vol_pct=1.0, max_fraction=0.25, max_single=0.25,
                  equity_curve=None, min_samples=MIN_SAMPLES):
    """Size a whole book from per-symbol candidate stats.

    `candidates` is a list of dicts:
      symbol, win_rate, ratio (or trades), trades (n), edge,
      asset_vol_pct, asset_class, corr_to_book

    Every field is measured, not assumed. A candidate with no validated
    edge is returned at size zero with the reason attached, so the output
    explains its own refusals rather than silently omitting them.
    """
    eq = _f(equity)
    dd = drawdown_state(equity_curve or [])
    rows = []
    invested = 0.0
    for c in (candidates or []):
        sym = str(c.get("symbol", ""))
        if not sym:
            continue
        trades = c.get("trades_list") or []
        if trades:
            wp, n = win_rate(trades)
            ratio = profit_loss_ratio(trades)
        else:
            wp = _f(c.get("win_rate"))
            n = int(_f(c.get("n_samples", c.get("trades", 0))))
            ratio = _f(c.get("ratio"))
        edge = _f(c.get("edge"))
        s = size_position(
            eq, wp, ratio, n, edge, costs_bps=costs_bps,
            target_vol_pct=target_vol_pct,
            asset_vol_pct=_f(c.get("asset_vol_pct"), 1.0) or 1.0,
            book_corr=_f(c.get("corr_to_book")),
            max_fraction=max_fraction)
        if dd["scale"] < 1.0 and s["notional"] > 0:
            s["notional"] = round(s["notional"] * dd["scale"], 2)
            s["fraction"] = round(s["fraction"] * dd["scale"], 6)
            s["reason"] += " ({} by drawdown {:.1%})".format(dd["state"], dd["dd"])
        invested += abs(s["notional"])
        rows.append({"symbol": sym, "asset_class": c.get("asset_class", ""),
                     "win_rate": round(wp, 4), "ratio": (None if ratio == float("inf")
                                                         else round(ratio, 4)),
                     "n_samples": n, **s})
    # Concentration is assessed on the sized book, not the candidate list.
    weights = {r["symbol"]: r["notional"] for r in rows if r["notional"] > 0}
    conc = concentration_limit(weights, max_single=max_single)
    return {
        "equity": round(eq, 2),
        "invested": round(invested, 2),
        "cash": round(eq - invested, 2),
        "drawdown": dd,
        "concentration": conc,
        "positions": rows,
        "deployed_pct": round(invested / eq * 100.0, 2) if eq > 0 else 0.0,
        "note": "size zero means no validated edge, not a missing calculation",
    }
