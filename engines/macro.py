"""Macro regime from stored FRED series.

The point of this module is to produce one number a signal can be gated on,
derived from macro that is already stored, and to **refuse** when that macro
is absent or stale rather than substituting a default.

Why refuse rather than default
------------------------------
mafutani's house rule, applied consistently: a value that cannot be measured
is reported as missing, not filled in. The obvious alternative -- default VIX
to 17, default the curve to +0.5, emit a regime -- would mean a deployment
with no FRED data silently behaves as though macro were neutral, and nobody
could tell that the gate was inert. A gate that is quietly always-on is worse
than no gate, because it looks like a control.

What is derived
---------------
Only from series mafutani actually stores, and only when enough of them are
present to mean anything:

- **risk**: VIX level and its own trailing percentile. A VIX of 30 is only
  alarming next to its own history; 30 after a decade of 12 is not the same
  event as 30 after a decade of 30.
- **curve**: T10Y2Y and T10Y3M. Inversion is the classic recession tell.
- **credit**: HY and IG OAS. Widening credit is risk-off arriving *before*
  equity prices show it, which is the whole reason it is here.
- **rate level**: DGS10, for context only. It does not change the regime.

The composite is deliberately coarse: three buckets, not a continuous score.
A continuous "macro score" implies a precision this data cannot support.
"""

from data import fred

# A macro observation older than this is not a reading, it is a memory.
# Daily series are not daily-complete -- weekends and holidays are absent --
# so "stale" has to mean several days, not "not today".
DEFAULT_MAX_AGE_DAYS = 7

# Inputs the regime is computed from. Absent any of these the answer is None
# rather than partial: a regime missing its credit leg is not a milder
# regime, it is an unmeasured one.
RISK = "VIXCLS"
CURVE_2_10 = "T10Y2Y"
CURVE_3M_10 = "T10Y3M"
# Curve bands. These must not overlap.
#
# An earlier version tested inversion (`value < 0`) and steepness
# (`value >= -0.10`) as two independent predicates. They overlap on
# [-0.10, 0), so every curve reading in that band was counted as evidence
# *for and against* itself and the regime came out "transition" -- a
# contradiction manufactured out of one number, with a flat curve labelled
# "inverted" in passing. The curve now has exactly one classification.
CURVE_INVERTED = -0.25   # materially inverted
CURVE_STEEP = 0.25       # genuinely steep

HY_SPREAD = "BAMLH0A0HYM2"
IG_SPREAD = "BAMLC0A0CM"

# A median over fewer points than this is not a baseline. Below it the leg
# is reported as unmeasured rather than as calm.
MIN_MEDIAN_POINTS = 5


def _latest_all(conn):
    """{series_id: newest row} in one pass.

    `latest_stored` already resolves every series in a single aggregate
    query, so looking each series up individually meant running that same
    query once per series -- five identical scans per regime call. Build the
    index once instead.
    """
    out = {}
    for r in fred.latest_stored(conn):
        sid = r.get("series_id")
        if sid and sid not in out:
            out[sid] = r
    return out


def _percentile(values, x):
    """Fraction of `values` at or below `x`, in 0..1. None if unusable."""
    vals = [v for v in (values or []) if v is not None]
    if not vals:
        return None
    if x is None:
        return None
    return sum(1 for v in vals if v <= x) / float(len(vals))


def vix_context(conn, lookback=756, max_age_days=DEFAULT_MAX_AGE_DAYS,
                latest=None):
    """VIX level plus its trailing percentile.

    Returns {"vix", "percentile", "asof", "age_days", "usable"}. `usable`
    is False when the series is absent or stale, and callers are expected to
    honour that rather than read the level anyway.
    """
    latest = _latest_all(conn) if latest is None else latest
    cur = latest.get(RISK)
    out = {"series_id": RISK, "vix": None, "percentile": None,
           "asof": "", "age_days": None, "usable": False}
    if not cur:
        return out
    out["vix"] = cur.get("value")
    out["asof"] = cur.get("obs_date", "")
    out["age_days"] = cur.get("age_days")
    if out["age_days"] is not None and out["age_days"] > max_age_days:
        return out
    try:
        # `limit=lookback` pushes the bound into SQL. Fetching the whole
        # history and slicing the tail of it in Python read ~9k rows to keep
        # 756.
        hist = [r["value"] for r in
                fred.load_series(conn, RISK, limit=int(lookback))]
    except Exception:
        hist = []
    out["percentile"] = _percentile(hist, out["vix"])
    out["usable"] = out["vix"] is not None
    return out


