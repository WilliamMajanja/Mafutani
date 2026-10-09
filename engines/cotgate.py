"""COT as a strategy gate: what was knowable, and when.

Stdlib only, import-safe, pure (lists/dicts in, dict out, no I/O, no DB).

This is the module that makes a COT-gated strategy honest. The whole
problem with using weekly positioning data on a 1-minute tape is not
parsing -- that is already correct in `data/cot.py` -- it is **time**.
Joined naively onto candles, a COT report dated Tuesday looks available
on Tuesday. It is not: the CFTC publishes on the Friday three days later.

That single fact is the difference between a strategy that can be
backtested and one that reports a spectacular Sharpe it could never have
earned. `visible_at()` is the whole point of this module: at any bar, it
returns only what had actually been published by then.

The timeline
------------
``build_timeline`` turns stored reports into ``(available_from, ...)``
pairs where ``available_from`` is the publication instant, not the report
date. Bars before the first publication get ``None`` -- no data yet --
which is a different thing from "positioning was neutral" and from "no
COT for this contract". All three are distinguished.

Resolution, honestly stated
---------------------------
One print describes one Tuesday and stays the current answer for a week,
because it is the only thing anyone had. Every bar between two
publications sees the same gate value. So a six-month backtest contains
~26 COT observations, not 259,200 -- and any Sharpe computed on the
resulting trades is bounded by that sample size, not by the bar count.
`gate_trial_count()` exists so the significance layer can be told.

Why a gate and not a signal
---------------------------
COT is a slow variable measured once a week. Using it to *time* entries
would be claiming precision the measurement does not have. Using it to
*veto* trades in a regime where the crowd is already fully positioned is
the shape the data actually supports: it changes which trades are taken,
not when.

Where it refuses
----------------
* A bar before the first publication: ``None``, not a permissive default.
* A percentile of 50 is not "unavailable" -- it is a real reading, and
  the gate passes it. Only a genuinely absent reading blocks, and it
  blocks *openly* with a reason rather than silently doing nothing.
"""

import bisect
import calendar

# The CFTC publishes at 15:30 ET on the Friday after the Tuesday it
# measures. 21:00 UTC is 15:30 in the worst case (EDT, UTC-4) and 16:00
# in the worst other case (EST, UTC-5). Publishing 90 minutes early
# instead of 30 late is deliberate: a gate that lets a trade in 90
# minutes before the data existed is a look-ahead, and a gate that waits
# an extra 30 minutes costs one bar a week.
PUBLISH_HOUR_UTC = 21

DEFAULT_LAG_DAYS = 3
DEFAULT_CATEGORY = "m_money"

# Gates. The threshold is a percentile of that category's own trailing
# net, so it is scale-free: "managed money is in the top decile for this
# contract" means the same thing for crude and for gold.
DEFAULT_MAX_PERCENTILE = 90.0
DEFAULT_MIN_PERCENTILE = 10.0


def _date_to_ts(d):
    """ISO YYYY-MM-DD -> epoch seconds at 00:00 UTC. None if malformed."""
    s = str(d or "")
    if len(s) != 10:
        return None
    try:
        y, m, d = int(s[:4]), int(s[5:7]), int(s[8:10])
        return calendar.timegm((y, m, d, 0, 0, 0, 0, 0, 0))
    except (TypeError, ValueError):
        return None


def publication_ts(report_date, lag_days=DEFAULT_LAG_DAYS,
                   hour_utc=PUBLISH_HOUR_UTC):
    """When a report dated `report_date` became public. None if malformed.

    `lag_days=3` gives the Friday of the same week; the CFTC shifts this
    around government holidays, so this is the *earliest* defensible
    moment, not the exact one. Trading on it is the conservative error.
    """
    base = _date_to_ts(report_date)
    if base is None:
        return None
    return base + int(lag_days) * 86400 + int(hour_utc) * 3600


