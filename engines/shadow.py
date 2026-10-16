"""Forward shadow: observe a candidate without risking capital. Stdlib only.

What this is
------------
The stage between "it looked good in a backtest" and "it may trade real
money". The candidate runs against live venue books, its hypothetical fills
are recorded exactly as they would have been, and nothing is sent anywhere.

Why it is a separate stage rather than "paper trading"
-----------------------------------------------------
Paper trading in this app (`brokers/paper.py`) answers "what would have
happened if I traded this?". Forward shadow answers the question a backtest
cannot: *does this still work on data that did not exist when it was
validated?* A backtest is evidence about the past, including its own
parameter search; the argument that a strategy will continue is the weakest
part of every backtest, and this is where it gets tested.

Every cycle records what was observed and what was expected, so a
deterioration is visible as a number rather than as an opinion.

Deliberately not here
---------------------
No order routing, no broker credentials, no position sizing. A shadow cycle
cannot reach an exchange even if it wanted to, because it has no path to one.

Each cycle records:
- the evidence that preceded it (which backtest, which engine version), so
  the shadow is anchored to a specific claim rather than floating free
- the trades it would have taken and their net result
- whether the result matched what that evidence predicted

A shadow cycle that recorded nothing is still recorded. "We observed and
there was no signal" and "nobody looked" are different facts, and only one
of them is evidence.
"""

import json
import time

__all__ = ["SCHEMA_SQL", "run_cycle", "cycles", "summary", "deteriorated",
           "MIN_CYCLES_FOR_VERDICT", "SHADOW_VERSION"]

SHADOW_VERSION = 1

# A verdict on a handful of cycles would be the same mistake a backtest makes
# at smaller scale, so below this the honest answer is "not enough yet".
MIN_CYCLES_FOR_VERDICT = 10