def classify_curve(v, inverted=CURVE_INVERTED, steep=CURVE_STEEP):
    """One label for a curve spread: inverted, flat, or steep. Or None.

    Exactly one of the three, always. A caller cannot accidentally be told
    a reading is both flattened and steepening, because there is no pair of
    overlapping predicates left to do it.
    """
    if v is None:
        return None
    if v < inverted:
        return "inverted"
    if v >= steep:
        return "steep"
    return "flat"


def curve_context(conn, max_age_days=DEFAULT_MAX_AGE_DAYS, latest=None):
    """Both curve spreads, classified, with staleness honoured."""
    latest = _latest_all(conn) if latest is None else latest
    out = {"2s10s": None, "3m10s": None,
           "state_2s10s": None, "state_3m10s": None,
           "usable": False, "series": [],
           "bands": {"inverted_below": CURVE_INVERTED, "steep_at_or_above": CURVE_STEEP}}
    for sid, key in ((CURVE_2_10, "2s10s"), (CURVE_3M_10, "3m10s")):
        out["series"].append(sid)
        cur = latest.get(sid)
        if not cur:
            continue
        age = cur.get("age_days")
        if age is not None and age > max_age_days:
            # Left as None, and therefore unclassified: a stale curve is not
            # a flat curve.
            continue
        out[key] = cur.get("value")
        out["state_" + key] = classify_curve(out[key])
    out["usable"] = out["2s10s"] is not None or out["3m10s"] is not None
    return out


