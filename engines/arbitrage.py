"""Cross-instrument arbitrage engine. Stdlib only, import-safe, no DB.

Four independent models, chosen because arbitrage is not one thing:

1. `cross_venue`  -- crypto only. Buy the same asset where it is cheapest,
   sell it where it is dearest. Needs two books for one symbol.
2. `triangular`   -- crypto only. A closed three-leg cycle that returns to
   the starting asset. Needs three pairs, one venue or several.
3. `fx_cross`     -- FX only. The synthetic cross rate through a pivot
   versus the direct rate. EURUSD should equal EURGBP x GBPUSD.
4. `spread`       -- commodity only. Ratio/relationship dislocation between
   two correlated contracts (BRENT-WTI crack, gold/silver).

Every function is pure: dicts/lists in, dict out, no I/O, no raises on bad
input. Each returns `edge_bps=None` when it cannot price an edge, and the
caller renders "insufficient data" rather than a number.

The fee rule, applied everywhere
--------------------------------
Gross spread is not profit. Every edge is *net of both legs' taker
fees*, taken from `data.venuebooks.fee_bps` (round-trip per venue). A
venue cost is charged twice because an arbitrage trades twice. Any
comparison that forgot this would report a 3-bps cross-venue gap on
crypto as a 3-bps profit, when the two legs cost ~14 bps and the trade is
a guaranteed loss.

The synthetic rule, applied everywhere
--------------------------------------
A leg priced off a generated tick is not executable. Any model touching
a synthetic quote returns `edge_bps=None` and says why, rather than
printing a number that looks like an opportunity. This is the same
reasoning that put `source` on quotes_l1 in the first place.
"""

import math

try:
    from data.venuebooks import fee_bps as _venue_fee_bps
    from data.venuebooks import is_synthetic as _is_synthetic
except ImportError:  # pragma: no cover - alternate sys.path
    try:
        from venuebooks import fee_bps as _venue_fee_bps
        from venuebooks import is_synthetic as _is_synthetic
    except ImportError:
        def _venue_fee_bps(venue):
            return 50.0

        def _is_synthetic(venue):
            return True


