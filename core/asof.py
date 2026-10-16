"""Point-in-time discipline for anything dated. Stdlib only, no imports.

What this is for
----------------
A backtest is a claim about what could have been known at a moment in the
past. That claim is false the moment a query returns something published
after it, and the failure is silent: the number still looks reasonable,
it is just better than the truth.

Three things go wrong, and they are different problems:

1. **Look-ahead.** A query is asked for more than the trade date covers.
   `as_of` clamps it.
2. **Undated items.** A news headline with no timestamp. Including it is a
   guess; excluding it in a *live* window loses real information. The rule
   here is the one that matters for backtests: an item with no timestamp is
   kept only when the window reaches the present, because
   *"a backtest cannot prove it is not from the future"*.
3. **Coverage gaps.** A feed that could not reach the requested window
   returns an empty result, which is indistinguishable from "nothing
   happened". Those are opposite claims and only one of them is usually
   true. `coverage_gap` reports the difference as *unavailable*.

Rule 3 is the important one, and it is mafutani's house rule arrived at from
a different direction: a value that cannot be measured is reported as
missing, not filled in. An empty list from a feed that never looked is a
fabricated observation.

Epoch seconds throughout, because that is what the database stores. Strings
are accepted and parsed, since the FRED/COT stores are keyed by ISO date.
"""

import calendar
import time

DAY = 86400

__all__ = [
    "DAY", "parse_date", "as_of", "in_window", "coverage_gap",
    "window_days", "clamp_window", "MAX_FUTURE_SKEW_S",
]


# How far past "now" a timestamp may sit and still be treated as now.
# Some vendors stamp in local time or round up to the next publication, so
# a token amount of skew is normal. Anything beyond this is not a clock
# difference, it is a leak, and clamping it silently would hide that.
MAX_FUTURE_SKEW_S = 36 * 3600


def parse_date(value):
    """Epoch seconds from an epoch int or an ISO date/datetime string.

    Returns None for anything unparseable. Never raises and never guesses:
    a date that cannot be read is a date that must not be defaulted, because
    defaulting it is how an undated item becomes a present-day one.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%Y/%m/%d", "%Y%m%d"):
        try:
            return int(calendar.timegm(time.strptime(text, fmt)))
        except (ValueError, TypeError):
            continue
    return None


def as_of(requested, now=None, skew=MAX_FUTURE_SKEW_S):
    """Clamp `requested` so it can never point past `now`.

    A request for a future window is a bug in the caller or a symptom of
    look-ahead. Either way the answer must not be the future.

    Within `skew` of now the value passes through: publication stamps are
    routinely rounded forward. Beyond that it is clamped, so an absurd value
    is visibly wrong rather than silently accepted.

    Returns (clamped_ts, was_clamped).
    """
    now = int(now if now is not None else time.time())
    ts = parse_date(requested)
    if ts is None:
        return now, True
    limit = now + max(0, int(skew))
    if ts > limit:
        return now, True
    return ts, False


def in_window(pub, start, end, now=None, undated=None):
    """Is `pub` inside `[start, end + 1 day)`?

    `pub` None means undated. An undated item is kept **only** when the
    window reaches the present, because otherwise a backtest is asserting
    that a timestamp-less item did not come from the future, which is not a
    thing it can know.

    `undated` can override: pass True to keep undated items anyway (a live
    window), False to drop them, None for the rule above.

    Returns (bool, reason). The reason is empty when the item is in window,
    and otherwise says which rule excluded it -- so a caller can report
    "dropped 40 undated items" instead of quietly returning fewer rows.
    """
    lo = parse_date(start)
    hi = parse_date(end)
    if lo is None and hi is None:
        return True, ""
    if hi is not None:
        hi = hi + DAY          # half-open at midnight after `end`
    ts = parse_date(pub)
    if ts is None:
        if undated is not None:
            return bool(undated), ("kept undated (forced)" if undated
                                   else "dropped undated (forced)")
        now = int(now if now is not None else time.time())
        reaches_present = hi is None or hi >= now - DAY
        if reaches_present:
            return True, "kept undated: window reaches the present"
        return False, ("dropped undated: window ends before the present, "
                       "so its date cannot be proven to be inside")
    if lo is not None and ts < lo:
        return False, "before window start"
    if hi is not None and ts >= hi:
        return False, "at or after window end"
    return True, ""


def coverage_gap(observed, start, end, now=None, tail_lookback=None):
    """Was the window actually observed, or merely asked for?

    `observed` is the oldest timestamp the feed returned. Some feeds (Yahoo
    news, social) return their latest N items whatever window was asked for,
    so an empty result over a window they never covered claims an absence
    nobody saw.

    A window counts as observed when it ends by today and the feed could
    actually serve it. Two ways to establish that:

    - `observed`: the oldest item the feed returned. It bounds coverage for a
      feed that returns newest-first and is unbroken in time.
    - `tail_lookback`: a feed that declares a fixed lookback can serve
      nothing older than `now - tail_lookback`, regardless of what it
      returned. This is the stronger of the two claims, so when both are
      given the *later* reachable start is the binding one.

    Anything else is a gap, returned as a sentence rather than as None.

    The caveat on `observed`, which matters: a relevance-ranked or merged feed
    does not return its items in time order, so its oldest item does not
    bound coverage at all. Passing `tail_lookback` for those, or passing the
    oldest item only for a feed you know is chronological, is the difference
    between a coverage claim and a guess.
    """
    lo = parse_date(start)
    hi = parse_date(end)
    if lo is None or hi is None:
        return "window not specified: no coverage can be claimed"
    now = int(now if now is not None else time.time())
    if hi > now:
        return ("window ends %s in the future (%d days); nothing can have "
                "been observed there yet"
                % (_iso(hi), (hi - now) // DAY))
    if tail_lookback:
        reachable = now - int(tail_lookback)
        if reachable > lo:
            return ("feed reaches back %d days, to %s, but the window opens "
                    "%s; anything earlier is unobserved, not absent"
                    % (int(tail_lookback) // DAY, _iso(reachable), _iso(lo)))
    oldest = parse_date(observed)
    if oldest is None:
        return ("no observations returned, so absence of data in "
                "[%s, %s] is unknown rather than established"
                % (_iso(lo), _iso(hi)))
    if oldest > lo:
        return ("feed coverage starts %s, after the window opens %s; "
                "anything before that is unobserved, not absent"
                % (_iso(oldest), _iso(lo)))
    return None


def window_days(start, end):
    """Whole days spanned by a window, or None if either end is unreadable."""
    lo, hi = parse_date(start), parse_date(end)
    if lo is None or hi is None:
        return None
    return max(0, (hi - lo) // DAY)


def clamp_window(start, end, max_days=180, now=None):
    """Bound a window to `max_days`, ending at `now`.

    Returns (start, end). The end moves, not the window length, because the
    recent end is the part a caller cares about.
    """
    now = int(now if now is not None else time.time())
    hi = parse_date(end)
    hi = now if hi is None else min(hi, now)
    span = int(max_days) * DAY
    lo = parse_date(start)
    if lo is None or hi - lo > span:
        lo = hi - span
    return lo, hi


def _iso(ts):
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ts)))
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"