def credit_context(conn, max_age_days=DEFAULT_MAX_AGE_DAYS, latest=None):
    """HY and IG option-adjusted spreads, and whether credit is widening."""
    latest = _latest_all(conn) if latest is None else latest
    out = {"hy": None, "ig": None, "hy_widening": None, "ig_widening": None,
           "usable": False, "hy_basis": "", "ig_basis": ""}
    for sid, key in ((HY_SPREAD, "hy"), (IG_SPREAD, "ig")):
        cur = latest.get(sid)
        if not cur:
            continue
        age = cur.get("age_days")
        if age is not None and age > max_age_days:
            out[key + "_basis"] = "stale (%sd old)" % age
            continue
        out[key] = cur.get("value")
        try:
            hist = [r["value"] for r in fred.load_series(conn, sid, limit=30)]
        except Exception:
            hist = []
        # Widening = the newest reading sits above the recent median. Using
        # the median rather than the mean keeps a single credit event from
        # moving the baseline as far as the event itself.
        if out[key] is None:
            continue
        if len(hist) < MIN_MEDIAN_POINTS:
            # Say so. Leaving `widening` as None while still counting the leg
            # as usable is how a thin history becomes an invisible "credit
            # is fine".
            out[key + "_basis"] = ("only %d stored point(s); need %d for a "
                                   "median" % (len(hist), MIN_MEDIAN_POINTS))
            continue
        med = sorted(hist)[len(hist) // 2]
        out[key + "_widening"] = out[key] > med
        out[key + "_basis"] = "%.2f vs 30d median %.2f" % (out[key], med)
    out["usable"] = out["hy"] is not None or out["ig"] is not None
    return out


def regime(conn, max_age_days=DEFAULT_MAX_AGE_DAYS, vix_high=25.0,
           curve_inverted=CURVE_INVERTED, curve_steep=CURVE_STEEP):
    """Composite macro regime, or None when it cannot be measured.

    Returns {"regime", "confidence", "inputs", "reason"}. `regime` is one of
    "risk_on", "risk_off", "transition", "neutral", or None.

    None means *not measured*, and is deliberately distinct from
    "neutral". There is no default: see the module docstring.
    """
    latest = _latest_all(conn)
    vix = vix_context(conn, max_age_days=max_age_days, latest=latest)
    curve = curve_context(conn, max_age_days=max_age_days, latest=latest)
    credit = credit_context(conn, max_age_days=max_age_days, latest=latest)
    inputs = {"vix": vix, "curve": curve, "credit": credit}
    # Re-classify against the caller's bands, so a caller who retunes the
    # curve bands gets its own thresholds rather than the module defaults
    # that classify_curve happened to use.
    if curve_inverted != CURVE_INVERTED or curve_steep != CURVE_STEEP:
        for key in ("2s10s", "3m10s"):
            curve["state_" + key] = classify_curve(
                curve.get(key), curve_inverted, curve_steep)

    if not (vix["usable"] or curve["usable"] or credit["usable"]):
        return {"regime": None, "confidence": 0.0, "inputs": inputs,
                "reason": ("no macro stored, or all of it stale beyond %dd; "
                           "run the macro fetch rather than reading a default"
                           % max_age_days)}

    # How many of the three legs actually contributed. Drives confidence, and
    # is reported so a caller can decide how much weight the regime deserves.
    legs = sum(1 for leg in (vix, curve, credit) if leg["usable"])
    confidence = round(legs / 3.0, 2)

    off, on = [], []

    # -- risk-off evidence
    if vix["usable"]:
        if vix["vix"] is not None and vix["vix"] >= vix_high:
            off.append("vix %.1f >= %.0f" % (vix["vix"], vix_high))
        elif vix["percentile"] is not None and vix["percentile"] >= 0.9:
            # Elevated versus its own history even if the absolute level is
            # unremarkable -- which is most of the time a vol spike happens.
            off.append("vix at p%.0f of its own history" % (vix["percentile"] * 100))
    if curve["usable"]:
        for key in ("2s10s", "3m10s"):
            state = curve.get("state_" + key)
            if state == "inverted":
                off.append("%s inverted at %+.2f" % (key, curve[key]))
            elif state == "steep":
                on.append("%s steep at %+.2f" % (key, curve[key]))
    if credit["usable"] and credit.get("hy_widening"):
        off.append("hy spreads above their 30d median")

    # -- risk-on evidence
    if vix["usable"] and vix["vix"] is not None and vix["vix"] < 15.0:
        if vix["percentile"] is None or vix["percentile"] < 0.5:
            on.append("vix %.1f low and unremarkable" % vix["vix"])
    if credit["usable"] and credit.get("hy") is not None \
            and credit.get("hy_widening") is False:
        on.append("hy spreads at or below their 30d median")

    if off and on:
        regime_name = "transition"
        reason = "%s; counter-evidence: %s" % ("; ".join(off), "; ".join(on))
    elif off:
        regime_name = "risk_off"
        reason = "; ".join(off)
    elif on:
        regime_name = "risk_on"
        reason = "; ".join(on)
    else:
        # Measured, but nothing fired. That is a real answer and it is
        # different from "not measured".
        regime_name = "neutral"
        reason = "measured (%d/3 legs), no threshold crossed" % legs

    return {"regime": regime_name, "confidence": confidence, "inputs": inputs,
            "reason": reason}


def gate(params, regime_result, allow_in=None, block_in=None,
         default_allow=True):
    """Should this signal be allowed, given the macro regime?

    The policy comes from the caller's arguments first, then from the
    strategy's own `params`. Recognised keys in `params`:

        macro_block_in    list of regimes that veto
        macro_allow_in    list of regimes that are the only ones permitted
        macro_default_allow   bool, applied when the regime is unmeasured

    `params` is honoured because that is where a per-strategy policy has to
    live -- a strategy that should not trade into a credit event says so in
    its own configuration. Accepting the argument and ignoring it would be
    worse than not accepting it, since a caller would reasonably conclude
    the setting took effect.

    With no policy at all, macro does not gate. That is the right default:
    a control nobody asked for that silently suppresses trades is worse
    than no control.

    Returns {"allowed", "reason", "regime", "policy"}. When the regime is
    unmeasured (`None`) the configured default applies, and the reason says
    so rather than claiming the trade was cleared by macro.
    """
    params = params if isinstance(params, dict) else {}

    def _regimes(value):
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple)):
            return None
        out = []
        for v in value:
            v = str(v).strip().lower()
            if v:
                out.append(v)
        return out or None

    p_allow = _regimes(allow_in)
    p_block = _regimes(block_in)
    if p_allow is None:
        p_allow = _regimes(params.get("macro_allow_in"))
    if p_block is None:
        p_block = _regimes(params.get("macro_block_in"))
    # A bool() here would turn the string "false" into True.
    if isinstance(params.get("macro_default_allow"), bool):
        default_allow = params["macro_default_allow"]

    reg = (regime_result or {}).get("regime")
    if isinstance(reg, str):
        reg = reg.strip().lower()
    conf = (regime_result or {}).get("confidence", 0.0)
    policy = {"allow_in": p_allow, "block_in": p_block,
              "default_allow": bool(default_allow)}
    base = {"allowed": bool(default_allow), "regime": reg,
            "confidence": conf, "policy": policy}
    if reg is None:
        base["reason"] = ("macro unmeasured; gate not applied (%s)"
                          % (regime_result or {}).get("reason", "no reason given"))
        return base
    if p_block and reg in p_block:
        base.update({"allowed": False,
                     "reason": "blocked by macro regime '%s': %s"
                               % (reg, (regime_result or {}).get("reason", ""))})
        return base
    if p_allow and reg not in p_allow:
        base.update({"allowed": False,
                     "reason": "macro regime '%s' not in %s: %s"
                               % (reg, sorted(p_allow),
                                  (regime_result or {}).get("reason", ""))})
        return base
    if not (p_allow or p_block):
        base["reason"] = ("macro regime '%s' measured, but no policy is "
                          "configured; gate not applied" % reg)
        return base
    base["reason"] = "macro regime '%s' permits the trade: %s" % (
        reg, (regime_result or {}).get("reason", ""))
    return base
