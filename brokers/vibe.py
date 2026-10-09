"""Vibe layer: a shadow account and a grounding gate, stdlib only.

A port of two ideas from HKUDS/Vibe-Trading (MIT), reimplemented against
this application's own data rather than vendored:

*Shadow account* -- infer if/then entry rules from a user's own profitable
roundtrips, then scan the live book for those rules firing. The point is
not to be told a strategy; it is to see whether the rules you already
follow still describe the market you are in.

*Grounding gate* -- Vibe-Trading found that a language model will state a
confident number that no tool ever observed, and that catching this with
regex catalogues of phrasings is a losing game (their own changelog calls
the 65-regex approach a fix commit per wording). Their fix was to have
the model *declare* each figure's kind in a structured block and check
the declaration against the session's observations.

This module implements the checkable half of that idea without a model:
`Ground` records what was actually observed, and `audit_text` reports
which figures in a rendered line can be traced to an observation. It is
deliberately strict -- an untraceable number is reported, never silently
passed through, because the failure mode it exists to catch is a
plausible wrong number.

Deliberate differences from the upstream project
-----------------------------------------------
* No codegen. Vibe-Trading generates Python from an extracted profile
  and runs it. Here the rules are a frozen dataclass tree walked by an
  explicit evaluator, so a journal -- which is untrusted input -- cannot
  become executable code. This is the single largest divergence and it
  is a security decision, not a simplification.
* No LLM. Rule extraction is frequency-and-support based, so it is
  deterministic and reproducible. Same journal in, same rules out.
* Fail-closed on feature gaps. A rule whose inputs are missing on a bar
  does not match that bar. Upstream found 84 of 462 alphas returned a
  number anyway because a NaN comparison is False and the ternary fell
  through to a constant; that class of bug is designed out here.

No network, no filesystem writes, no globals: everything is a plain
object that the interactive layer calls with data it already has.
"""
import math
import re
import time
from collections import Counter, defaultdict

from .base import BrokerError


class VibeError(BrokerError):
    """Bad input to the Vibe layer (bad journal, bad rule, bad window)."""


#: Quantity tolerance. A fill is dust-free to ~1e-12 of a unit, so this
#: only ever swallows floating-point residue from a matched pairing.
_EPS = 1e-12


# ====================================================================== rules
# A condition is a small immutable tree. Three node kinds:
#   ("cmp", feature, op, threshold)  a numeric comparison
#   ("in",  feature, frozenset)      membership (e.g. asset_type)
#   ("all"/"any"/"not", children)    boolean combination
#
# Keeping conditions as data rather than code is what makes an untrusted
# journal safe to feed in. There is no eval, no exec, and no import path
# from a journal cell to anything executable.

_CMP_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}

_IN_OPS = {
    "in": lambda v, allowed: v in allowed,
    "not_in": lambda v, allowed: v not in allowed,
}


def cmp(feature, op, threshold):
    """Numeric comparison node. Raises on an unknown operator."""
    if op not in _CMP_OPS:
        raise VibeError("unknown operator {!r}".format(op))
    return ("cmp", str(feature), str(op), float(threshold))


def member(feature, op, values):
    """Membership node: ``in`` or ``not_in`` over a set of strings."""
    if op not in _IN_OPS:
        raise VibeError("unknown membership operator {!r}".format(op))
    return ("in", str(feature), str(op), frozenset(str(v) for v in values))


def between(feature, low, high):
    """Half-open range node: ``low <= x < high``.

    Half-open because that is how the bucketing edges partition the
    line. A closed upper bound would make the boundary bar belong to two
    buckets, and the feature_support count would then double-count it.
    """
    low, high = float(low), float(high)
    if not high > low:
        raise VibeError(
            "empty range for {!r}: {!r} is not below {!r}".format(
                feature, low, high))
    return ("between", str(feature), low, high)


def all_of(*children):
    return ("all", tuple(children))


def any_of(*children):
    return ("any", tuple(children))


def negate(child):
    return ("not", (child,))


def eval_condition(cond, features):
    """Evaluate a condition tree against a feature mapping.

    Returns True/False. A *missing* feature yields False, never a
    fallback: an absent input must not be able to satisfy a rule, since
    a rule that fires on absent data is a rule that fires on everything.
    """
    kind = cond[0]
    if kind == "all":
        return all(eval_condition(c, features) for c in cond[1])
    if kind == "any":
        return any(eval_condition(c, features) for c in cond[1])
    if kind == "not":
        return not eval_condition(cond[1][0], features)
    if kind == "cmp":
        _, feature, op, threshold = cond
        value = features.get(feature)
        if value is None:
            return False
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False
        if math.isnan(value) or math.isinf(value):
            # NaN compares False against everything, so an unguarded
            # comparison could accidentally satisfy a `!=` or `<=`.
            return False
        return _CMP_OPS[op](value, threshold)
    if kind == "between":
        _, feature, low, high = cond
        value = features.get(feature)
        if value is None:
            return False
        try:
            value = float(value)
        except (TypeError, ValueError):
            return False
        if math.isnan(value) or math.isinf(value):
            return False
        return low <= value < high
    if kind == "in":
        _, feature, op, allowed = cond
        value = features.get(feature)
        if value is None:
            return False
        return _IN_OPS[op](str(value), allowed)
    raise VibeError("unknown condition node {!r}".format(kind))


