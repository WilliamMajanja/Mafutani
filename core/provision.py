"""Idempotent per-user provisioning.

Every account — whether created by `POST /api/auth/signup` or by
`scripts/seed.py` — runs through :func:`provision` so that a brand new
login is immediately usable instead of hitting empty tables:

  * a `risk_limits` row (otherwise every order trips "no risk limits")
  * a default watchlist seeded with liquid, cross-asset symbols
  * the starter strategy set assigned for the user's tier
  * a journal-ready account (no rows needed, table presence is enough)

Everything here is INSERT OR IGNORE / existence-checked, so running it
again on an existing account is a no-op. It never raises: a provisioning
failure must not block a signup.
"""

import json

# Liquid, cross-asset starter basket: one per major asset class in the
# seeded universe so a new account is useful immediately.
DEFAULT_WATCHLIST = ["AAPL", "MSFT", "NVDA", "BTCUSD", "ETHUSD", "EURUSD", "XAUUSD"]

# Starting paper cash for a new account, by tier.
#
# An account with no `accounts` row is a dead end: _apply_paper_fill
# reads cash from that table, finds nothing, treats it as 0.0, and the
# first fill drives the balance negative. Signup and provisioning
# between them created the user, the risk limits and the watchlist but
# never the cash ledger, so every self-registered account started unable
# to trade. These are paper dollars, not money.
STARTING_CASH = {
    "Free": 10_000.0,
    "Pro": 100_000.0,
    "Institutional": 1_000_000.0,
    "Admin": 1_000_000.0,
}
_DEFAULT_CASH = 10_000.0

# Starter strategies per tier. Mirrors core/tiers.py gates: Free users get
# the Free method, higher tiers get progressively more, but every tier
# keeps at least one working strategy so the app is never a dead end.
STARTER_STRATEGIES = {
    "Free": ["ema_trend"],
    "Pro": ["ema_trend", "dual_ema", "mean_reversion"],
    "Institutional": ["ema_trend", "dual_ema", "mean_reversion",
                      "rsi_fade", "book_imbalance", "news_momentum"],
    "Admin": ["ema_trend", "dual_ema", "mean_reversion",
              "rsi_fade", "book_imbalance", "news_momentum"],
}


def provision(conn, user_id, tier="Free", watchlist=None):
    """Ensure a user has risk limits, a watchlist and starter strategies.

    Returns a dict of what was created, e.g.
    ``{"risk_limits": True, "watchlist": True, "strategies": 3}``.
    Never raises — callers treat provisioning as best-effort.
    """
    result = {"risk_limits": False, "watchlist": False, "account": False,
              "starting_cash": 0.0, "strategies": 0, "skipped": None}
    uid = int(user_id)
    tier = str(tier or "Free")

    # ---- cash ledger: without this row the account cannot trade -------
    try:
        has = conn.execute(
            "SELECT 1 FROM accounts WHERE user_id = ?", (uid,)).fetchone()
        if has is None:
            cash = float(STARTING_CASH.get(tier, _DEFAULT_CASH))
            conn.execute(
                "INSERT INTO accounts (user_id, cash, starting_cash)"
                " VALUES (?, ?, ?)", (uid, cash, cash))
            result["account"] = True
            result["starting_cash"] = cash
    except Exception as e:
        result["skipped"] = "accounts: {}".format(e)

    # ---- risk limits: without this row every order is rejected ----------
    try:
        has = conn.execute(
            "SELECT 1 FROM risk_limits WHERE user_id = ?", (uid,)).fetchone()
        if has is None:
            conn.execute(
                "INSERT OR IGNORE INTO risk_limits (user_id) VALUES (?)", (uid,))
            result["risk_limits"] = True
    except Exception as e:
        result["skipped"] = "risk_limits: {}".format(e)

    # ---- default watchlist ---------------------------------------------
    try:
        has = conn.execute(
            "SELECT 1 FROM watchlists WHERE user_id = ?", (uid,)).fetchone()
        if has is None:
            syms = watchlist if watchlist is not None else DEFAULT_WATCHLIST
            # only keep symbols that actually exist in the universe, so the
            # watchlist never renders dead rows
            clean = []
            for s in syms:
                row = conn.execute(
                    "SELECT 1 FROM symbols WHERE symbol = ?", (s,)).fetchone()
                if row is not None:
                    clean.append(s)
            conn.execute(
                "INSERT INTO watchlists (user_id, name, symbols_json)"
                " VALUES (?, 'default', ?)",
                (uid, json.dumps(clean)))
            result["watchlist"] = True
    except Exception as e:
        result["skipped"] = (result["skipped"] or "") + " watchlist: {}".format(e)

    # ---- starter strategies --------------------------------------------
    # strategies are global catalogue rows; a user "owns" them by tier, so
    # there is nothing per-user to insert. Verified here so a broken
    # catalogue is caught at provisioning time rather than mid-backtest.
    try:
        wanted = STARTER_STRATEGIES.get(tier, STARTER_STRATEGIES["Free"])
        have = {r[0] for r in conn.execute(
            "SELECT name FROM strategies").fetchall()}
        result["strategies"] = sum(1 for w in wanted if w in have)
        result["missing"] = [w for w in wanted if w not in have]
    except Exception as e:
        result["skipped"] = (result["skipped"] or "") + " strategies: {}".format(e)

    try:
        conn.commit()
    except Exception:
        pass
    return result


def provision_all(conn, only_missing=True):
    """Backfill provisioning for every existing user (used by seed.py)."""
    out = []
    rows = conn.execute("SELECT id, email, tier FROM users").fetchall()
    for r in rows:
        uid, email, tier = r["id"], r["email"], r["tier"]
        if only_missing and _is_provisioned(conn, uid):
            continue
        res = provision(conn, uid, tier)
        out.append({"id": uid, "email": email, "tier": tier, **res})
    return out


def _is_provisioned(conn, uid):
    try:
        rl = conn.execute("SELECT 1 FROM risk_limits WHERE user_id = ?",
                          (uid,)).fetchone()
        wl = conn.execute("SELECT 1 FROM watchlists WHERE user_id = ?",
                          (uid,)).fetchone()
        return rl is not None and wl is not None
    except Exception:
        return False