def _f(x, default=0.0):
    """Float or default. Rejects NaN/inf, which poison every sum below."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return v


def _positive(x):
    v = _f(x)
    return v if v > 0 else 0.0


# -- book sanity ---------------------------------------------------------
#
# A book wider than this is not a two-sided market anybody can cross, and
# pricing an edge against it invents one. This is not hypothetical: kraken
# served USDJPY at a 155.8 bps spread with its ask 1.68% above the last
# trade -- the ask side simply had nothing near the market -- while every
# other FX pair on the same venue came in under 2 bps. EURJPY had the same
# problem mirrored: 90.4 bps, with the bid ~1.3% under the last trade.
#
# The threshold is not a guess. Measured across kraken's book, healthy pairs
# run 0.09-1.72 bps and the broken ones run 90+ bps, so the gap between them
# is a regime nobody has observed here. It matches `engines/signals.py`'s
# `max_spread_bps` default, which the signal layer has been refusing on for
# the same reason -- the arbitrage scanner was the one place that did not.
MAX_BOOK_SPREAD_BPS = 50.0


def book_spread_bps(book):
    """Width of ONE book: (ask - bid) / mid. None when unpriceable.

    Deliberately the intra-book width, not a cross-venue gap. A wide spread
    *between* two venues can be a genuine dislocation and is exactly what
    cross_venue exists to find; a wide spread *within* one venue's book
    means that venue is not quoting a market you can trade against.
    """
    bid, ask = _positive((book or {}).get("bid")), _positive((book or {}).get("ask"))
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    if mid <= 0:
        return None
    return (ask - bid) / mid * 10000.0


def _wide_book_names(legs, limit=None):
    """[(label, spread_bps)] for legs whose own book is too wide to price."""
    cap = MAX_BOOK_SPREAD_BPS if limit is None else _f(limit, MAX_BOOK_SPREAD_BPS)
    out = []
    for leg in legs or []:
        leg = leg or {}
        sp = book_spread_bps(leg)
        if sp is not None and sp > cap:
            out.append(("{}@{}".format(leg.get("symbol", "?"),
                                       leg.get("venue", "") or "?"), sp))
    return out


def _synthetic_reasons(legs):
    """Names of legs whose venue book is synthetic (or missing)."""
    bad = []
    for leg in legs or []:
        venue = (leg or {}).get("venue", "")
        if _is_synthetic(venue):
            bad.append("{}@{}".format((leg or {}).get("symbol", "?"), venue or "?"))
    return bad


# ------------------------------------------------------------------ 1. cross-venue
def cross_venue(symbol, books, size=None):
    """Cheapest-ask vs dearest-bid for ONE symbol across venues.

    Args:
      symbol: str
      books:  {venue: {"bid":.., "ask":.., "bid_size":.., "ask_size":..}}
              More than two venues is fine -- the best pair is chosen.
      size:   optional notional cap in quote currency; if omitted the
              opportunity is capped by the thinner of the two books.

    Returns a dict describing the best pair, or one with `edge_bps=None`
    and a `reason` when it cannot be priced.
    """
    out = {"model": "cross_venue", "symbol": str(symbol or ""),
           "ok": False, "edge_bps": None, "reason": "",
           "buy_venue": "", "sell_venue": "",
           "buy_ask": None, "sell_bid": None,
           "max_size": None, "notional": None,
           "gross_bps": None, "cost_bps": None}

    usable = {}
    for venue, book in (books or {}).items():
        bid, ask = _positive((book or {}).get("bid")), _positive((book or {}).get("ask"))
        if bid > 0 and ask > 0 and ask >= bid:
            usable[venue] = dict(book or {}, bid=bid, ask=ask)
    if len(usable) < 2:
        out["reason"] = ("need 2+ live venues for this symbol, have {}"
                         .format(len(usable)))
        return out

    # Each venue's *own* book must be crossable. A venue quoting a 2% band
    # is not offering a price you can lift; treating its far touch as a
    # liftable ask manufactures a gap between it and every healthy venue.
    # Checked on all venues, not just the two the comparison picks, so the
    # refusal names the real culprit instead of blaming whichever pair the
    # min/max happened to select.
    wide = _wide_book_names(
        [dict(b, symbol=symbol, venue=v) for v, b in usable.items()])
    if wide:
        out["reason"] = ("book too wide to price against (max {} bps): {}"
                         .format(MAX_BOOK_SPREAD_BPS,
                                 ", ".join("{} {:.1f} bps".format(n, s)
                                           for n, s in wide)))
        out["wide_books"] = [{"leg": n, "spread_bps": round(s, 2)}
                             for n, s in wide]
        return out

    # Best ask to lift, best bid to hit -- chosen independently, then
    # checked for the one thing that invalidates the idea: the same
    # venue on both sides is not an arbitrage, it is paying the spread.
    buy_venue = min(usable, key=lambda v: usable[v]["ask"])
    sell_venue = max(usable, key=lambda v: usable[v]["bid"])
    if buy_venue == sell_venue:
        out["reason"] = "only one venue is live; nothing to compare against"
        return out

    buy_ask = usable[buy_venue]["ask"]
    sell_bid = usable[sell_venue]["bid"]
    if sell_bid <= buy_ask:
        out["reason"] = ("no dislocation: best bid {} <= best ask {}"
                         .format(round(sell_bid, 6), round(buy_ask, 6)))
        return out

    gross = (sell_bid - buy_ask) / buy_ask * 10000.0
    cost = _venue_fee_bps(buy_venue) + _venue_fee_bps(sell_venue)
    edge = gross - cost

    # -- persistence: how old are the books that produced this gap?
    #
    # Reported separately from the edge and never merged into it. A gap
    # built on stale quotes is not executable, so it still gets
    # edge_bps=None below -- but "this dislocation has survived N seconds
    # across venues nobody trusts" is exactly the risk-premium signal the
    # exchange-default literature describes, and it used to be discarded.
    ages = [_f((usable[v] or {}).get("age_s"), 0.0) for v in (buy_venue,
                                                             sell_venue)]
    staleness = [bool((usable[v] or {}).get("stale")) for v in (buy_venue,
                                                               sell_venue)]
    age_s = max(ages or [0.0])
    out["quote_age_s"] = round(age_s, 1) if ages else None
    # A gap measured on 4-day-old quotes is a dead feed, not a risk
    # premium. Claiming "persistent dislocation" for a stale feed would be
    # the same error in the opposite direction -- dressing up no data as a
    # signal. The persistence claim is bounded to a window where the books
    # are recent enough for the gap to mean anything.
    out["feed_dead"] = age_s > PERSISTENCE_MAX_AGE_S
    out["persistent_gap"] = (
        (age_s > STALE_PERSISTENCE_S) and not out["feed_dead"])
    out["observed_gap_bps"] = round(gross, 2)

    # Size is limited by what each book actually shows. Reporting a
    # notional the venue does not have is how a scanner reports a
    # 500k trade on a book holding 0.4.
    ask_depth = _positive(usable[buy_venue].get("ask_size"))
    bid_depth = _positive(usable[sell_venue].get("bid_size"))
    cap = 0.0
    if ask_depth > 0:
        cap = ask_depth
    if bid_depth > 0:
        cap = bid_depth if cap <= 0 else min(cap, bid_depth)
    notional = cap * buy_ask if (cap > 0 and buy_ask > 0) else None
    if size:
        try:
            want = _f(size)
        except Exception:
            want = 0.0
        if want > 0:
            notional = want if not notional else min(notional, want)

    if out["persistent_gap"]:
        # Deliberately not `ok`: a gap measured on books this old cannot be
        # executed, and calling it ok is how a scanner talks someone into a
        # trade that cannot be placed. The numbers stay visible.
        out["ok"] = False
        out["edge_bps"] = None
        out["reason"] = (
            "gap of {:.1f} bps has persisted {:.0f}s -- a risk premium on the "
            "distrusted venue, not an executable edge (costs {:.1f} bps)"
            .format(gross, out["quote_age_s"] or 0.0, cost))
        out["buy_venue"] = buy_venue
        out["sell_venue"] = sell_venue
        out["buy_ask"] = round(buy_ask, 8)
        out["sell_bid"] = round(sell_bid, 8)
        out["gross_bps"] = round(gross, 2)
        out["cost_bps"] = round(cost, 2)
        out["max_size"] = None
        out["notional"] = None
        return out

    out.update({"ok": True,
                "buy_venue": buy_venue, "sell_venue": sell_venue,
                "buy_ask": round(buy_ask, 8), "sell_bid": round(sell_bid, 8),
                "gross_bps": round(gross, 2),
                "cost_bps": round(cost, 2),
                "edge_bps": round(edge, 2),
                "max_size": round(cap, 8) if cap > 0 else None,
                "notional": round(notional, 2) if notional else None,
                "reason": ("profitable after fees" if edge > 0
                           else "gross {} bps does not cover {} bps of fees"
                                .format(round(gross, 2), round(cost, 2)))})
    return out


# ------------------------------------------------------------------ 1b. attribution
def venue_attribution(symbol, books, size=None):
    """Leave-one-venue-out test on a cross-venue gap.

    A cross-venue gap is a statement about *two* venues, which means it can
    be an artefact of one of them. The most common shape is a venue whose
    fiat gateway is impaired: its book is not really comparable with anyone
    else's, because the claim behind it is not the same claim. Anything
    measured against it is measuring the gateway, not the market.

    Flowerday, Gandal, Halaburda, Olson & Ardel, "Bitcoin arbitrage: The
    role of an exchange" (Quantitative Finance and Economics 10(3):624-648,
    2026) find that ~80% of hours with measured cross-exchange dispersion
    include Bitfinex, and that excluding it drops the correlation between
    mispricing and volatility from 0.86 to 0.59 and the restricted mean
    duration of a positive episode from 11.1 to 3.6 minutes. One venue was
    carrying most of the apparent signal.

    So this recomputes the gap with each venue removed in turn and reports
    what survives. A gap that collapses when any single venue is dropped is
    not a market dislocation and should never have been presented as one.

    Requires three or more venues. With two, removing either leaves nothing
    to compare, and a "robust" verdict would be vacuous -- so it reports
    `testable=False` rather than manufacturing confidence from a test that
    cannot run.
    """
    out = {"symbol": str(symbol or ""), "testable": False,
           "venues": [], "robust": None, "driver": None,
           "survives_every_dropout": None,
           "base_gross_bps": None, "reason": ""}

    usable = {}
    for venue, book in (books or {}).items():
        bid, ask = _positive((book or {}).get("bid")), _positive((book or {}).get("ask"))
        if bid > 0 and ask > 0 and ask >= bid:
            usable[venue] = dict(book or {}, bid=bid, ask=ask)

    out["venue_count"] = len(usable)
    if len(usable) < 3:
        out["reason"] = ("needs 3+ venues to test attribution; {} available"
                         .format(len(usable)))
        return out

    out["testable"] = True
    base = cross_venue(symbol, usable, size=size)
    base_gross = _f(base.get("gross_bps"), 0.0)
    out["base_gross_bps"] = base_gross if base_gross else None
    out["base_edge_bps"] = base.get("edge_bps")

    # Nothing to attribute when there is no dislocation to attribute.
    # Without this the loop below reports `robust=True` on a set of venues
    # that all agree -- "the gap survives every dropout" when there was
    # never a gap. A robustness verdict about a non-existent effect is not
    # a weak result, it is a fabricated one.
    if base_gross <= 0:
        out["reason"] = ("no dislocation to attribute ({})"
                         .format(base.get("reason") or "best bid <= best ask"))
        return out

    # Each dropout is scored on the gross dislocation, not the net edge.
    # Net edge is already negative on costs in almost every case, so
    # comparing net edges would show every dropout as "no change" and hide
    # exactly the collapse we are looking for.
    survivors = []
    for venue in sorted(usable):
        reduced = {v: b for v, b in usable.items() if v != venue}
        r = cross_venue(symbol, reduced, size=size)
        gross = _f(r.get("gross_bps"), 0.0)
        ratio = (gross / base_gross) if base_gross > 0 else None
        row = {
            "dropped": venue,
            "gross_bps": r.get("gross_bps"),
            "edge_bps": r.get("edge_bps"),
            "retained_fraction": round(ratio, 4) if ratio is not None else None,
            "buy_venue": r.get("buy_venue"),
            "sell_venue": r.get("sell_venue"),
            "priced": r.get("gross_bps") is not None,
            # A gap that disappears when this venue leaves was that venue.
            "collapses": bool(base_gross > 0 and gross < base_gross * 0.5),
        }
        out["venues"].append(row)
        survivors.append(not row["collapses"])

    out["survives_every_dropout"] = all(survivors) if survivors else None
    collapsed = [r["dropped"] for r in out["venues"] if r["collapses"]]

    if not collapsed:
        out["robust"] = True
        out["driver"] = None
        out["driver_is_venue"] = False
        out["reason"] = ("gap survives removing any single venue ({} tested)"
                         .format(len(out["venues"])))
        return out

    # More than one collapse means no single venue is "the driver" -- the
    # gap lives in the spread of the book, not in any one member of it. A
    # pairwise gap has only two venues; when you add a third you can find
    # the middle of the distribution, and if that middle is what survives,
    # the extreme venue is an outlier rather than the market's price.
    if len(collapsed) == len(out["venues"]):
        out["robust"] = False
        out["driver"] = None
        out["driver_is_venue"] = False
        out["reason"] = ("gap rests on whichever two venues are furthest "
                         "apart -- every single-venue drop collapses it")
    elif len(collapsed) == 1:
        out["robust"] = False
        out["driver"] = collapsed[0]
        out["driver_is_venue"] = True
        out["reason"] = ("gap collapses without {} -- this is one venue's "
                         "book, not a market dislocation".format(collapsed[0]))
    else:
        out["robust"] = False
        out["driver"] = None
        out["driver_is_venue"] = False
        out["reason"] = (
            "gap narrows by more than half without {} -- no single venue "
            "carries it, so it is dispersion within the book"
            .format(" or ".join(collapsed)))
    return out


# ------------------------------------------------------------------ 2. triangular
def triangular(symbols, books, fee_multiplier=1.0, venue=None):
    """Closed three-leg crypto cycle: A -> B -> C -> A.

    Args:
      symbols: (a, b, c) the app's symbol names.
      books:   {symbol: {"bid":.., "ask":..}} for each, optionally nested
               as {symbol: {venue: book}} when the legs sit on different
               venues. A venue is optional; leg pricing uses the best
               available ask in / best available bid out.
      fee_multiplier: scales the fee estimate (a stress knob).
      venue:   the venue a *bare* book dict belongs to. Required for bare
               books to be priced, because a book with no venue label
               cannot be costed: an unnamed book is charged the default
               taker rate and is explicitly NOT treated as synthetic.
               Nested books carry their own venue and ignore this.

    The multiplier is a ratio of *execution prices*: starting with 1 unit
    of A, each leg converts at that leg's rate, so the final amount of A
    is the profit factor. > 1.0 is an arbitrage, < 1.0 is a loss.

    The subtlety that makes or breaks this: a cycle that starts and ends
    on the *same venue* with three crossing spreads cannot profit, and a
    naive ratio will happily report one because it ignores that each hop
    pays a spread. Multiplying the actual leg rates -- not mid prices --
    is what keeps the number honest.
    """
    out = {"model": "triangular", "ok": False, "edge_bps": None, "reason": "",
           "legs": [], "profit_factor": None,
           "cost_bps": None, "symbols": list(symbols or [])}

    syms = [str(s or "").strip().upper() for s in (symbols or [])]
    if len(syms) != 3 or len(set(syms)) != 3:
        out["reason"] = "triangular needs 3 distinct symbols"
        return out

    books = books or {}
    bad = []
    for s in syms:
        node = books.get(s)
        if node is None:
            bad.append("{}: no book".format(s))
    if bad:
        out["reason"] = "; ".join(bad)
        return out

    def _side(node, side):
        """(price, venue) for the best ask-in (buy) or best bid-out (sell).

        A node is either {venue: book} or a bare book dict. Bare is
        detected by the presence of bid/ask keys, which a venue-keyed
        dict never has as literal keys. A bare book falls back to the
        caller's `venue` hint so it can be costed.
        """
        if "bid" in node or "ask" in node:
            px = _positive(node.get(side))
            if px <= 0:
                return 0.0, ""
            return px, str(node.get("venue") or venue or "")
        best, best_venue = 0.0, ""
        for ven, book in node.items():
            px = _positive((book or {}).get(side))
            if px > best:
                best, best_venue = px, ven
        return best, best_venue

    # Build the cycle in base/quote terms: for each consecutive pair
    # (x, y), buying x/y with y and selling it for y.
    # A real triangular arb needs the *inverse* of one leg; taking the
    # cycle as ratios of mid prices and then checking the sign of the
    # product against 1.0 is what the profit factor expresses.
    amount = 1.0
    legs = []
    cost = 0.0
    for i in range(3):
        a, b = syms[i], syms[(i + 1) % 3]
        ask, av = _side(books[a], "ask")
        bid_b, bv = _side(books[b], "bid")
        if ask <= 0 or bid_b <= 0:
            out["reason"] = "leg {}/{} has no usable book".format(a, b)
            out["legs"] = legs
            return out
        venue = av or bv or ""
        if _is_synthetic(venue):
            bad.append("{}@{}".format(a, venue or "?"))
        rate = bid_b / ask if ask > 0 else 0.0
        amount *= rate
        legs.append({"from": a, "to": b, "venue": venue,
                     "ask": round(ask, 8), "bid": round(bid_b, 8),
                     "rate": round(rate, 10)})
        cost += _venue_fee_bps(venue) * float(fee_multiplier or 1.0)

    if bad:
        out["reason"] = "synthetic leg(s): " + ", ".join(bad)
        out["legs"] = legs
        return out

    pf = amount
    edge = (pf - 1.0) * 10000.0
    edge -= cost
    out.update({"ok": True, "legs": legs,
                "profit_factor": round(pf, 10),
                "cost_bps": round(cost, 2),
                "edge_bps": round(edge, 2),
                "reason": ("cycle closes profitable" if edge > 0
                           else "cycle pays {} bps of fees".format(round(cost, 2)))})
    return out


# ------------------------------------------------------------------ 3. fx cross
def _compose_op(direct, l1, l2):
    """How leg1 and leg2 combine to yield `direct`: "mul", "div", or None.

    Derived from currency codes, never hand-declared. An earlier version
    multiplied unconditionally and shipped triangles such as
    `EURGBP = EURJPY x USDJPY`, which is dimensionally meaningless -- on
    live data it reported +324,420,004 bps. Code review missed it because
    every symbol looked plausible in isolation; only the arithmetic of the
    currencies exposes it, so that arithmetic is what decides now.

    A pair quotes units of quote per 1 unit of base. With A/B and B/C the
    product is A/C (multiply). With A/B and A/C -- two quotes of the same
    base -- B/C is their quotient (divide).
    """
    def split(sym):
        try:
            from data.fxmarkets import split_pair as _sp
        except ImportError:  # pragma: no cover - alternate sys.path
            try:
                from fxmarkets import split_pair as _sp
            except ImportError:
                text = str(sym or "").strip().upper()
                return (text[:3], text[3:]) if len(text) == 6 and text.isalpha() \
                    else (None, None)
        return _sp(sym)

    d_b, d_q = split(direct)
    a_b, a_q = split(l1)
    c_b, c_q = split(l2)
    if not all((d_b, d_q, a_b, a_q, c_b, c_q)):
        return None
    # (A/B) x (B/C) = A/C
    if a_q == c_b and a_b == d_b and c_q == d_q:
        return "mul"
    # (A/B) / (A/C) = C/B   -- the two legs share a BASE (USDCHF, USDJPY)
    if a_b == c_b and a_q == d_q and c_q == d_b:
        return "div"
    # (A/B) / (C/B) = A/C   -- the two legs share a QUOTE (EURJPY, USDJPY)
    if a_q == c_q and a_b == d_b and c_b == d_q:
        return "div"
    return None


def fx_cross(direct_symbol, direct, legs):
    """Synthetic cross rate through a pivot vs the traded direct rate.

    Args:
      direct_symbol: str, e.g. "EURUSD"
      direct:        {"bid":.., "ask":.., "venue":..}
      legs:          [{"symbol":"EURGBP","bid":..,"ask":..,"venue":..},
                      {"symbol":"GBPUSD","bid":..,"ask":..,"venue":..}]

    The tradeable quantity is the cost of each direction, and which side of
    each book you cross depends on the operation -- this is the part that is
    easy to get wrong in either direction.

    For a `div` cross (EURUSD = EURJPY / USDJPY) the legs trade *opposite*
    ways. Going long buys EURJPY (pay its ask) and sells USDJPY (hit its
    bid), so the long price is ask1/bid2. Combining both legs on the same
    side -- ask1/ask2 and bid1/bid2 -- prices a book that cannot be traded,
    because it assumes liquidity on both sides of two books at once. On
    live kraken books that reported +82.7 bps gross on EURUSD while both
    real directions lost ~134 bps; the mids agreed to 0.6 bps, so there was
    no dislocation at all.

    For a `mul` cross (EURUSD = EURGBP x GBPUSD) the legs trade the *same*
    way, because buying one leg supplies what the other needs: buying
    EURGBP buys EUR and sells GBP, buying GBPUSD buys that GBP back and
    sells USD. So the long price is ask1*ask2 and the short is bid1*bid2.
    Applying the div rule here instead reintroduces the same phantom one
    operation over.

    Both legs are priced at their own side. Using a mid for one leg and an
    ask for the other is the standard way cross-rate scanners invent free
    money out of nothing.

    Legs are combined by the operation their currencies imply rather than
    by assuming multiplication. A pair set that does not actually compose
    into the direct is refused, not priced.
    """
    out = {"model": "fx_cross", "ok": False, "edge_bps": None, "reason": "",
           "direct_symbol": str(direct_symbol or ""),
           "cross_symbol": "", "direct_mid": None,
           "cross_buy": None, "cross_sell": None,
           "direction": "", "cost_bps": None, "legs": []}

    d = direct or {}
    d_bid, d_ask = _positive(d.get("bid")), _positive(d.get("ask"))
    if d_bid <= 0 or d_ask <= 0:
        out["reason"] = "no direct book for {}".format(direct_symbol)
        return out
    leg_list = list(legs or [])
    if len(leg_list) != 2:
        out["reason"] = "fx cross needs exactly 2 legs"
        return out

    op = _compose_op(direct_symbol, leg_list[0].get("symbol"),
                     leg_list[1].get("symbol"))
    if op is None:
        out["reason"] = ("{} does not compose from {} and {}"
                         .format(direct_symbol, leg_list[0].get("symbol"),
                                 leg_list[1].get("symbol")))
        return out
    out["op"] = op

    # A leg whose own book is implausibly wide is not a market this edge can
    # be crossed against. Checked before any pricing, and the direct counts
    # as a book too -- a wide leg beside a tight direct is exactly how a
    # scanner reports an opportunity that exists only in the wide leg.
    #
    # `dict(d, symbol=...)` rather than a bare label dict: the direct's own
    # bid/ask must come along, or it is measured as having no book at all
    # and silently skipped -- which is exactly what happened on the first
    # version of this guard, caught by test_a_wide_direct_is_refused_too.
    named = [dict(d or {}, symbol=direct_symbol)] + list(leg_list)
    wide = _wide_book_names(named)
    if wide:
        out["reason"] = ("book too wide to price against (max {} bps): {}"
                         .format(MAX_BOOK_SPREAD_BPS,
                                 ", ".join("{} {:.1f} bps".format(n, s)
                                           for n, s in wide)))
        out["wide_books"] = [{"leg": n, "spread_bps": round(s, 2)}
                             for n, s in wide]
        return out

    venues = []
    for leg in leg_list:
        lb, la = _positive(leg.get("bid")), _positive(leg.get("ask"))
        sym = str(leg.get("symbol", "?"))
        if lb <= 0 or la <= 0:
            out["reason"] = "leg {} has no usable book".format(sym)
            return out
        venues.append(leg.get("venue", ""))
        out["legs"].append({"symbol": sym, "venue": leg.get("venue", ""),
                            "bid": round(lb, 10), "ask": round(la, 10)})

    def _combine(side1, side2):
        """Leg prices for a cross, each leg on its own side.

        `side1` applies to leg 1 and `side2` to leg 2. Which sides those
        are depends on the operation, and getting it wrong prices a book
        that cannot be traded -- see fx_cross's docstring.
        """
        a, b = leg_list[0][side1], leg_list[1][side2]
        return a * b if op == "mul" else a / b

    # The side pairing is NOT the same for both operations, which is the
    # whole subtlety here.
    #
    # `mul`, e.g. EURUSD = EURGBP x GBPUSD. Going long buys EUR and sells
    # USD; buying EURGBP does both halves at once (ask1: buy EUR, sell GBP),
    # and buying GBPUSD supplies the USD (ask2: buy GBP, sell USD). Both
    # legs are bought, so both are priced at the ask.
    #
    # `div`, e.g. EURUSD = EURJPY / USDJPY. Going long buys EURJPY (ask1:
    # buy EUR) but must *sell* USDJPY to shed the USD (bid2). Opposite sides.
    #
    # Applying "opposite sides" everywhere -- or "same side" everywhere --
    # prices one of the two operations on a position nobody can take. The
    # first attempt at this fix used one rule for both and reintroduced the
    # phantom edge on the mul crosses, which is how it was caught: the live
    # scan printed a positive edge on EURJPY straight after it went away
    # from EURUSD.
    if op == "mul":
        cross_buy = _combine("ask", "ask")     # long: buy both legs
        cross_sell = _combine("bid", "bid")     # short: sell both legs
    else:
        cross_buy = _combine("ask", "bid")     # long: buy leg1, sell leg2
        cross_sell = _combine("bid", "ask")    # short: sell leg1, buy leg2
    if cross_buy <= 0 or cross_sell <= 0:
        out["reason"] = "non-positive cross from {} legs".format(op)
        return out

    # The direct book counts as a venue too. Checking only the legs let
    # a synthetic direct price against real legs price an edge against a
    # number that does not exist.
    synth = [v for v in venues + [d.get("venue", "")] if _is_synthetic(v)]
    if synth:
        out["reason"] = "synthetic leg(s): " + ", ".join(synth)
        out["cross_symbol"] = "{} {} {}".format(
            leg_list[0].get("symbol"), "x" if op == "mul" else "/",
            leg_list[1].get("symbol"))
        return out

    direct_mid = (d_bid + d_ask) / 2.0
    # Three crossings: two legs to build the cross, one to unwind the
    # direct. Every venue here publishes a real two-sided book, so the
    # taker fee is the honest cost -- there is no reference-only fudge.
    cost = _venue_fee_bps(d.get("venue", ""))
    for v in venues:
        cost += _venue_fee_bps(v)

    # Direction A: direct is dear, synthetic is cheap -> buy cross, sell direct.
    edge_a = (d_bid - cross_buy) / cross_buy * 10000.0 if cross_buy > 0 else 0.0
    # Direction B: direct is cheap, synthetic is dear -> buy direct, sell cross.
    edge_b = (cross_sell - d_ask) / d_ask * 10000.0 if d_ask > 0 else 0.0

    if edge_a >= edge_b:
        direction, gross = "buy_cross_sell_direct", edge_a
    else:
        direction, gross = "buy_direct_sell_cross", edge_b
    edge = gross - cost

    out.update({"ok": True,
                "cross_symbol": "{} {} {}".format(
                    leg_list[0].get("symbol"), "x" if op == "mul" else "/",
                    leg_list[1].get("symbol")),
                "direct_mid": round(direct_mid, 10),
                "cross_buy": round(cross_buy, 10),
                "cross_sell": round(cross_sell, 10),
                "gross_bps": round(gross, 2),
                "cost_bps": round(cost, 2),
                "direction": direction,
                "edge_bps": round(edge, 2),
                "reason": ("cross is dislocated" if edge > 0
                           else "cross tracks direct within fees")})
    return out


# ------------------------------------------------------------------ 4. commodity spread
# ------------------------------------------------------- mean-reversion model
#
# "Edge bps" answers "how wide is this right now". That is not the same
# question as "is this worth trading", and the mean-reversion futures
# literature is blunt about the difference: a spread can be wide, mean
# reverting, and still lose money after costs and stops.
#
# So the spread model also estimates expectancy, which needs three things
# edge alone does not have: how fast the spread reverts, where the stop
# goes, and what it costs to get in and out.
#
# Half-life comes from an AR(1) fit on log-ratio returns, the standard
# estimator. Where it cannot be estimated -- too little history, or a
# series that trends rather than reverts -- expectancy is None. It is
# never a default, and never optimistic.


def _ar1_rho(series):
    """AR(1) coefficient of a stationary-looking series. None if unusable.

    Rejects a series whose variation is negligible relative to its own
    magnitude. Without that guard, a perfectly linear drift -- whose
    first differences are all equal to within floating-point noise -- came
    back with a *negative* autocorrelation and a confident 2.9-bar
    half-life. A deterministic trend was being reported as mean-reverting,
    which is the one thing this estimate must never do.
    """
    n = len(series)
    if n < 30:
        return None
    mean = sum(series) / n
    var = sum((x - mean) ** 2 for x in series) / n
    if var <= 0:
        return None
    # Coefficient of variation of the level: if the series barely moves
    # relative to where it sits, its differences are drift, not noise.
    scale = abs(mean) + var ** 0.5
    if var ** 0.5 <= 1e-6 * scale:
        return None
    cov = sum((series[i] - mean) * (series[i - 1] - mean)
              for i in range(1, n)) / (n - 1)
    rho = cov / var
    # An AR(1) fit to a short or noisy series can leave the unit circle,
    # which would make the half-life logarithm blow up or go negative.
    if not (-0.99 <= rho < 1.0):
        return None
    return rho


def _half_life_from_rho(rho):
    """Half-life in bars from an AR(1) coefficient. None if not reverting."""
    if rho is None:
        return None
    # rho >= 0 means the series does not mean-revert: no decay to trade.
    if rho >= -0.02:
        return None
    denom = _f(rho, 0.0)
    hl = -math.log(2.0) / math.log(1.0 + denom)
    return hl if hl > 0 else None


def _ou_hit_probability(z, stop_z, half_life):
    """P(mean reverts to 0 before hitting `stop_z`) starting at `z`.

    Standard Ornstein-Uhlenbeck first-passage result, with theta derived
    from the half-life. z and stop_z are in units of the series' own
    standard deviation, so the answer is scale-free.
    """
    if half_life is None or half_life <= 0:
        return None
    theta = math.log(2.0) / half_life
    # Avoid overflow for long half-lives by working with the differences.
    a, b = 0.0, float(stop_z)
    if b == a or theta <= 0:
        return None
    try:
        num = math.exp(2.0 * theta * (b - z)) - 1.0
        den = math.exp(2.0 * theta * (b - a)) - 1.0
        if den == 0:
            return None
        p = num / den
    except OverflowError:
        return None
    return max(0.0, min(1.0, p))


def spread(a_symbol, a_quote, b_symbol, b_quote, history=None,
           label="", kind="ratio"):
    """Relationship dislocation between two correlated contracts.

    Args:
      a_symbol/a_quote, b_symbol/b_quote: live books for the two legs.
      history:  optional {symbol: [closes...]} used to turn the current
                ratio into a z-score against its own distribution.
      kind:     "ratio" (default, e.g. gold/silver) or "difference"
                (e.g. the Brent-WTI crack, in dollars).

    A commodity spread is not a guaranteed profit -- unlike the crypto
    and FX models above, two contracts can be genuinely mispriced for
    weeks. So this model reports the dislocation *and* its z-score when
    history is available, and never claims an edge is riskless. The
    z-score is what makes the number actionable: a 2-sigma crack is a
    regime, a 0.3-sigma one is noise.
    """
    out = {"model": "spread", "ok": False, "edge_bps": None, "reason": "",
           "a_symbol": str(a_symbol or ""), "b_symbol": str(b_symbol or ""),
           "label": label or "{} / {}".format(a_symbol, b_symbol),
           "kind": kind, "value": None, "z_score": None,
           "gross_bps": None, "cost_bps": None}

    a, b = a_quote or {}, b_quote or {}
    a_bid, a_ask = _positive(a.get("bid")), _positive(a.get("ask"))
    b_bid, b_ask = _positive(b.get("bid")), _positive(b.get("ask"))
    if a_bid <= 0 or a_ask <= 0 or b_bid <= 0 or b_ask <= 0:
        out["reason"] = "both legs need a live two-sided book"
        return out
    for leg_name, venue in ((str(a_symbol), a.get("venue", "")),
                            (str(b_symbol), b.get("venue", ""))):
        if _is_synthetic(venue):
            out["reason"] = "synthetic leg: {}@{}".format(leg_name, venue or "?")
            return out

    a_mid, b_mid = (a_bid + a_ask) / 2.0, (b_bid + b_ask) / 2.0
    if kind == "difference":
        # Crack-style: what it costs to own the spread, worst case.
        value_sell = a_bid - b_ask     # long A, short B
        value_buy = a_ask - b_bid      # short A, long B
        unit = "abs"
    else:
        value_sell = a_bid / b_ask if b_ask > 0 else 0.0
        value_buy = a_ask / b_bid if b_bid > 0 else 0.0
        unit = "ratio"
    if value_sell <= 0 or value_buy <= 0:
        out["reason"] = "non-positive {} from supplied books".format(kind)
        return out

    ven_a, ven_b = a.get("venue", ""), b.get("venue", "")
    # Charged twice per leg: entering and exiting the spread trade is
    # four crossings, not two.
    cost = 2 * (_venue_fee_bps(ven_a) + _venue_fee_bps(ven_b))
    # Gross how far the tradeable ratio sits from its own mid, i.e. the
    # full spread of the two-leg package rather than one leg's.
    gross = abs(value_buy - value_sell) / value_sell * 10000.0
    edge = gross - cost

    z = None
    hist = history or {}
    if unit == "ratio":
        ha = [float(x) for x in (hist.get(str(a_symbol)) or []) if _f(x) > 0]
        hb = [float(x) for x in (hist.get(str(b_symbol)) or []) if _f(x) > 0]
        n = min(len(ha), len(hb))
        if n >= 20:
            series = [ha[i] / hb[i] for i in range(n) if hb[i] > 0]
            if len(series) >= 20:
                mean = sum(series) / len(series)
                var = sum((x - mean) ** 2 for x in series) / len(series)
                sd = var ** 0.5
                if sd > 0:
                    z = round((value_sell - mean) / sd, 2)

    out.update({"ok": True,
                "a_mid": round(a_mid, 8), "b_mid": round(b_mid, 8),
                "value": round(value_sell, 8),
                "value_inverse": round(value_buy, 8),
                "z_score": z,
                "gross_bps": round(gross, 2),
                "cost_bps": round(cost, 2),
                "edge_bps": round(edge, 2),
                "reason": ("spread wide vs fees" if edge > 0
                           else "spread inside fees -- no free lunch")})

    # -- expectancy: is it worth trading, not just wide right now?
    ex = None
    if kind == "ratio":
        ha = [float(x) for x in (hist.get(str(a_symbol)) or []) if _f(x) > 0]
        hb = [float(x) for x in (hist.get(str(b_symbol)) or []) if _f(x) > 0]
        ex = _expectancy(ha, hb, value_sell, cost)
    out["expectancy"] = ex
    if ex and ex.get("expectancy_bps") is not None:
        out["expectancy_bps"] = ex["expectancy_bps"]
        out["verdict"] = ex["verdict"]
    return out


def _expectancy(series_a, series_b, value, cost_bps, stop_z=2.5):
    """Expected bps per trade after costs and a stop, or None.

    This is the number that should drive a decision. `edge_bps` is the
    instantaneous width of the dislocation and says nothing about whether
    closing it is reachable before a stop.

    Units: all payoffs are in basis points of the *ratio's own value*, and
    so is `cost_bps`. A log move of `sigma` is roughly `sigma * 10000`
    bps of the ratio, which is the conversion used below.

    Returns None -- "cannot be estimated" -- rather than a flattering
    default whenever an input is missing. Reporting expectancy for a
    series with no measurable mean reversion is inventing the one quantity
    that matters.
    """
    out = {"expectancy_bps": None, "half_life": None, "stop_z": stop_z,
           "win_prob": None, "cost_bps": round(_f(cost_bps), 2),
           "verdict": "cannot estimate", "note": ""}
    n = min(len(series_a or []), len(series_b or []))
    if n < 30:
        out["note"] = "need >=30 aligned bars to fit a decay"
        return out
    ratio = [math.log(series_a[i] / series_b[i]) for i in range(n)
             if series_b[i] > 0 and series_a[i] > 0]
    if len(ratio) < 30:
        out["note"] = "degenerate ratio series"
        return out
    diffs = [ratio[i] - ratio[i - 1] for i in range(1, len(ratio))]
    rho = _ar1_rho(diffs)
    half_life = _half_life_from_rho(rho)
    out["half_life"] = round(half_life, 2) if half_life else None
    if not half_life:
        out["note"] = "series does not mean-revert (rho={})".format(
            round(rho, 3) if rho is not None else "n/a")
        return out

    mean = sum(ratio) / len(ratio)
    var = sum((x - mean) ** 2 for x in ratio) / len(ratio)
    sigma = math.sqrt(var)
    if sigma <= 0 or value <= 0:
        out["note"] = "zero variance in the ratio"
        return out

    # Dislocation in sigma units of the log ratio, signed.
    cur_z = (math.log(value) - mean) / sigma
    sign = 1.0 if cur_z >= 0 else -1.0
    stop = stop_z * sign
    win_p = _ou_hit_probability(cur_z, stop, half_life)
    if win_p is None:
        out["note"] = "first-passage probability unavailable"
        return out

    payoff_win = abs(cur_z) * sigma * 10000.0
    payoff_loss = abs(stop - cur_z) * sigma * 10000.0
    expectancy = win_p * payoff_win - (1.0 - win_p) * payoff_loss - _f(cost_bps)
    out.update({"expectancy_bps": round(expectancy, 2),
                "win_prob": round(win_p, 3),
                "current_z": round(cur_z, 2),
                "stop_z": stop_z})
    out["verdict"] = ("viable" if expectancy > 0 else
                      "marginal" if expectancy > -abs(_f(cost_bps)) * 0.25
                      else "not viable after costs and stop")
    return out


# FX crosses worth checking: (direct, [leg1, leg2]). Chosen because each
# is a genuinely liquid pivot triangle rather than an arbitrary product.
#
# Every leg here is verified as a live kraken pair. GBPJPY is the obvious
# omission: it is one of the most liquid crosses in the world and kraken
# returns "EQuery:Unknown asset pair" for it. It is excluded rather than
# left to fail at scan time, so the set only ever names tradeable pairs.
#
# The multiply-vs-divide relationship is not declared here: fx_cross
# derives it from the currency codes and refuses anything that does not
# compose, which is what stopped a meaningless triangle being priced as a
# 324,000,000 bps "edge".
FX_CROSSES = (
    ("EURUSD", ("EURGBP", "GBPUSD")),      # GBP/EUR x USD/GBP
    ("EURGBP", ("EURUSD", "GBPUSD")),      # USD/EUR / USD/GBP
)

# Crosses kraken cannot price, kept here so the exclusion is a decision
# someone can inspect rather than a pair quietly missing.
#
# Measured 2026-10-15 across every kraken pair this app routes:
#
#   pair     spread    1000 trades span    trades/hour
#   EURUSD     0.3 bp        0.3 h            3919
#   USDCHF     1.0 bp        0.4 h            2302
#   GBPUSD     0.7 bp        0.8 h            1296
#   USDCAD     0.4 bp        2.8 h             354
#   XAUTUSD    0.5 bp        7.9 h             127
#   AUDUSD     2.7 bp        8.0 h             125
#   EURGBP     1.1 bp       35.3 h              28
#   USDJPY   136.1 bp      537.7 h             1.9
#   EURJPY    90.4 bp     1092.3 h             0.9
#
# USDJPY trades about once every 13 minutes and EURJPY once every 65. Their
# top-of-book did not change at all across a 60-second sample (USDJPY
# 158.823/160.999 and EURJPY 176.000/177.599 at every tick, while EURUSD
# moved at 0.3-0.5 bps). `AssetPairs` reports both as `status: online`, so
# this is not a halt -- the markets are simply near-untraded, and the wide
# band is stale resting orders nobody has traded against.
#
# This is the same exclusion as GBPJPY above, for the same reason: kraken
# cannot support the triangle. It is not a transient glitch that will clear,
# so listing these would advertise two opportunities that can never exist.
# The book-sanity guard would refuse them anyway -- this just stops the scan
# spending its budget, and the operator's attention, on them.
#
# To restore: move the entry back into FX_CROSSES and re-measure. Do not
# re-add on the assumption the liquidity has returned; check trades/hour
# first, because a wide book on a market that trades once an hour is not a
# spread that tightens, it is a book with nobody in it.
FX_CROSSES_DORMANT = (
    ("EURUSD", ("EURJPY", "USDJPY")),      # JPY/EUR / JPY/USD
    ("EURJPY", ("EURUSD", "USDJPY")),      # USD/EUR x JPY/USD
)

# A dislocation counts as *persistent* once its books are older than the
# freshness window -- the exchange-default literature's case, where the gap
# survives because arbitrageurs hesitate on a venue they distrust.
STALE_PERSISTENCE_S = 90

# ...but only up to this age. Past it the feed is simply dead, and a gap
# built on week-old quotes is not a signal in either direction.
PERSISTENCE_MAX_AGE_S = 3600

# Commodity relationships: (a, b, kind, label). Ratio for metals
# (gold/silver is the classic), difference for energy cracks.
#
# Only gold has a real book (venue + Kraken both publish XAUTUSD), so
# gold/silver and platinum/palladium are the two entries whose *left-hand*
# leg is live. The rest are computed from generated quotes and are
# refused by `spread()` as unpriceable -- they exist so the relationship
# maths and its z-score are exercised, not to claim an executable edge.
COMMODITY_SPREADS = (
    ("XAUUSD", "XAGUSD", "ratio", "Gold / Silver"),
    ("XPTUSD", "XPDUSD", "ratio", "Platinum / Palladium"),
    ("COPPER", "ALUM", "ratio", "Copper / Aluminium"),
    ("BRENT", "WTI", "difference", "Brent - WTI crack"),
    ("WTI", "HEATOIL", "difference", "WTI - Heating Oil"),
    ("CORN", "WHEAT", "ratio", "Corn / Wheat"),
    ("COFFEE", "COCOA", "ratio", "Coffee / Cocoa"),
)


def scan_crypto(symbols, books_by_symbol, size=None, attribute=False):
    """Run every crypto model over the routed symbols.

    Args:
      symbols:       list of crypto symbols to consider.
      books_by_symbol: {symbol: {venue: book}}.
      attribute:     also run `venue_attribution` per symbol, so each row
                     says whether its gap survives dropping a venue. Off by
                     default because it re-runs the cross-venue model once
                     per venue; on for any surface that shows a number.
    """
    out = []
    for sym in sorted(symbols or []):
        node = (books_by_symbol or {}).get(sym) or {}
        if len(node) >= 2:
            row = cross_venue(sym, node, size=size)
            if attribute:
                row["attribution"] = venue_attribution(sym, node, size=size)
            out.append(row)
    return out


def scan_triangles(symbols, books_by_symbol, fee_multiplier=1.0):
    """Enumerate 3-cycles over the crypto universe.

    With 5 routed symbols there are 60 ordered cycles and only 10 are
    distinct up to rotation; both directions are kept because the
    reverse cycle is a genuinely different trade. Capped so a wider
    universe cannot turn a UI request into a combinatorial stall.
    """
    syms = [str(s or "").strip().upper() for s in (symbols or [])]
    syms = [s for s in syms if s in (books_by_symbol or {})]
    results = []
    seen = set()
    cap = 24
    for i in range(len(syms)):
        for j in range(len(syms)):
            for k in range(len(syms)):
                if len({i, j, k}) != 3:
                    continue
                key = tuple(sorted((syms[i], syms[j], syms[k])))
                if key in seen:
                    continue
                seen.add(key)
                cycle = (syms[i], syms[j], syms[k])
                r = triangular(cycle, books_by_symbol,
                               fee_multiplier=fee_multiplier)
                if r.get("ok") or r.get("legs"):
                    results.append(r)
                if len(results) >= cap:
                    return results
    return results


def scan_fx(books_by_symbol):
    """Direct-vs-cross for every configured triangle that has books."""
    books = books_by_symbol or {}
    out = []
    for direct, (l1, l2) in FX_CROSSES:
        dbook = _flat(books.get(direct))
        l1b = _flat(books.get(l1))
        l2b = _flat(books.get(l2))
        if dbook is None or l1b is None or l2b is None:
            continue
        legs = [dict(l1b or {}, symbol=l1), dict(l2b or {}, symbol=l2)]
        out.append(fx_cross(direct, dbook, legs))
    return out


def scan_commodity(books_by_symbol, history=None):
    """Every configured commodity relationship that has two live books."""
    books = books_by_symbol or {}
    out = []
    for a, b, kind, label in COMMODITY_SPREADS:
        ab = _flat(books.get(a))
        bb = _flat(books.get(b))
        if ab is None or bb is None:
            continue
        out.append(spread(a, ab, b, bb, history=history, label=label, kind=kind))
    return out


def _flat(node):
    """Collapse {venue: book} (or a bare book) into one book, best midpoint.

    A symbol with several venues does not have one price. Choosing the
    venue with the tightest spread is the honest reduction, and it is
    recorded as `venue` so the caller can still see where it came from.
    """
    if not node:
        return None
    if "bid" in node or "ask" in node:
        return dict(node)
    best, best_spread = None, None
    for venue, book in node.items():
        bid, ask = _positive((book or {}).get("bid")), _positive((book or {}).get("ask"))
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        spread = ask - bid
        if best_spread is None or spread < best_spread:
            row = dict(book)
            row["venue"] = (book or {}).get("venue", venue)
            best, best_spread = row, spread
    return best