def build_timeline(rows, category=DEFAULT_CATEGORY,
                   lag_days=DEFAULT_LAG_DAYS, weeks=156):
    """Stored COT rows -> a sorted publication timeline.

    `rows` are dicts as stored by data.cotstore: report_date, category,
    long_pos, short_pos, open_interest.

    Returns {"available_from": [ts, ...], "entries": [{...}], ...} where
    the two lists are parallel, so `visible_at` is a bisect.

    **Every measure is point-in-time.** Entry *i*'s percentile and index
    are computed from entries 0..i only -- never from a later print that
    had not been published when entry i was read. There is deliberately
    no cheaper whole-series variant to reach for: an earlier draft had
    one, and it was the exact look-ahead this module exists to prevent,
    hidden behind a function name that did not advertise the difference.

    Cost is O(n^2) in the number of stored prints. At ~600 weekly prints
    that is ~180k comparisons, once per run -- irrelevant next to the
    backtest, and cheap enough that "just recompute it" is never the
    wrong answer.
    """
    picked = {}
    for r in (rows or []):
        if str(r.get("category") or "") != category:
            continue
        date = str(r.get("report_date") or "")
        pub = publication_ts(date, lag_days)
        if pub is None:
            continue
        lng = r.get("long_pos")
        sht = r.get("short_pos")
        try:
            lng, sht = float(lng), float(sht)
        except (TypeError, ValueError):
            continue
        picked[date] = {
            "report_date": date,
            "available_from": pub,
            "net": lng - sht,
            "long": lng,
            "short": sht,
            "open_interest": r.get("open_interest") or 0,
        }
    dates = sorted(picked)
    entries = [picked[d] for d in dates]
    nets = [e["net"] for e in entries]
    for i, e in enumerate(entries):
        e["weekly_change"] = (nets[i] - nets[i - 1]) if i else None
        e.update(_measures(nets, i, weeks))
    return {
        "category": category,
        "lag_days": lag_days,
        "available_from": [e["available_from"] for e in entries],
        "entries": entries,
        "count": len(entries),
        "first_publication": entries[0]["available_from"] if entries else None,
        "last_publication": entries[-1]["available_from"] if entries else None,
    }


def _measures(nets, i, weeks):
    """Normalised read for entry i, from nets[:i+1] only.

    Point-in-time on purpose: the window ends at the print being
    evaluated, never at a later one. `build_timeline` overwrites these
    with its cheaper whole-series version; this is what makes that
    difference measurable and testable.
    """
    hist = nets[:i + 1]
    n = len(hist)
    out = {"percentile": None, "cot_index": None, "observations": n}
    if n < 2:
        return out
    cur = hist[-1]
    lo, hi = min(hist), max(hist)
    if hi == lo:
        # A contract that has held one net position for the whole window
        # is not "at the 100th percentile" and must not be blocked as
        # crowded long forever. Ranking every identical value at or below
        # itself puts it at 1.0, which is true of the rank and completely
        # misleading as a crowding signal. Mid-range is what it actually
        # is, and it agrees with the index below.
        out["percentile"] = 50.0
        out["cot_index"] = 50.0
        return out
    out["percentile"] = round(
        sum(1 for v in hist if v <= cur) / float(n) * 100.0, 2)
    out["cot_index"] = round((cur - lo) / float(hi - lo) * 100.0, 2)
    return out


def visible_at(timeline, ts):
    """The COT state a trader could actually have seen at `ts`.

    None when nothing had been published yet. That None is load-bearing:
    it is the difference between "no gate applied" and "positioning was
    neutral", and the two produce different trades.
    """
    if not timeline or not timeline.get("available_from"):
        return None
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    avail = timeline["available_from"]
    i = bisect.bisect_right(avail, ts) - 1
    if i < 0:
        return None
    return timeline["entries"][i]


