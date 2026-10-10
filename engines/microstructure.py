"""Order-flow imbalance at the best quotes. Stdlib only, no I/O.

What this is
------------
The single strongest empirical relationship in market microstructure. Over
short intervals the change in mid-price is driven mainly by order flow
imbalance at the best bid and ask, linearly, with a slope inversely
proportional to market depth.

Rama Cont, Arseniy Kukanov and Sasha Stoikov, "The Price Impact of Order Book
Events", Journal of Financial Econometrics 12(1):47-88, 2014 (arXiv:1011.6402).
Reported: beta significant in 98% of samples, average R-squared about 65%,
against about 32% for trade imbalance. Robust to seasonality and stable
across time scales and stocks.

The definition
--------------
For consecutive book states, with r the bid-queue size and q the
ask-queue size:

    dW =  r         if  bid rises
          r - r_prev if  bid unchanged
         -r_prev    if  bid falls

    dV = -q_prev     if  ask rises
          q - q_prev if  ask unchanged
          q         if  ask falls

    e  = dW - dV          OFI = sum of e over the interval

Positive OFI means net buying pressure at the touch: limit buys arriving,
limit sells cancelling, market buys hitting. Negative is the mirror.

Why this is M=1, and why that is not a compromise
--------------------------------------------------
Cont et al. work at the best quotes and, in Appendix B3, extend to the first
five levels on each side. They find the extra levels "only slightly improve"
goodness of fit, and conclude that activity beyond the best quotes "has very
little influence on price changes".

More to the point: their data (TAQ) contains no order-flow events, so
footnote 2 records that they *estimated* OFI by observing changes in volume
at the bid and ask at regularly spaced points in calendar time. That is
exactly what a polled REST snapshot is. Estimating OFI from snapshot depth
is their primary method, not a fallback -- so a single level computed from
venue_quotes is the paper's specification, not a degraded version of it.

The multi-level question is genuinely open rather than settled in either
direction. Ke Xu, Martin Gould and Sam Howison, "Multi-Level Order-Flow
Imbalance in a Limit Order Book" (arXiv:1907.06230, 2019) re-estimate on
full LOBSTER event data and find deeper levels do help, by 65-75%
out-of-sample RMSE on large-tick names, provided Ridge rather than OLS is
used -- OLS over-fits because flow at different levels is strongly
correlated. So depth beyond level 1 is worth having and worth testing
properly; it is simply not available here.

What is NOT claimed
-------------------
That this predicts crypto. Cont et al. studied 50 S&P 500 stocks on the
NYSE. The relationship is a market-structure regularity, not a law, and
crypto books differ (24/7, wider relative spreads, no L2 snapshot access
here). Dobrev, Liu, Kim and Rodriguez, "Order Flow Imbalances and
Amplification of Price Movements" (Federal Reserve FEDS Notes, 2025-11-03)
find the imbalance-amplifies-volatility effect in U.S. Treasuries, again not
crypto. Anyone reading this should treat the strategy as a hypothesis to be
measured and deflated, not as a ported result.
"""
import math


def _f(x, default=0.0):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    return v


def mid(bid, ask):
    return (float(bid) + float(ask)) / 2.0


def event_ofi(prev, cur):
    """The signed contribution e from one book state to the next.

    `prev` and `cur` are dicts with bid/ask/bid_size/ask_size. Returns None
    if either state is unusable -- a missing or crossed book has no
    direction, and guessing one is how a zero becomes a fake buy signal.
    """
    if not prev or not cur:
        return None
    b0, a0 = _f(prev.get("bid")), _f(prev.get("ask"))
    b1, a1 = _f(cur.get("bid")), _f(cur.get("ask"))
    r0, r1 = _f(prev.get("bid_size")), _f(cur.get("bid_size"))
    q0, q1 = _f(prev.get("ask_size")), _f(cur.get("ask_size"))
    if b1 <= 0 or a1 <= 0 or a1 < b1:
        return None
    if b0 <= 0 or a0 <= 0 or a0 < b0:
        return None
    if r0 < 0 or r1 < 0 or q0 < 0 or q1 < 0:
        return None

    if b1 > b0:
        dw = r1
    elif b1 == b0:
        dw = r1 - r0
    else:
        dw = -r0

    if a1 > a0:
        dv = -q0
    elif a1 == a0:
        dv = q1 - q0
    else:
        dv = q1

    return dw - dv


def ofi_series(books):
    """OFI for each consecutive pair in `books` (oldest first).

    Returns a list aligned to books[1:], so entry i is the flow from
    books[i] to books[i+1]. Unusable transitions yield None rather than
    zero: a transition we could not read is missing data, and encoding it
    as "no flow" would bias every aggregate that follows toward zero.
    """
    out = []
    seq = [b for b in (books or []) if b]
    for i in range(1, len(seq)):
        out.append(event_ofi(seq[i - 1], seq[i]))
    return out


