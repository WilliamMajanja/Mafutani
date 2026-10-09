"""Onboard an Institutional account and credit it a starting balance.

Two things this does that a raw INSERT would not:

  * the credit is recorded in BOTH `starting_cash` and `cash`, and in the
    audit log, so the balance is a documented deposit rather than an
    unexplained number that looks like trading profit;
  * provisioning is run through core.provision.provision, so the account
    gets the same risk limits, watchlist and starter strategies as any
    other signup. An Institutional user with default 20-trades-per-hour
    and a $100 notional cap cannot actually trade.
"""
import argparse
import json
import os
import sys
import time

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKDIR not in sys.path:
    sys.path.insert(0, WORKDIR)

from core.auth import pbkdf2_hash          # noqa: E402
from core.provision import provision, STARTING_CASH  # noqa: E402


def connect(path=None):
    import sqlite3
    from data.db import connect as db_connect
    # Reuse the app's own connection helper so the path resolution
    # (DB_PATH env, default location) can never drift from the server's.
    return db_connect(path)


def audit(conn, user_id, tier, action, detail):
    """Mirror api.routes._audit's column layout exactly.

    The table is (ts, actor_user_id, action, tier, paper_live,
    details_json) -- writing the intuitive column names silently fails
    here, and a silent audit failure is the one thing an audit log must
    never be.
    """
    try:
        conn.execute(
            "INSERT INTO audit_log (ts, actor_user_id, action, tier,"
            " paper_live, details_json) VALUES (?, ?, ?, ?, ?, ?)",
            (int(time.time()), user_id, str(action), str(tier or ""),
             "paper", json.dumps(detail or {})))
        conn.commit()
    except Exception as e:
        print("  (audit write FAILED: %s)" % e)
        raise


def onboard(email, password, tier="Institutional", cash=None, db=None,
            risk=None):
    conn = connect(db)
    try:
        email = str(email or "").strip().lower()
        if "@" not in email or len(email) < 3:
            raise SystemExit("invalid email: %r" % email)
        if not password or len(password) < 8:
            raise SystemExit("password must be >= 8 chars")
        tier = str(tier or "Free")
        target = float(cash) if cash is not None else float(
            STARTING_CASH.get(tier, 10_000.0))
        if target < 0:
            raise SystemExit("cash must be >= 0")

        existing = conn.execute(
            "SELECT id, tier FROM users WHERE email = ?", (email,)).fetchone()
        created = existing is None
        if created:
            cur = conn.execute(
                "INSERT INTO users (email, password_hash, tier)"
                " VALUES (?, ?, ?)", (email, pbkdf2_hash(password), tier))
            uid = cur.lastrowid
        else:
            uid = existing["id"]
            conn.execute("UPDATE users SET tier = ? WHERE id = ?", (tier, uid))
        conn.commit()

        # Cash. Recorded in starting_cash so the balance is auditable as
        # a deposit, and never as profit.
        acct = conn.execute(
            "SELECT cash, starting_cash FROM accounts WHERE user_id = ?",
            (uid,)).fetchone()
        before = float(acct["cash"]) if acct else 0.0
        if acct is None:
            conn.execute(
                "INSERT INTO accounts (user_id, cash, starting_cash)"
                " VALUES (?, ?, ?)", (uid, target, target))
            basis = "created"
        else:
            conn.execute(
                "UPDATE accounts SET cash = ?, starting_cash = ?"
                " WHERE user_id = ?", (target, target, uid))
            basis = "reset"
        conn.commit()

        prov = provision(conn, uid, tier)

        # Risk limits matched to the balance. Defaults are sized for a
        # ~$100 account and would make a $500M account unable to trade.
        limits = risk or {
            "max_position_qty": 10_000.0,
            "max_notional": min(target, 500_000_000.0) or 100_000.0,
            "max_daily_loss": max(target * 0.1, 1_000.0),
            "max_trades_per_hour": 200,
        }
        conn.execute(
            "INSERT OR IGNORE INTO risk_limits (user_id) VALUES (?)", (uid,))
        conn.execute(
            "UPDATE risk_limits SET max_position_qty = ?, max_notional = ?,"
            " max_daily_loss = ?, max_trades_per_hour = ?, updated_at = ?"
            " WHERE user_id = ?",
            (float(limits["max_position_qty"]), float(limits["max_notional"]),
             float(limits["max_daily_loss"]), int(limits["max_trades_per_hour"]),
             int(time.time()), uid))
        conn.commit()

        audit(conn, uid, tier, "account.onboard",
              {"email": email, "created": created, "cash_before": before,
               "cash_after": target, "tier": tier, "basis": basis,
               "note": "starting balance credited, not trading profit"})
        conn.commit()

        row = conn.execute(
            "SELECT cash, starting_cash FROM accounts WHERE user_id = ?",
            (uid,)).fetchone()
        rl = conn.execute(
            "SELECT max_position_qty, max_notional, max_trades_per_hour"
            " FROM risk_limits WHERE user_id = ?", (uid,)).fetchone()
        return {
            "user_id": uid, "email": email, "tier": tier, "created": created,
            "cash": row["cash"], "starting_cash": row["starting_cash"],
            "cash_before": before, "basis": basis,
            "provisioned": prov, "risk": dict(rl) if rl else None,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Onboard a Mafutani account")
    ap.add_argument("email")
    ap.add_argument("password")
    ap.add_argument("--tier", default="Institutional")
    ap.add_argument("--cash", type=float, default=None)
    ap.add_argument("--db", default=None)
    a = ap.parse_args()
    out = onboard(a.email, a.password, a.tier, a.cash, a.db)
    print(json.dumps(out, indent=2, default=str))
