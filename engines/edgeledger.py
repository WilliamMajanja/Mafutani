"""Append-only evidence ledger and promotion gate. Stdlib only.

Why a ledger and not a flag
---------------------------
`strategies.mode` is `wired`/`stub` -- a statement about whether code exists.
That is the wrong axis entirely: a fully wired strategy with no edge and a
fully wired strategy with a deflated Sharpe of 0.99 are the same row.

This records what was actually measured, when, under which engine version,
and how many trials had been run by then -- so that "does this strategy have
an edge" has an answer that does not depend on who remembers running it.

Four properties, each of which is the whole point:

**Append-only.** Evidence rows are never updated or deleted. A strategy's
measured history is the only record of what it was believed to be doing at
each point, and an in-place "corrected" row destroys the correction.

**Refuses rather than defaults.** No evidence means not promoted. Not
promoted is a real answer with a reason attached, and it is different from
"promoted", which is also what you get when nobody checks.

**Explicit adoption.** Nothing promotes itself. A stage change is an
operator action, matching `brokers/gods.py`: *"No auto-promotion. Adoption is
an explicit operator action."*

**Engine version is part of the evidence.** A run's numbers mean nothing
without the convention that produced them, and the fill convention changed
once already. Evidence computed under a superseded version cannot promote
anything, because it describes a different experiment.

What the gate requires
----------------------
Four independent conditions. Any of them failing is reported, not just the
first, because "why was this refused" should not require four runs to
answer:

1. At least one evidence row exists.
2. Its verdict is `edge likely real` (DSR >= 0.95 after deflation) -- not
   `edge plausible, unproven`, which is exactly the verdict that invites
   someone to trade it anyway.
3. Walk-forward degradation is at least `MIN_DEGRADATION`; a strategy that
   collapses out-of-sample has memorised its history.
4. Enough forward shadow cycles have been observed. Until `record_shadow`
   has run `MIN_SHADOW_CYCLES` times, promotion is refused -- see
   `engines.shadow`.
"""

import json
import time

from engines import significance as sig

__all__ = [
    "record_evidence", "record_shadow", "shadow_cycles", "latest", "history",
    "current_stage", "set_stage", "promotion_check", "summarize",
    "SCHEMA_SQL", "STAGES", "MIN_SHADOW_CYCLES", "MIN_DEGRADATION",
    "PROMOTING_VERDICT", "LEDGER_VERSION",
]

LEDGER_VERSION = 1

# Stage names, in the order an edge travels through them. `promoted` is the
# only one that authorises capital, and reaching it is an operator action.
STAGES = ("candidate", "validated", "shadowed", "promoted", "rejected")

# The only verdict that authorises anything. `edge plausible, unproven`
# exists to be read and then not acted on.
PROMOTING_VERDICT = "edge likely real"

# Out-of-sample score as a fraction of in-sample. Below this the strategy
# fitted the window it was shown.
MIN_DEGRADATION = 0.7