# Shadow expectancy falling this far below what the evidence predicted is
# the signal that the edge has decayed. Reported, never acted on: this module
# does not demote anything, it only measures.
DETERIORATION_RATIO = 0.5


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS shadow_cycles (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            INTEGER NOT NULL DEFAULT (strftime('%s','now')),
  strategy      TEXT NOT NULL,
  symbol        TEXT NOT NULL DEFAULT '',
  params_json   TEXT NOT NULL DEFAULT '{}',
  anchor_id     INTEGER,
  engine_version INTEGER,
  trades        INTEGER NOT NULL DEFAULT 0,
  wins          INTEGER NOT NULL DEFAULT 0,
  expectancy_bps REAL,
  predicted_bps REAL,
  detail_json   TEXT NOT NULL DEFAULT '{}',
  note          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_shadow_strategy
  ON shadow_cycles(strategy, id DESC);
"""


def _now():
    try:
        return int(time.time())
    except Exception:
        return 0


def ensure_schema(conn):
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


def run_cycle(conn, strategy, trade_pnl_bps=None, predicted_bps=None,
              symbol="", params=None, anchor_id=None, engine_version=None,
              note=""):
    """Record one observed forward cycle.

    `trade_pnl_bps` is the per-trade net P&L in basis points the candidate
    would have earned on live books. It is not computed here -- this module
    measures and records, and the fill logic belongs to the strategy engine,
    because a second implementation of the fill would be a second thing to
    be wrong.

    An empty trade list is a legitimate cycle: it says the candidate was
    observed and did not fire. That is different from not running the cycle,
    and conflating them would let a strategy accrue forward evidence by
    never being looked at.

    Returns (ok, detail).
    """
    return _record(conn, strategy, trade_pnl_bps, predicted_bps, symbol,
                   params, anchor_id, engine_version, note)


def _record(conn, strategy, trade_pnl_bps, predicted_bps, symbol, params,
            anchor_id, engine_version, note):
    """Shared body of `run_cycle`. The per-trade P&L is kept in the row so
    `summary` can aggregate it, rather than only a mean that cannot be
    re-examined."""
    strategy = str(strategy or "").strip()
    if not strategy:
        return False, {"error": "no strategy named"}
    pnl = []
    for x in (trade_pnl_bps or []):
        try:
            pnl.append(float(x))
        except (TypeError, ValueError):
            continue
    wins = sum(1 for x in pnl if x > 0)
    expectancy = None
    if pnl:
        try:
            expectancy = round(sum(pnl) / len(pnl), 4)
        except ZeroDivisionError:
            expectancy = None
    detail = {
        "shadow_version": SHADOW_VERSION,
        "trades": len(pnl),
        "wins": wins,
        "no_signal": not pnl,
        "observed_at": _now(),
        "pnl": pnl,
    }
    try:
        ensure_schema(conn)
        cur = conn.execute(
            "INSERT INTO shadow_cycles (ts, strategy, symbol, params_json,"
            " anchor_id, engine_version, trades, wins, expectancy_bps,"
            " predicted_bps, detail_json, note)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), strategy, str(symbol or ""), json.dumps(params or {}),
             int(anchor_id) if anchor_id is not None else None,
             int(engine_version) if engine_version is not None else None,
             len(pnl), wins, expectancy, predicted_bps,
             json.dumps(detail), str(note or "")))
        conn.commit()
        detail["id"] = int(cur.lastrowid)
        return True, detail
    except Exception as e:
        return False, {"error": str(e)}


def cycles(conn, strategy, limit=500):
    """Recorded cycles for `strategy`, newest first."""
    limit = max(1, min(int(limit or 500), 5000))
    try:
        ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM shadow_cycles WHERE strategy = ?"
            " ORDER BY id DESC LIMIT ?", (str(strategy or ""), limit)).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        d = _row_to_dict(r)
        try:
            d["detail"] = json.loads(d.get("detail_json") or "{}")
        except Exception:
            d["detail"] = {}
        out.append(d)
    return out


def summary(conn, strategy, min_cycles=MIN_CYCLES_FOR_VERDICT):
    """Aggregate a strategy's forward record.

    Distinguishes four states, because they mean different things and
    collapsing any two of them is how a shadow stage becomes decorative:

    * never run          -- nothing observed at all
    * observed, no signal -- ran and did not fire; real but not evidence of
      anything either way
    * too few cycles     -- some trades, but too few to conclude
    * assessed           -- enough cycles for a verdict
    """
    rows = cycles(conn, strategy)
    n = len(rows)
    total_trades = sum(int(r.get("trades") or 0) for r in rows)
    all_pnl = []
    for r in rows:
        try:
            all_pnl.extend(json.loads(r.get("detail_json") or "{}")
                            .get("pnl", []))
        except Exception:
            pass
    wins = sum(int(r.get("wins") or 0) for r in rows)
    predicted = [r.get("predicted_bps") for r in rows
                 if r.get("predicted_bps") is not None]
    observed = [r.get("expectancy_bps") for r in rows
                if r.get("expectancy_bps") is not None]

    out = {
        "strategy": str(strategy or ""),
        "cycles": n,
        "shadow_version": SHADOW_VERSION,
        "trades": total_trades,
        "wins": wins,
        "win_rate": (wins / float(total_trades)) if total_trades else None,
        "expectancy_bps": None,
        "predicted_bps": None,
        "first_ts": rows[-1].get("ts") if rows else None,
        "last_ts": rows[0].get("ts") if rows else None,
        "min_cycles": int(min_cycles),
        "deteriorated": None,
        "verdict": None,
        "reasons": [],
    }
    if n == 0:
        out["verdict"] = "never run"
        out["reasons"].append("no forward cycle has been recorded")
        return out
    if total_trades == 0:
        out["verdict"] = "observed, no signal"
        out["reasons"].append(
            "%d cycle(s) observed, none produced a trade. That is a real "
            "observation and it is not evidence for or against the edge."
            % n)
        return out
    out["expectancy_bps"] = round(sum(all_pnl) / len(all_pnl), 4)
    if predicted:
        out["predicted_bps"] = round(sum(predicted) / len(predicted), 4)
    if n < int(min_cycles):
        out["verdict"] = "too few cycles"
        out["reasons"].append(
            "%d of %d cycles observed; a verdict here would repeat the "
            "backtest's mistake at smaller scale" % (n, int(min_cycles)))
        return out
    out["deteriorated"] = deteriorated(out)
    out["verdict"] = ("edge has decayed" if out["deteriorated"]
                      else "holding up in forward observation")
    return out


def deteriorated(summary_row, ratio=DETERIORATION_RATIO):
    """Has the forward result fallen well below what was predicted?

    Only answerable when the evidence recorded a prediction. Without one,
    this returns None rather than False: "we did not predict anything" is
    not "it is holding up".
    """
    obs = (summary_row or {}).get("expectancy_bps")
    pred = (summary_row or {}).get("predicted_bps")
    if obs is None or pred is None or pred <= 0:
        return None
    return obs < pred * float(ratio)