def ofi_series_with_prices(books):
    """(ofi, mid_change) pairs -- the regression inputs of eq. (7).

    Cont et al. regress the change in mid-price on contemporaneous OFI. The
    pairing is kept together so a caller cannot accidentally regress on
    index rather than on time.
    """
    seq = [b for b in (books or []) if b]
    out = []
    for i in range(1, len(seq)):
        e = event_ofi(seq[i - 1], seq[i])
        if e is None:
            out.append((None, None))
            continue
        dmid = mid(seq[i]["bid"], seq[i]["ask"]) - mid(seq[i - 1]["bid"],
                                                        seq[i - 1]["ask"])
        out.append((e, dmid))
    return out


def depth(books):
    """Total size resting at the touch. The slope in Cont et al. is inversely
    proportional to depth, so this is the normaliser that makes a raw OFI
    comparable across instruments of different size."""
    seq = [b for b in (books or []) if b]
    if not seq:
        return 0.0
    return _f(seq[-1].get("bid_size")) + _f(seq[-1].get("ask_size"))


def normalize(ofi, book_depth):
    """OFI scaled by resting depth.

    A raw OFI of 5000 is large on a thin book and unremarkable on a deep
    one. Dividing by the bid+ask size at the touch gives the quantity
    Cont et al.'s inverse-depth slope implies, and makes a threshold
    meaningful across venues instead of tuning per instrument.

    Returns None when depth is unusable, because normalising by ~0
    manufactures an enormous number from an unmeasurable book.
    """
    d = _f(book_depth)
    if d <= 0:
        return None
    return _f(ofi) / d


def fit_slope(pairs):
    """OLS of mid-change on OFI, with the intercept.

    Cont et al. found beta significant in 98% of samples but the intercept
    significant in only 10%, so they drop it and use dP ~= beta*OFI. Both are
    returned here so a caller can choose; `use_intercept` defaults False in
    the signal because that is the paper's specification.

    Returns a dict, never raises. n<2 or zero variance in either input
    yields ok=False with the reason -- a slope fitted to a flat book is a
    number with no meaning, and dividing by a zero standard deviation is an
    exception waiting to happen.
    """
    xs, ys = [], []
    for e, dp in (pairs or []):
        if e is None or dp is None:
            continue
        xs.append(_f(e))
        ys.append(_f(dp))
    n = len(xs)
    out = {"ok": False, "n": n, "beta": None, "alpha": None,
           "r2": None, "beta_t": None, "reason": ""}
    if n < 3:
        out["reason"] = "need at least 3 readable transitions, got {}".format(n)
        return out
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0:
        out["reason"] = "OFI never moved; no slope is defined"
        return out
    beta = sxy / sxx
    alpha = my - beta * mx
    syy = sum((y - my) ** 2 for y in ys)
    ssr = sum((y - (alpha + beta * x)) ** 2 for x, y in zip(xs, ys))
    r2 = (1.0 - ssr / syy) if syy > 0 else None

    se = None
    if n > 2:
        sigma2 = ssr / (n - 2)
        se = math.sqrt(sigma2 / sxx) if sxx > 0 else None
    t = (beta / se) if (se and se > 0) else None

    out.update({"ok": True, "beta": beta, "alpha": alpha, "r2": r2,
                "beta_t": t, "reason": "fitted on {} transitions".format(n)})
    return out


def ofi_state(books, lookback=60, min_transitions=20):
    """Everything a scalping signal needs, with an explicit depth verdict.

    Returns depth, recent OFI, its depth-normalised value, the fitted slope,
    and -- importantly -- whether there is enough history to mean anything.
    The last field exists because this deployment stores a polled top-of-book
    snapshot, not order events, so a "no history" answer is a normal outcome
    and must be reportable rather than papered over with a default.
    """
    seq = [b for b in (books or []) if b]
    out = {"ok": False, "n": len(seq), "depth": 0.0, "ofi": None,
           "ofi_norm": None, "mid": None, "slope": None, "fit": None,
           "sufficient_history": False, "reason": ""}
    if len(seq) < 2:
        out["reason"] = "need at least 2 book states, got {}".format(len(seq))
        return out

    pairs = ofi_series_with_prices(seq)
    readable = [p for p in pairs if p[0] is not None]
    out["fit"] = fit_slope(pairs)
    out["depth"] = depth(seq)
    out["mid"] = mid(seq[-1]["bid"], seq[-1]["ask"])
    if not readable:
        out["reason"] = "no readable book transitions (crossed or missing sizes)"
        return out

    lb = max(2, int(lookback or 60))
    window = readable[-lb:]
    recent = sum(e for e, _dp in window)
    out["ofi"] = recent
    out["ofi_norm"] = normalize(recent, out["depth"])
    out["transitions_used"] = len(window)
    out["sufficient_history"] = len(window) >= int(min_transitions)
    if not out["sufficient_history"]:
        out["reason"] = ("only {} readable transitions, need {} -- an OFI "
                         "over this few points is a fit to noise"
                         .format(len(window), int(min_transitions)))
        return out
    out["ok"] = True
    out["reason"] = "{} transitions over {} book states".format(
        len(window), len(seq))
    return out