# Forward cycles in shadow before promotion is permitted. Evidence from a
# backtest is evidence about the past; this is evidence about now.
MIN_SHADOW_CYCLES = 20

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS edge_ledger (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  strategy       TEXT NOT NULL,
  symbol         TEXT NOT NULL DEFAULT '',
  params_json    TEXT NOT NULL DEFAULT '{}',
  kind           TEXT NOT NULL DEFAULT 'evidence',
  stage          TEXT NOT NULL DEFAULT 'candidate',
  engine_version INTEGER,
  verdict        TEXT NOT NULL DEFAULT '',
  summary_json   TEXT NOT NULL DEFAULT '{}',
  shadow_cycles  INTEGER NOT NULL DEFAULT 0,
  decided_by     TEXT NOT NULL DEFAULT '',
  note           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_edge_ledger_strategy
  ON edge_ledger(strategy, id DESC);
CREATE INDEX IF NOT EXISTS idx_edge_ledger_kind
  ON edge_ledger(kind, strategy, id DESC);
"""


def _now():
    try:
        return int(time.time())
    except Exception:
        return 0


def ensure_schema(conn):
    """Create the table if absent. Never destructive."""
    try:
        conn.executescript(SCHEMA_SQL)
        return True
    except Exception:
        return False


def _row_to_dict(row):
    if row is None:
        return None
    try:
        return {k: row[k] for k in row.keys()}
    except Exception:
        try:
            return dict(row)
        except Exception:
            return {}


def _parse(value, default):
    try:
        out = json.loads(value)
        return out if out is not None else default
    except Exception:
        return default


# --------------------------------------------------------------------------
# writing (append-only)
# --------------------------------------------------------------------------


def record_evidence(conn, strategy, summary, symbol="", params=None,
                    engine_version=None, note="", stage="candidate",
                    decided_by=""):
    """Append one evidence row. Returns the new id, or None if refused.

    `summary` is whatever the caller measured: an `engines.significance`
    report, a `engines.walkforward` result, or both. It is stored as given,
    because the gate reads specific fields and anything else belongs to
    whoever asked for it.

    Refuses when there is no strategy name. An evidence row nobody can
    attribute is worse than no row, since it looks like a record.
    """
    strategy = str(strategy or "").strip()
    if not strategy:
        return None
    if stage not in STAGES:
        stage = "candidate"
    summary = summary if isinstance(summary, dict) else {"raw": summary}
    verdict = str(summary.get("verdict") or "")
    if not verdict:
        verdict = str((summary.get("significance") or {}).get("verdict") or "")
    try:
        ensure_schema(conn)
        cur = conn.execute(
            "INSERT INTO edge_ledger (ts, strategy, symbol, params_json, kind,"
            " stage, engine_version, verdict, summary_json, shadow_cycles,"
            " decided_by, note)"
            " VALUES (?, ?, ?, ?, 'evidence', ?, ?, ?, ?, 0, ?, ?)",
            (_now(), strategy, str(symbol or ""),
             json.dumps(params or {}), stage,
             int(engine_version) if engine_version is not None else None,
             verdict, json.dumps(summary), str(decided_by or ""),
             str(note or "")))
        conn.commit()
        return int(cur.lastrowid)
    except Exception:
        return None


def record_shadow(conn, strategy, cycles_observed, symbol="", params=None,
                  note="", decided_by=""):
    """Append one forward shadow cycle.

    Shadow cycles are counted, not assumed: a cycle that was never run is
    indistinguishable from one that ran and produced nothing, so this
    records the observation rather than incrementing a counter blindly.

    Returns the cumulative count the ledger now holds, or None if refused.
    """
    strategy = str(strategy or "").strip()
    if not strategy:
        return None
    try:
        cycles = int(cycles_observed)
    except (TypeError, ValueError):
        return None
    if cycles < 0:
        return None
    prior = shadow_cycles(conn, strategy)
    total = prior + cycles
    try:
        ensure_schema(conn)
        cur = conn.execute(
            "INSERT INTO edge_ledger (ts, strategy, symbol, params_json, kind,"
            " stage, verdict, summary_json, shadow_cycles, decided_by, note)"
            " VALUES (?, ?, ?, ?, 'shadow', 'shadowed', '', ?, ?, ?, ?)",
            (_now(), strategy, str(symbol or ""),
             json.dumps(params or {}),
             json.dumps({"cycles_this_row": cycles, "cumulative": total}),
             total, str(decided_by or ""), str(note or "")))
        conn.commit()
        return total
    except Exception:
        return None


def shadow_cycles(conn, strategy):
    """Cumulative shadow cycles recorded for `strategy`."""
    try:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT shadow_cycles FROM edge_ledger WHERE kind = 'shadow'"
            " AND strategy = ? ORDER BY id DESC LIMIT 1",
            (str(strategy or ""),)).fetchone()
        return int(_row_to_dict(row).get("shadow_cycles") or 0)
    except Exception:
        return 0


def set_stage(conn, strategy, stage, decided_by="", note=""):
    """Move an edge to a new stage. Explicit operator action, always.

    Never promotes on its own evidence: `promotion_check` must pass first,
    and a `promoted` transition without a passing check is refused here
    rather than at some later point where the reason is no longer visible.

    Returns (ok, reason).
    """
    strategy = str(strategy or "").strip()
    if not strategy:
        return False, "no strategy named"
    if stage not in STAGES:
        return False, "unknown stage %r (known: %s)" % (stage, ", ".join(STAGES))
    if stage == "promoted":
        chk = promotion_check(conn, strategy)
        if not chk["eligible"]:
            # Recorded as a refusal *event*, not as the `rejected` stage. An
            # attempt that leaves no trace is indistinguishable from an
            # attempt never made, but calling it "rejected" would assert more
            # than the evidence supports -- a strategy short of cycles is not
            # rejected, it is unproven -- and would change the stage the
            # strategy is actually in.
            _append(conn, strategy, "refused_promotion", decided_by, note,
                    {"attempted_stage": "promoted", "eligible": False,
                     "reasons": chk["reasons"], "at": _now()})
            return False, "refused: " + "; ".join(chk["reasons"])
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO edge_ledger (ts, strategy, kind, stage, decided_by,"
            " note, summary_json) VALUES (?, ?, 'stage', ?, ?, ?, ?)",
            (_now(), strategy, stage, str(decided_by or ""),
             str(note or ""), json.dumps({"at": _now()})))
        conn.commit()
        return True, "stage set to %s" % stage
    except Exception as e:
        return False, "ledger write failed: %s" % e


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def latest(conn, strategy, kind="evidence"):
    """Most recent row of `kind` for `strategy`, with summary parsed."""
    try:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM edge_ledger WHERE kind = ? AND strategy = ?"
            " ORDER BY id DESC LIMIT 1", (str(kind or ""),
                                          str(strategy or ""))).fetchone()
    except Exception:
        return None
    d = _row_to_dict(row)
    if not d:
        return None
    d["summary"] = _parse(d.get("summary_json"), {})
    d["params"] = _parse(d.get("params_json"), {})
    return d


def history(conn, strategy=None, limit=100):
    """Newest-first rows, optionally for one strategy."""
    limit = max(1, min(int(limit or 100), 1000))
    try:
        ensure_schema(conn)
        if strategy:
            rows = conn.execute(
                "SELECT * FROM edge_ledger WHERE strategy = ?"
                " ORDER BY id DESC LIMIT ?", (str(strategy), limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM edge_ledger ORDER BY id DESC LIMIT ?",
                (limit,)).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        d = _row_to_dict(r)
        d["summary"] = _parse(d.get("summary_json"), {})
        d["params"] = _parse(d.get("params_json"), {})
        out.append(d)
    return out


def current_stage(conn, strategy):
    """The stage this edge is in.

    Read only from `kind = 'stage'` rows. A refused promotion and a recorded
    shadow cycle are both events, not states: neither moves the edge, and
    letting them do so would mean a strategy that somebody kept trying to
    promote looked rejected rather than unproven.
    """
    try:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT stage FROM edge_ledger WHERE kind = 'stage'"
            " AND strategy = ? ORDER BY id DESC LIMIT 1",
            (str(strategy or ""),)).fetchone()
    except Exception:
        return "candidate"
    stage = str((_row_to_dict(row) or {}).get("stage") or "")
    return stage if stage in STAGES else "candidate"


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------


def promotion_check(conn, strategy, shadow_min=MIN_SHADOW_CYCLES,
                    min_degradation=MIN_DEGRADATION):
    """May this edge be promoted to live? Every reason is reported.

    Returns {"eligible", "reasons", "checks", "stage", "verdict"}. When it
    is not eligible, `reasons` says what to do about it -- not merely that
    something failed.
    """
    strategy = str(strategy or "").strip()
    reasons = []
    checks = {}

    if not strategy:
        return {"eligible": False, "strategy": "", "stage": None,
                "verdict": None, "shadow_cycles": 0,
                "reasons": ["no strategy named"],
                "checks": {"has_evidence": False}}

    ev = latest(conn, strategy, kind="evidence")
    checks["has_evidence"] = ev is not None
    if ev is None:
        reasons.append(
            "no evidence recorded: run a backtest and record it before "
            "asking whether it can trade")

    verdict = (ev or {}).get("verdict") or ""
    summary = (ev or {}).get("summary") or {}
    wf_result = summary.get("walkforward") or {}

    # -- 2. verdict
    checks["verdict"] = verdict
    if ev is not None:
        if verdict != PROMOTING_VERDICT:
            reasons.append(
                "verdict is %r, and only %r promotes -- %s"
                % (verdict or "(none)", PROMOTING_VERDICT,
                   "run significance.edge_report on the trade list"
                   if not verdict else _why_not(verdict)))

    # -- 3. out-of-sample
    degradation = wf_result.get("degradation")
    checks["degradation"] = degradation
    checks["oos_verdict"] = wf_result.get("verdict") or ""
    if ev is not None:
        if degradation is None:
            reasons.append(
                "no out-of-sample score: run engines.walkforward.walk_forward "
                "and record its result with the evidence")
        elif float(degradation) < float(min_degradation):
            reasons.append(
                "out-of-sample is %.0f%% of in-sample (needs %.0f%%): %s"
                % (float(degradation) * 100.0, float(min_degradation) * 100.0,
                   wf_result.get("verdict") or "degrades out-of-sample"))

    # -- engine version
    ev_version = (ev or {}).get("engine_version")
    current = _current_engine_version()
    checks["engine_version"] = ev_version
    checks["current_engine_version"] = current
    if ev is not None and ev_version is not None and ev_version != current:
        reasons.append(
            "evidence was produced by engine version %s; this build is %s. "
            "The two describe different experiments, so the numbers are not "
            "comparable. Re-run the backtest." % (ev_version, current))

    # -- 4. forward cycles
    cycles = shadow_cycles(conn, strategy)
    checks["shadow_cycles"] = cycles
    checks["shadow_required"] = int(shadow_min)
    if cycles < int(shadow_min):
        reasons.append(
            "%d of %d forward shadow cycles observed. A backtest is "
            "evidence about the past; this is evidence about now."
            % (cycles, int(shadow_min)))

    stage = current_stage(conn, strategy)
    return {
        "eligible": not reasons,
        "strategy": strategy,
        "stage": stage,
        "verdict": verdict,
        "shadow_cycles": cycles,
        "reasons": reasons,
        "checks": checks,
        "promotion_block": ("promotion is an explicit operator action and "
                            "this edge is not eligible for it"
                            if reasons else
                            "eligible; record the adoption with "
                            "set_stage(..., 'promoted', decided_by=...)"),
    }


def _append(conn, strategy, kind, decided_by, note, summary):
    """One append. The single write path, so every row shape is identical.

    `stage` is set to the kind, which is correct for a stage row and inert
    for an event row: `current_stage` only reads `kind = 'stage'`.
    """
    try:
        ensure_schema(conn)
        conn.execute(
            "INSERT INTO edge_ledger (ts, strategy, kind, stage, decided_by,"
            " note, summary_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), strategy, kind,
             kind if kind in STAGES else "candidate",
             str(decided_by or ""), str(note or ""), json.dumps(summary or {})))
        conn.commit()
        return True
    except Exception:
        return False


def _why_not(verdict):
    if verdict == "edge plausible, unproven":
        return ("DSR is suggestive but below the 0.95 bar. That verdict "
                "exists to be read and then not acted on.")
    if verdict == "no edge after costs":
        return "it does not survive costs, so it is untradeable rather than marginal"
    if verdict == "insufficient data":
        return "fewer trades than a Sharpe means anything on"
    if verdict == "not evaluable":
        return "the numbers could not be computed; report that as unknown"
    return "it is not the promoting verdict"


def _current_engine_version():
    try:
        from engines.backtest import BACKTEST_ENGINE_VERSION
        return int(BACKTEST_ENGINE_VERSION)
    except Exception:
        return LEDGER_VERSION


def summarize(trade_pnl_bps, walkforward_result=None, n_trials=1,
              cost_bps=None, label=""):
    """Build the `summary` dict `record_evidence` expects.

    Wraps the two existing engines rather than restating their thresholds,
    so there is exactly one definition of "significant" in the codebase.
    """
    cost = sig.DEFAULT_COST_BPS if cost_bps is None else cost_bps
    out = {
        "significance": sig.edge_report(trade_pnl_bps, cost_bps=cost,
                                        n_trials=n_trials, label=label),
    }
    if isinstance(walkforward_result, dict):
        out["walkforward"] = walkforward_result
    return out