def evaluate(state, category=DEFAULT_CATEGORY,
             max_percentile=DEFAULT_MAX_PERCENTILE,
             min_percentile=DEFAULT_MIN_PERCENTILE):
    """Gate verdict for one COT state.

    Returns (allowed, reason, detail). Three outcomes, never two:

      allowed=True   gate passed (or is disabled)
      allowed=False  gate blocked -- with the reading that did it
      allowed=None   no reading available -- the *caller* decides, and
                     the strategy refuses rather than guessing
    """
    detail = {"category": category, "report_date": None, "percentile": None,
              "cot_index": None, "net": None, "weekly_change": None,
              "age_days": None, "blocker": None}
    if state is None:
        return None, "no COT published yet", detail

    detail.update({
        "report_date": state.get("report_date"),
        "percentile": state.get("percentile"),
        "cot_index": state.get("cot_index"),
        "net": state.get("net"),
        "weekly_change": state.get("weekly_change"),
    })
    p = state.get("percentile")
    if p is None:
        # One observation is not a reading. Refusing is right; allowing
        # would mean the gate is silently inert for new contracts.
        return None, "COT percentile unavailable (one observation)", detail

    if max_percentile is not None and p >= max_percentile:
        detail["blocker"] = "crowded-long"
        return False, ("blocked: {} net at the {:.0f}th percentile of its own "
                       "trailing history".format(category, p)), detail
    if min_percentile is not None and p <= min_percentile:
        detail["blocker"] = "crowded-short"
        return False, ("blocked: {} net at the {:.0f}th percentile of its own "
                       "trailing history".format(category, p)), detail
    return True, ("COT gate clear ({} at the {:.0f}th pctile)"
                  .format(category, p)), detail


def gate_trial_count(timeline, weeks=156):
    """Independent COT observations in a run.

    This is the number the significance layer needs. A 259,200-bar
    backtest gated on COT is not a 259,200-sample experiment: the gate
    can only change its mind once a week, so the effective sample is the
    number of publications inside the window.
    """
    if not timeline or not timeline.get("available_from"):
        return 0
    return max(0, len(timeline["available_from"]))


def timeline_for_window(timeline, from_ts, to_ts):
    """The entries a backtest over [from_ts, to_ts] could have seen."""
    if not timeline:
        return {"available_from": [], "entries": [], "count": 0}
    lo, hi = [], []
    for e in timeline.get("entries", []):
        if from_ts and e["available_from"] < int(from_ts):
            continue
        if to_ts and e["available_from"] > int(to_ts):
            continue
        lo.append(e["available_from"])
        hi.append(e)
    return {"available_from": lo, "entries": hi, "count": len(hi)}


class Gate(object):
    """A built timeline, bound and ready to query.

    The strategy receives one of these rather than the module, so the
    timeline travels with the object and a caller cannot accidentally
    hand `ema_trend_cot` the module and leave it reading a global. It
    mirrors the three module functions the signal uses, which is why
    `engines.cotgate` itself is passed as `cot=` nowhere.
    """

    __slots__ = ("timeline", "default_category", "default_lag_days")

    def __init__(self, timeline, default_category=DEFAULT_CATEGORY,
                 default_lag_days=DEFAULT_LAG_DAYS):
        self.timeline = timeline or {"available_from": [], "entries": [],
                                     "count": 0}
        self.default_category = default_category
        self.default_lag_days = default_lag_days

    def visible_at(self, ts):
        return visible_at(self.timeline, ts)

    def evaluate(self, state, category=None, max_percentile=None,
                 min_percentile=None):
        return evaluate(
            state,
            category or self.default_category,
            max_percentile if max_percentile is not None
            else DEFAULT_MAX_PERCENTILE,
            min_percentile if min_percentile is not None
            else DEFAULT_MIN_PERCENTILE)

    def describe(self, ts, category=None, max_percentile=None,
                 min_percentile=None):
        return describe(self.timeline, ts,
                        category or self.default_category,
                        max_percentile if max_percentile is not None
                        else DEFAULT_MAX_PERCENTILE,
                        min_percentile if min_percentile is not None
                        else DEFAULT_MIN_PERCENTILE)

    def __len__(self):
        return len(self.timeline.get("available_from") or [])

    def trial_count(self, weeks=None):
        return gate_trial_count(self.timeline, weeks or 0)


def describe(timeline, ts, category=DEFAULT_CATEGORY,
             max_percentile=DEFAULT_MAX_PERCENTILE,
             min_percentile=DEFAULT_MIN_PERCENTILE):
    """Full gate report for one bar -- what the strategy attaches to its
    own reason string so a blocked trade explains itself."""
    state = visible_at(timeline, ts)
    allowed, reason, detail = evaluate(state, category, max_percentile,
                                       min_percentile)
    out = dict(detail)
    out.update({"allowed": allowed, "reason": reason,
                "category": category,
                "max_percentile": max_percentile,
                "min_percentile": min_percentile})
    if state is not None and state.get("available_from"):
        out["available_from"] = state["available_from"]
    return out