def features_of(cond, out=None):
    """Every feature name a condition tree reads. Used to pre-check gaps."""
    if out is None:
        out = set()
    kind = cond[0]
    if kind in ("all", "any", "not"):
        for child in cond[1]:
            features_of(child, out)
    elif kind == "between":
        out.add(cond[1])
    elif kind in ("cmp", "in"):
        out.add(cond[1])
    else:
        raise VibeError("unknown condition node {!r}".format(kind))
    return out


# ==================================================================== rules
class Rule(object):
    """One inferred if/then rule, with the evidence that produced it.

    ``support`` and ``coverage`` travel with the rule on purpose: a rule
    that fires on one trade out of two hundred is not a rule, and a
    caller that only sees the condition cannot tell that.
    """

    __slots__ = ("rule_id", "text", "entry", "exit_", "support",
                 "coverage", "avg_hold_bars", "samples")

    def __init__(self, rule_id, text, entry, exit_=None, support=0,
                 coverage=0.0, avg_hold_bars=0.0, samples=()):
        self.rule_id = rule_id
        self.text = text
        self.entry = entry
        self.exit_ = exit_
        self.support = int(support)
        self.coverage = float(coverage)
        self.avg_hold_bars = float(avg_hold_bars)
        self.samples = tuple(samples)

    @property
    def strength(self):
        """A single ordering score: support weighted by coverage.

        Coverage is squared because two rules each covering 40% overlap
        heavily, and reporting both as equally strong overstates how much
        of the journal is actually explained.
        """
        return self.support * (self.coverage ** 2)

    def matches(self, features):
        return eval_condition(self.entry, features)

    def to_dict(self):
        return {
            "rule_id": self.rule_id,
            "text": self.text,
            "entry": _cond_to_json(self.entry),
            "exit": _cond_to_json(self.exit_) if self.exit_ else None,
            "support": self.support,
            "coverage": round(self.coverage, 4),
            "strength": round(self.strength, 4),
            "avg_hold_bars": self.avg_hold_bars,
            "samples": list(self.samples),
        }

    def __repr__(self):
        return "<Rule {} support={} coverage={:.2f}>".format(
            self.rule_id, self.support, self.coverage)


def _cond_to_json(cond):
    """Condition tree -> JSON-safe, with sets as sorted lists."""
    if cond is None:
        return None
    kind = cond[0]
    if kind in ("all", "any"):
        return {kind: [_cond_to_json(c) for c in cond[1]]}
    if kind == "not":
        return {"not": _cond_to_json(cond[1][0])}
    if kind == "cmp":
        return {"feature": cond[1], "op": cond[2], "value": cond[3]}
    if kind == "between":
        return {"feature": cond[1], "low": cond[2], "high": cond[3]}
    if kind == "in":
        return {"feature": cond[1], "op": cond[2],
                "values": sorted(cond[3])}
    raise VibeError("unknown condition node {!r}".format(kind))


def _cond_from_json(obj):
    """Inverse of _cond_to_json, for rules that round-trip through JSON."""
    if obj is None:
        return None
    if "all" in obj:
        return all_of(*[_cond_from_json(c) for c in obj["all"]])
    if "any" in obj:
        return any_of(*[_cond_from_json(c) for c in obj["any"]])
    if "not" in obj:
        return negate(_cond_from_json(obj["not"]))
    if "feature" in obj and "op" in obj:
        if "values" in obj:
            return member(obj["feature"], obj["op"], obj["values"])
        return cmp(obj["feature"], obj["op"], obj["value"])
    if "feature" in obj and "low" in obj and "high" in obj:
        return between(obj["feature"], obj["low"], obj["high"])
    raise VibeError("unrecognised condition: {!r}".format(obj))


# ================================================================= features
# Feature names are fixed here, on the boundary between the extractor and
# the scanner, so the two cannot drift: a rule referring to a feature the
# scanner never computes would simply never fire.
FEATURE_NAMES = (
    "entry_rsi14",          # Wilder RSI at the entry bar
    "prior_return",         # close-to-close return over the lookback
    "volume_ratio",         # entry volume / mean volume over the lookback
    "range_pct",            # (high - low) / close on the entry bar
    "hour_utc",             # hour of the entry bar, UTC
    "weekday",              # 0 = Monday
)

#: Which features are numeric-comparable, and which are categorical.
NUMERIC_FEATURES = ("entry_rsi14", "prior_return", "volume_ratio", "range_pct")
CATEGORICAL_FEATURES = ("hour_utc", "weekday")


def rsi(values, period=14):
    """Wilder's RSI over a list of closes. Stdlib, no numpy.

    Returns a list aligned to ``values``; the first ``period`` entries are
    None because RSI is undefined until there is enough history. Callers
    must treat None as absent, not as zero -- a zero RSI is a real
    reading (a flat market), and conflating the two is how a rule ends up
    matching every quiet bar in the book.
    """
    n = len(values)
    out = [None] * n
    if n < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        delta = values[i] - values[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, n):
        delta = values[i] - values[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain, avg_loss):
    if avg_loss == 0:
        # No downward movement in the window. RSI is 100 by
        # convention, not an error -- but it is a *limit*, so a rule
        # keyed on a high RSI should not be surprised by it.
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def rolling_mean(values, window):
    out = [None] * len(values)
    if window <= 0:
        return out
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= window:
            total -= values[i - window]
        if i >= window - 1:
            out[i] = total / window
    return out


def compute_features(closes, highs=None, lows=None, volumes=None,
                     timestamps=None, lookback=20, rsi_period=14):
    """Feature series for a candle history, aligned to ``closes``.

    Index i of every returned list describes the bar *ending* at closes
    [i], i.e. the same bar a rule would be evaluated on. Entries before
    enough history exists are None, and None is what makes a rule
    inapplicable rather than falsely satisfied.

    Feature definitions, spelled out because the bucket edges in
    ``_BUCKET_EDGES`` are only meaningful against them:

    ``prior_return``   simple return of the bar itself, (c[i]-c[i-1])/c[i-1]
    ``entry_rsi14``    Wilder RSI(14) of the bar, None until bar 14
    ``volume_ratio``   v[i] / mean(v[i-lookback .. i])
    ``range_pct``      (high[i]-low[i])/close[i]
    ``hour_utc``       UTC hour of the bar's open
    ``weekday``        UTC weekday, 0=Monday

    ``prior_return`` is deliberately one bar, not ``lookback`` bars: the
    bucket edges are +/-1% and +/-5%, which describe a single minute bar
    of a liquid instrument and would swallow every bar of a daily one.
    ``lookback`` applies to ``volume_ratio`` alone.

    ``timestamps`` is a list of epoch seconds, one per bar. It is
    required for the two calendar features (``hour_utc``, ``weekday``);
    when it is omitted, or its length disagrees with ``closes``, those
    two stay None for every bar, so a rule keyed on them simply never
    fires instead of firing on a guess. The alternative -- defaulting the
    hour to 0 -- makes every open trade look like a London-open trade.
    """
    n = len(closes)
    if n == 0:
        return {name: [] for name in FEATURE_NAMES}
    highs = highs if highs is not None else closes
    lows = lows if lows is not None else closes
    volumes = volumes if volumes is not None else [0.0] * n
    have_ts = bool(timestamps) and len(timestamps) == n

    rsi_series = rsi(closes, rsi_period)
    mean_vol = rolling_mean(volumes, lookback)

    out = {name: [None] * n for name in FEATURE_NAMES}
    for i in range(n):
        if i >= 1 and closes[i - 1]:
            out["prior_return"][i] = (closes[i] - closes[i - 1]) / closes[i - 1]
        if i >= 1 and rsi_series[i] is not None:
            out["entry_rsi14"][i] = rsi_series[i]
        if mean_vol[i] and mean_vol[i] > 0:
            out["volume_ratio"][i] = volumes[i] / mean_vol[i]
        if closes[i]:
            out["range_pct"][i] = (highs[i] - lows[i]) / closes[i]
        if have_ts:
            # UTC explicitly. A rule extracted from a journal in one zone
            # and scanned in another would be a rule about a different
            # hour of the day, and the two would agree on the count while
            # describing opposite things.
            try:
                out["hour_utc"][i] = int(
                    time.gmtime(int(timestamps[i])).tm_hour)
                out["weekday"][i] = int(
                    time.gmtime(int(timestamps[i])).tm_wday)
            except (TypeError, ValueError, OSError, OverflowError):
                pass
    return out


# ================================================================= roundtrips
class Roundtrip(object):
    """A matched open/close pair from the user's own fills."""

    __slots__ = ("symbol", "side", "open_ts", "open_price", "qty",
                 "close_ts", "close_price", "pnl", "bars_held")

    def __init__(self, symbol, side, open_ts, open_price, qty,
                 close_ts, close_price, pnl, bars_held=0):
        self.symbol = symbol
        self.side = side
        self.open_ts = int(open_ts)
        self.open_price = float(open_price)
        self.qty = float(qty)
        self.close_ts = int(close_ts)
        self.close_price = float(close_price)
        self.pnl = float(pnl)
        self.bars_held = int(bars_held)

    @property
    def profitable(self):
        return self.pnl > 0

    def to_dict(self):
        return {
            "symbol": self.symbol, "side": self.side,
            "open_ts": self.open_ts, "open_price": self.open_price,
            "qty": self.qty, "close_ts": self.close_ts,
            "close_price": self.close_price, "pnl": round(self.pnl, 6),
            "bars_held": self.bars_held,
        }

    def __repr__(self):
        return "<Roundtrip {} {} pnl={:.2f}>".format(
            self.symbol, self.side, self.pnl)


def roundtrips_from_fills(fills, bar_seconds=60):
    """Pair fills into roundtrips with FIFO matching, per symbol.

    ``fills`` is a sequence of dicts with symbol, side, qty, price, ts.
    Only long and flat behaviour is modelled: a sell larger than the open
    position closes what it can and opens a short with the remainder.
    Shorts are kept (Vibe-Trading's extractor does not assume direction
    either) but are reported separately by the caller, because a rule
    learned only from shorts should not be applied to a long book.
    """
    by_symbol = defaultdict(list)
    for f in fills:
        try:
            sym = str(f["symbol"]).strip()
            side = str(f.get("side", "buy")).strip().lower()
            qty = float(f["qty"])
            price = float(f["price"])
            ts = int(f["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if not sym or qty <= 0 or price <= 0 or side not in ("buy", "sell"):
            continue
        by_symbol[sym].append((ts, side, qty, price))

    out = []
    for sym in sorted(by_symbol):
        # Stable sort by ts: fills recorded in the same second must keep
        # the order they arrived in, or the pairing is arbitrary.
        events = sorted(by_symbol[sym], key=lambda e: e[0])
        # Lots of one direction at a time: [qty, price, ts, direction],
        # direction +1 long / -1 short. Keeping both signs in one list
        # would let a fill net against a lot it never faced.
        lots = []
        for ts, side, qty, price in events:
            # A buy opens long / closes short; a sell is the reverse.
            direction = 1 if side == "buy" else -1
            remaining = qty

            # Close against opposing lots, oldest first (FIFO).
            while remaining > _EPS and lots and lots[0][3] != direction:
                lot_qty, lot_price, lot_ts, lot_dir = lots[0]
                matched = min(lot_qty, remaining)
                # Sign as the venue would report it: a long profits when
                # the exit is above entry, a short when it is below.
                pnl = (price - lot_price) * matched * lot_dir
                out.append(Roundtrip(
                    sym, "long" if lot_dir > 0 else "short",
                    lot_ts, lot_price, matched, ts, price, pnl,
                    bars_held=max(0, int((ts - lot_ts) / bar_seconds))))
                lot_qty -= matched
                remaining -= matched
                if lot_qty <= _EPS:
                    lots.pop(0)
                else:
                    lots[0][0] = lot_qty

            # Whatever is left opens a lot in the fill's own direction.
            # Dropping this remainder was the original defect: a sell
            # larger than the long book vanished, so the journal's own
            # position never reconciled and every later PnL was measured
            # against a position that had quietly changed size.
            if remaining > _EPS:
                lots.append([remaining, price, ts, direction])
    return out


# ================================================================ extractor
# Bucketing thresholds. Chosen so a bucket means "the trader was in this
# state", not "the number round happened to be near this value".
_BUCKET_EDGES = {
    "entry_rsi14": (30.0, 45.0, 55.0, 70.0),
    "prior_return": (-0.05, -0.01, 0.01, 0.05),
    "volume_ratio": (0.7, 1.0, 1.5, 2.5),
    "range_pct": (0.005, 0.01, 0.02, 0.04),
}
_CATEGORICAL_KEYS = {
    "hour_utc": lambda v: "h{:02d}".format(int(v) // 3),
    "weekday": lambda v: "d{}".format(int(v)),
}


def bucket_label(feature, value):
    """Coarse label for a feature value, or None when unusable."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    if feature in _BUCKET_EDGES:
        edges = _BUCKET_EDGES[feature]
        if v < edges[0]:
            return "very_low"
        if v < edges[1]:
            return "low"
        if v < edges[2]:
            return "mid"
        if v < edges[3]:
            return "high"
        return "very_high"
    if feature in _CATEGORICAL_KEYS:
        try:
            return _CATEGORICAL_KEYS[feature](v)
        except (TypeError, ValueError):
            return None
    return None


def bucket_threshold(feature, label):
    """The condition a bucket label stands for, as a node spec.

    A bucket is a half-open range, so the condition has to *reproduce the
    range*, not summarise it. A one-sided ``>=`` would be wrong in both
    directions: it would drop the bars below the bucket's lower edge
    (understating the rule) and admit the bars above its upper edge
    (overstating it). An overstating rule is the worse of the two: it
    fires on bars its own support count never contained, so the scan
    would report matches the journal cannot account for.

    Interior buckets therefore get an explicit ``between`` node. Only the
    open-ended outer buckets collapse to a single comparison.
    """
    edges = _BUCKET_EDGES.get(feature)
    if not edges:
        return None
    e0, e1, e2, e3 = edges
    if label == "very_low":
        return ("cmp", feature, "<", e0)
    if label == "low":
        return ("between", feature, e0, e1)
    if label == "mid":
        return ("between", feature, e1, e2)
    if label == "high":
        return ("between", feature, e2, e3)
    if label == "very_high":
        return ("cmp", feature, ">=", e3)
    return None


class Profile(object):
    """A shadow account: rules inferred from one journal.

    ``covered`` is the number of profitable roundtrips at least one rule
    matches, counted as a union at extraction time. It is stored rather
    than recomputed from the rules because the rules alone cannot
    reproduce it: coverage is a property of the *journal*, and the
    journal is not kept.
    """

    __slots__ = ("profile_id", "rules", "total", "profitable", "covered",
                 "source", "created_at", "notes")

    def __init__(self, profile_id, rules, total, profitable, covered=None,
                 source="", created_at="", notes=""):
        self.profile_id = profile_id
        self.rules = tuple(rules)
        self.total = int(total)
        self.profitable = int(profitable)
        # Default to the sum-of-coverages union *only* when the caller
        # supplies nothing better, and say so in the notes.
        if covered is None:
            self.covered = min(self.profitable,
                               int(round(sum(r.coverage * self.profitable
                                             for r in self.rules))))
        else:
            self.covered = int(covered)
        self.source = source
        self.created_at = created_at
        self.notes = notes

    @property
    def covered_fraction(self):
        return (self.covered / self.profitable) if self.profitable else 0.0

    @property
    def win_rate(self):
        return (self.profitable / self.total) if self.total else 0.0

    @property
    def explained(self):
        """Fraction of profitable roundtrips at least one rule covers.

        The honest definition is a union, not a sum. One-feature rules
        from the same extractor overlap heavily -- a trade in the
        bottom-RSI bucket usually also sits in a low-volume bucket -- so
        summing coverages and clamping at 1.0 reports "100% explained"
        for a profile whose rules collectively mention every trade
        without any of them being worth acting on.
        """
        if not self.profitable:
            return 0.0
        return self.covered_fraction

    def to_dict(self):
        return {
            "profile_id": self.profile_id,
            "source": self.source,
            "created_at": self.created_at,
            "total_roundtrips": self.total,
            "profitable_roundtrips": self.profitable,
            "win_rate": round(self.win_rate, 4),
            "covered": self.covered,
            "explained": round(self.explained, 4),
            "notes": self.notes,
            "rules": [r.to_dict() for r in self.rules],
        }

    def __repr__(self):
        return "<Profile {} rules={} win_rate={:.2f}>".format(
            self.profile_id, len(self.rules), self.win_rate)


class FeatureSpace(object):
    """The feature vocabulary a profile is expressed in.

    A space bundles the three things that have to agree for a rule to be
    honest: which features exist, how a value becomes a bucket label, and
    how a label becomes a condition. The base space and the God space
    differ in all three, and mixing them -- counting support under one
    bucketing and evaluating under another -- is how a rule ends up
    firing on bars nothing in the journal supports.

    ``label`` is a callable(feature, value) -> label or None.
    ``condition`` is a callable(feature, label) -> node or None.
    """

    __slots__ = ("name", "names", "label", "condition")

    def __init__(self, name, names, label, condition):
        self.name = name
        self.names = tuple(names)
        self.label = label
        self.condition = condition


def extract_profile(journal, feature_sets, min_support=3, max_rules=5,
                    profile_id="shadow_local", source="", created_at="",
                    min_coverage=0.0, space=None):
    """Infer rules from profitable roundtrips.

    ``journal`` is a list of Roundtrip; ``feature_sets`` maps
    roundtrip-index -> feature dict, i.e. the features as they stood
    *at that roundtrip's entry bar*. Index alignment is the caller's
    responsibility and is checked here, because a silently misaligned
    mapping would produce rules that look confident and mean nothing.

    ``space`` selects the feature vocabulary. It defaults to the base
    space; the God space is passed explicitly by a caller that computed
    God features, and the two are never mixed.

    A roundtrip with a missing or unrecognisable feature is skipped, not
    defaulted. Rules are then the most common bucket combinations among
    the survivors, filtered by support and coverage.
    """
    space = space or BASE_SPACE
    if len(feature_sets) != len(journal):
        raise VibeError(
            "feature/journal length mismatch: {} features for {} "
            "roundtrips".format(len(feature_sets), len(journal)))
    winners = [i for i, rt in enumerate(journal) if rt.profitable]
    if not winners:
        raise VibeError(
            "no profitable roundtrips: nothing to learn from "
            "({} roundtrips, none positive)".format(len(journal)))

    # Tally each feature's bucket label across the winners. This is the
    # only tally there is: an earlier version also counted whole label
    # *combinations* into a second, nested structure that nothing ever
    # read, which cost a dict build per roundtrip and implied a
    # combination-level analysis the ranking below does not do.
    feature_support = defaultdict(Counter)
    usable = []
    for i in winners:
        feats = feature_sets[i] or {}
        labels = {}
        for name in space.names:
            label = space.label(name, feats.get(name))
            if label is not None:
                labels[name] = label
        if not labels:
            # No usable feature on this bar. Counting it as "matched
            # nothing" would let a rule claim support it never had.
            continue
        usable.append(i)
        for name, label in labels.items():
            feature_support[name][label] += 1

    if not usable:
        raise VibeError(
            "every profitable roundtrip lacked the features a rule needs; "
            "need at least one of: " + ", ".join(space.names))

    # One candidate per (feature, bucket) pair. Ranking by how many
    # roundtrips the label appears in is what makes a two-feature rule
    # outrank a one-feature rule that explains more of the journal.
    candidates = []
    for name in space.names:
        if name not in feature_support:
            continue
        for label, count in feature_support[name].most_common():
            if count < min_support:
                continue
            coverage = count / float(len(usable))
            if coverage < min_coverage:
                continue
            cond = space.condition(name, label)
            if cond is None:
                continue
            matched = [i for i in usable
                       if space.label(name, feature_sets[i].get(name)) == label]
            holds = [journal[i].bars_held for i in matched if journal[i].bars_held]
            candidates.append((count, coverage, name, label, cond, holds, matched))

    if not candidates:
        raise VibeError(
            "no rule reached min_support={} over {} profitable "
            "roundtrips; lower it or gather more trades".format(
                min_support, len(usable)))

    # Deterministic ordering: strength, then rule_id-ish tiebreak on
    # (count, feature, label) so the same journal always yields the same
    # profile, whatever the dict iteration order.
    candidates.sort(key=lambda c: (-c[0], -c[1], c[2], c[3]))
    chosen = candidates[:max_rules]

    rules = []
    for idx, (count, coverage, name, label, cond, holds, matched) in enumerate(chosen, 1):
        samples = tuple("{}@{}".format(journal[i].symbol, journal[i].open_ts)
                        for i in matched[:3])
        rules.append(Rule(
            rule_id="R{}".format(idx),
            text="entry when {} is {}".format(name, label.replace("_", " ")),
            entry=cond,
            exit_=None,
            support=count,
            coverage=coverage,
            avg_hold_bars=(sum(holds) / float(len(holds))) if holds else 0.0,
            samples=samples,
        ))

    note = ("{} of {} profitable roundtrips had usable features; "
            "{} skipped for missing data".format(
                len(usable), len(winners), len(winners) - len(usable)))
    # Union of what the *chosen* rules cover, counted over the journal
    # rather than summed from the rules' own coverage figures.
    covered = set()
    for _count, _cov, _name, _label, _cond, _holds, matched in chosen:
        covered.update(matched)
    return Profile(profile_id, rules, len(journal), len(winners),
                   covered=len(covered),
                   source=source, created_at=created_at, notes=note)


def _matches_bucket(features, name, label, space=None):
    """Does this feature dict carry the label, under this space's rules?"""
    space = space or BASE_SPACE
    return space.label(name, features.get(name)) == label


def _condition_for(name, label):
    """Build the condition node for a bucket label, or None if unmapped.

    The spec from ``bucket_threshold`` is turned into a real node here, so
    the two cannot disagree about what a label means: a rule built from
    a different condition than the one its support was counted under is
    a rule that fires on bars nothing in the journal supports.
    """
    spec = bucket_threshold(name, label)
    if spec is not None:
        node = spec[0]
        if node == "between":
            return between(spec[1], spec[2], spec[3])
        return cmp(spec[1], spec[2], spec[3])
    if name == "hour_utc":
        # Labels are 3-hour blocks: h00 = 00:00-02:59 UTC, etc.
        block = int(label[1:])
        return member("hour_utc", "in",
                      [h for h in range(24) if h // 3 == block])
    if name == "weekday":
        return cmp("weekday", "==", float(label[1:]))
    return None


#: The base feature space: six features, coarse buckets.
BASE_SPACE = FeatureSpace(
    "base", FEATURE_NAMES, bucket_label, _condition_for)


# ================================================================== scanner
class Signal(object):
    """A rule firing on a symbol, with the evidence for the match."""

    __slots__ = ("symbol", "rule_id", "text", "features", "price", "ts")

    def __init__(self, symbol, rule_id, text, features, price, ts):
        self.symbol = symbol
        self.rule_id = rule_id
        self.text = text
        self.features = dict(features)
        self.price = price
        self.ts = int(ts)

    def to_dict(self):
        return {
            "symbol": self.symbol, "rule_id": self.rule_id, "text": self.text,
            "price": self.price, "ts": self.ts,
            "features": {k: v for k, v in self.features.items()
                         if v is not None},
        }

    def __repr__(self):
        return "<Signal {} {} {}>".format(self.symbol, self.rule_id, self.text)


def scan(profile, series_by_symbol, prices=None, ts=None, limit=50,
         space=None):
    """Evaluate every rule against the latest bar of each symbol.

    ``series_by_symbol`` maps symbol -> dict of feature lists from
    ``compute_features`` (or ``compute_god_features``). Only the final bar
    is considered: the question is "what fires right now", not a
    backtest.

    ``space`` must match the space the profile was extracted under. The
    scanner reads only the features in that space out of each series, so a
    God profile scanned against base features -- or the reverse -- would
    report gaps for rules that are actually evaluable, and would do it
    silently.

    A symbol whose latest bar has a gap in the features a rule needs is
    skipped for that rule, and the skip is reported so an all-quiet book
    reads as "no signal, and here is why" rather than "no signal".
    """
    space = space or BASE_SPACE
    signals, gaps = [], []
    for symbol in sorted(series_by_symbol):
        feats = series_by_symbol[symbol] or {}
        last = {}
        for name in space.names:
            series = feats.get(name) or []
            last[name] = series[-1] if series else None
        for rule in profile.rules:
            if rule.matches(last):
                price = (prices or {}).get(symbol)
                signals.append(Signal(symbol, rule.rule_id, rule.text, last,
                                      price, ts or 0))
            else:
                missing = [f for f in sorted(features_of(rule.entry))
                           if last.get(f) is None]
                if missing:
                    gaps.append((symbol, rule.rule_id, missing))
    # Sort the rule_id lexically: a session past 9 rules would order
    # R10 before R2, which is wrong and reads as a bug in the output.
    signals.sort(key=lambda s: (_rule_sort_key(s.rule_id), s.symbol))
    if limit:
        signals = signals[:limit]
    return signals, gaps


def _rule_sort_key(rule_id):
    """Natural sort for 'R2' < 'R10'. Digits compare numerically."""
    match = re.match(r"^([A-Za-z]*)(\d*)(.*)$", str(rule_id))
    if not match:
        return (str(rule_id), 0, "")
    prefix, digits, rest = match.groups()
    return (prefix, int(digits) if digits else 0, rest)


# ================================================================= grounding
# Matches a number in rendered text. Deliberately conservative: it wants
# a digit group with an optional decimal part, optionally thousands-
# separated, and will not fire mid-identifier. The two lookbehinds matter
# separately -- `[\w.]` keeps it out of `AAPL2` and `v1.2.3`, and the
# fixed-width `[\w][-_.]` keeps it out of a digit glued to a name by a
# joiner (`ETH-2024`, `rule_2`). Without the second, a hyphen is not a
# word character, so the token after it looks like a fresh number.
_NUM_RE = re.compile(
    r"(?<![\w.])"          # not mid-identifier
    r"(?<![\w][-_.])"      # not the tail of a joined token
    r"[-+]?"                # sign
    r"\d{1,3}(?:,\d{3})+"  # 1,234 -- thousands grouped
    r"(?:\.\d+)?(?![\w])"
    r"|"
    r"(?<![\w.])"
    r"(?<![\w][-_.])"      # repeated: applies to this branch too
    r"[-+]?\d+(?:\.\d+)?(?![\w])"
)

# Token shapes that are numbers in form but are not measurements. Each is
# removed *whole*, by shape, before any figure is read. Removing them by
# value instead -- a list of "boring" numbers -- is the mistake upstream
# describes: the list has to be amended for every new wording, and a real
# PnL of exactly 100.0 ends up silently exempt.
_NOT_FIGURE_SHAPES = (
    # ISO date, and a datetime with or without a zone
    re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?"
               r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?"),
    # clock time, with or without seconds
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?"),
    # version-ish: v1.2.3 / 1.2.3 (exactly one digit per group, so a
    # thousands-grouped figure like 1.234.567 is not swallowed)
    re.compile(r"(?<![\w])\d+\.\d+\.\d+(?![\w])"),
)

# An ordinal counts items in a list; it is recognised by the word in
# front of it. "Rule 2 of 5" is not a claim about the market.
#
# Order matters, and the order is the fix. Erasing "Rule 2" first turns
# "Rule 2 of 5" into "       of 5", and the count-of relation is then
# no longer visible, so the trailing 5 is left behind and flagged as an
# invented figure. The compound relation has to be consumed while the
# context is still intact; the bare label form is the fallback.
_ORDINAL_SHAPES = (
    re.compile(r"\b\d+\s+of\s+\d+\b", re.I),
    re.compile(r"\b(?:rule|step|phase|part|item|entry|row|no|number|version)"
               r"[\s#:]*\d+", re.I),
)


def _blank_shapes(text, shapes):
    """Erase matches in place, preserving length and every other char."""
    for shape in shapes:
        text = shape.sub(lambda m: " " * len(m.group(0)), text)
    return text


#: Significant digits kept when comparing a rendered figure to an
#: observation. Six is the point at which a *different* number still
#: differs: 0.1+0.2 printed as "0.3" matches, but 1234.6 never matches
#: 1234.56.
_SIG_DIGITS = 6


def _sig_key(value):
    """Normalise to N significant digits, so display rounding compares."""
    try:
        return float("%.*g" % (_SIG_DIGITS, value))
    except (TypeError, ValueError, OverflowError):
        return None


class Ground(object):
    """Records what was actually observed, so a number can be checked.

    This is the checkable half of Vibe-Trading's grounding gate, without
    the model. The session records the values it really read (a quote, a
    fill, a computed PnL) and then asks whether a number it is about to
    print is one of them. It cannot tell a *correct* number from a
    *plausible* one -- only a traceable one from an untraceable one, and
    it reports the difference rather than hiding it.
    """

    def __init__(self):
        # Two indexes over the same observations: exact, and rounded to
        # N significant digits. The second exists so a figure printed at
        # fewer digits than it was computed ("0.3" for 0.1+0.2) still
        # resolves. It is a set lookup, not a scan, so auditing a long
        # report stays linear in the report rather than in the report
        # times the session.
        self._values = set()
        self._sig = set()
        self._labels = {}
        self._sig_labels = {}

    def observe(self, label, value):
        """Record one observed number. Returns False for unusable input."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
        if math.isnan(v) or math.isinf(v):
            return False
        key = round(v, 10)
        self._values.add(key)
        sig = _sig_key(v)
        if sig is not None:
            self._sig.add(sig)
            self._sig_labels.setdefault(sig, set()).add(str(label))
        self._labels.setdefault(key, []).append(str(label))
        return True

    def observe_many(self, label, values):
        n = 0
        for v in values or ():
            if self.observe(label, v):
                n += 1
        return n

    def traceable(self, value):
        """Is this number one we observed?"""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
        if math.isnan(v) or math.isinf(v):
            return False
        if round(v, 10) in self._values:
            return True
        sig = _sig_key(v)
        return sig is not None and sig in self._sig

    def label_for(self, value):
        try:
            v = round(float(value), 10)
        except (TypeError, ValueError):
            return None
        labels = self._labels.get(v)
        if labels:
            return ", ".join(labels)
        # Fall back through the display-rounded index so a rendered
        # figure can still name the observation it came from.
        sig = _sig_key(v)
        if sig is None:
            return None
        names = self._sig_labels.get(sig)
        return ", ".join(sorted(names)) if names else None

    def __len__(self):
        return len(self._values)

    def audit_text(self, text, ignore_ordinals=True):
        """Return (untraceable, traceable) figures found in ``text``.

        Both lists hold ``(token, label)`` pairs; the label is the
        observation the number traces to, or None. Numbers inside a
        longer identifier (``AAPL2``, ``v1.2.3``, ``ETH-2024``) and
        date/time and ordinal shapes are skipped, because flagging
        "Rule 2 of 5" as an invented figure trains the reader to ignore
        the gate -- and a gate that is always crying wolf is off.
        """
        scanned = _blank_shapes(str(text or ""), _NOT_FIGURE_SHAPES)
        if ignore_ordinals:
            scanned = _blank_shapes(scanned, _ORDINAL_SHAPES)
        untraceable, traceable = [], []
        for match in _NUM_RE.finditer(scanned):
            token = match.group(0)
            try:
                value = float(token.replace(",", ""))
            except (TypeError, ValueError):
                continue
            if self.traceable(value):
                traceable.append((token, self.label_for(value)))
            else:
                untraceable.append((token, None))
        return untraceable, traceable

    def verdict(self, text, ignore_ordinals=True):
        """One-word summary for a caller rendering to a human."""
        untraceable, _ = self.audit_text(text, ignore_ordinals)
        if not untraceable:
            return "grounded"
        return "ungrounded:{}".format(
            ",".join(token for token, _ in untraceable[:5]))


# ============================================================== attribution
def attribute(profile, real_roundtrips, counters=None):
    """Compare a profile's counters to what actually happened.

    ``counters`` maps symbol -> {'fired', 'taken', 'pnl'} from a scan the
    caller ran. The point of the comparison is the *gap*: a rule that
    fires constantly and is almost never taken describes a behaviour the
    trader does not have.
    """
    counters = counters or {}
    rows, taken_total, fired_total = [], 0.0, 0
    for rule in profile.rules:
        fired, taken, pnl = 0, 0, 0.0
        for stats in counters.values():
            by_rule = stats.get("by_rule") or {}
            entry = by_rule.get(rule.rule_id) or {}
            fired += int(entry.get("fired", 0))
            taken += int(entry.get("taken", 0))
            pnl += float(entry.get("pnl", 0.0))
        adherence = (taken / fired) if fired else None
        rows.append({
            "rule_id": rule.rule_id,
            "text": rule.text,
            "fired": fired,
            "taken": taken,
            "adherence": round(adherence, 4) if adherence is not None else None,
            "realised_pnl": round(pnl, 2),
        })
        taken_total += taken
        fired_total += fired
    overall = (taken_total / fired_total) if fired_total else None
    return {
        "rules": rows,
        "fired_total": fired_total,
        "taken_total": taken_total,
        "adherence": round(overall, 4) if overall is not None else None,
        "realised_pnl": round(
            sum(r["realised_pnl"] for r in rows), 2),
        "note": ("adherence is taken/fired; null means the rule has not "
                 "fired yet, which is not evidence either way"),
    }
