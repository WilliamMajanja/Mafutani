"""Deterministic dev seed: users (with known passwords), the symbol
universe, and a small 1m-candle backfill for symbols with no history.

Idempotent — safe to re-run at any time (upserts + INSERT OR IGNORE).

Credentials for every *.local account are reset to a known password on each
run so the dev environment always has working, documented logins. External
(r@l) accounts like gmail are never touched. Run:

    python3 scripts/seed.py
"""

import hashlib
import math
import os
import random
import secrets
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

HASH_ITERATIONS = 200_000

# The credential ledger lives in core/devaccounts so the seeder and the
# server's boot log always agree. Re-exported here for convenience.
from core.devaccounts import (  # noqa: E402
    TEST_USERS, is_managed, local_dev_password as _local_dev_password,
    password_for, tier_for, format_ledger,
)
from core import devaccounts  # noqa: E402


def _hash_pw(password):
    from core.auth import pbkdf2_hash
    return pbkdf2_hash(password)


def seed_users(conn):
    """Upsert canonical accounts (tier/password enforced) and reset every
    other *.local dev account to its deterministic password."""
    for email, password, tier in TEST_USERS:
        conn.execute(
            "INSERT INTO users (email, password_hash, tier, disabled)"
            " VALUES (?, ?, ?, 0)"
            " ON CONFLICT(email) DO UPDATE SET tier = excluded.tier,"
            " disabled = 0, password_hash = excluded.password_hash",
            (email, _hash_pw(password), tier))
    # Deterministic passwords for signup-created *.local accounts.
    rows = conn.execute(
        "SELECT email FROM users WHERE email LIKE ?", (devaccounts.LOCAL_PATTERN,)
    ).fetchall()
    for r in rows:
        em = r["email"] if hasattr(r, "keys") else r[0]
        if tier_for(em) is not None:
            continue
        conn.execute("UPDATE users SET password_hash = ?, disabled = 0"
                     " WHERE email = ?", (_hash_pw(_local_dev_password(em)), em))
    conn.commit()


# ---------------------------------------------------------------- universe
# (symbol, asset_type, name, exchange, currency, sector)
SYMBOLS = [
    ("AAPL", "equity", "Apple Inc.", "NASDAQ", "USD", "Technology"),
    ("NVDA", "equity", "NVIDIA Corp.", "NASDAQ", "USD", "Technology"),
    ("MSFT", "equity", "Microsoft Corp.", "NASDAQ", "USD", "Technology"),
    ("EURUSD", "fx", "Euro / US Dollar", "EBS", "USD", "FX"),
    ("ALUM", "commodity", "Aluminium", "LME", "USD", "Metals"),
    ("COPPER", "commodity", "Copper", "COMEX", "USD", "Metals"),
    ("ZINC", "commodity", "Zinc", "LME", "USD", "Metals"),
    ("LEAD", "commodity", "Lead", "LME", "USD", "Metals"),
    ("NICKEL", "commodity", "Nickel", "LME", "USD", "Metals"),
    ("TIN", "commodity", "Tin", "LME", "USD", "Metals"),
    ("XAUUSD", "commodity", "Gold", "COMEX", "USD", "Metals"),
    ("XAGUSD", "commodity", "Silver", "COMEX", "USD", "Metals"),
    ("XPTUSD", "commodity", "Platinum", "NYMEX", "USD", "Metals"),
    ("XPDUSD", "commodity", "Palladium", "NYMEX", "USD", "Metals"),
    ("BRENT", "commodity", "Brent Crude", "ICE", "USD", "Energy"),
    ("WTI", "commodity", "Crude Oil WTI", "NYMEX", "USD", "Energy"),
    ("NGAS", "commodity", "Natural Gas", "NYMEX", "USD", "Energy"),
    ("GASOLINE", "commodity", "RBOB Gasoline", "NYMEX", "USD", "Energy"),
    ("HEATOIL", "commodity", "Heating Oil", "NYMEX", "USD", "Energy"),
    ("CORN", "commodity", "Corn", "CBOT", "USD", "Grains"),
    ("SOYBEANS", "commodity", "Soybeans", "CBOT", "USD", "Grains"),
    ("SOYMEAL", "commodity", "Soybean Meal", "CBOT", "USD", "Grains"),
    ("SOYOIL", "commodity", "Soybean Oil", "CBOT", "USD", "Grains"),
    ("WHEAT", "commodity", "Wheat", "CBOT", "USD", "Grains"),
    ("OATS", "commodity", "Oats", "CBOT", "USD", "Grains"),
    ("RRICE", "commodity", "Rough Rice", "CBOT", "USD", "Grains"),
    ("SUGAR", "commodity", "Sugar #11", "ICE", "USD", "Softs"),
    ("COFFEE", "commodity", "Coffee (Arabica)", "ICE", "USD", "Softs"),
    ("COCOA", "commodity", "Cocoa", "ICE", "USD", "Softs"),
    ("COTTON", "commodity", "Cotton", "ICE", "USD", "Softs"),
    ("OJ", "commodity", "Orange Juice", "ICE", "USD", "Softs"),
    ("FCATTLE", "commodity", "Feeder Cattle", "CME", "USD", "Livestock"),
    ("LCATTLE", "commodity", "Live Cattle", "CME", "USD", "Livestock"),
    ("LHOGS", "commodity", "Lean Hogs", "CME", "USD", "Livestock"),
]

# Crypto and stablecoin symbols live in data.cryptomarkets rather than
# here, because the same module decides which public venue quotes each
# one. Declaring them in two places is how a symbol ends up in the
# screener with no real feed behind it.
try:
    from data.cryptomarkets import CRYPTO_SYMBOLS
    from data.fxmarkets import FX_SYMBOLS
except ImportError:  # pragma: no cover - run from another cwd
    from cryptomarkets import CRYPTO_SYMBOLS
    from fxmarkets import FX_SYMBOLS

SYMBOLS.extend(
    (symbol, "crypto", name, exchange, currency, sector)
    for (symbol, name, exchange, currency, sector, _px) in CRYPTO_SYMBOLS
)
# FX rows come from the registry, replacing the hand-written EURUSD above
# rather than duplicating it. Left in place, INSERT OR IGNORE would let
# the older seed row win and the registry's tick size and venue would
# never be applied -- the opposite of what the registry is for.
SYMBOLS = [row for row in SYMBOLS if row[0] != "EURUSD"]
SYMBOLS.extend(
    (symbol, "fx", name, venue.upper(), quote, sector)
    for (symbol, name, _base, quote, venue, sector,
         _tick, _lot, _px) in FX_SYMBOLS
)


def seed_symbols(conn):
    for symbol, asset_type, name, exchange, currency, sector in SYMBOLS:
        conn.execute(
            "INSERT OR IGNORE INTO symbols"
            " (symbol, asset_type, name, exchange, currency, sector, enabled)"
            " VALUES (?, ?, ?, ?, ?, ?, 1)",
            (symbol, asset_type, name, exchange, currency, sector))
    conn.commit()


# ---------------------------------------------------------------- candles
# Approximate reference price per symbol (for backfill only).
BASE_PRICE = {
    "AAPL": 220.0, "NVDA": 130.0, "MSFT": 420.0,
    "BTCUSD": 65000.0, "ETHUSD": 3500.0,
    "ALUM": 2500.0, "COPPER": 9500.0, "ZINC": 2900.0, "LEAD": 2100.0,
    "NICKEL": 16500.0, "TIN": 32000.0,
    "XAUUSD": 2400.0, "XAGUSD": 28.5, "XPTUSD": 950.0, "XPDUSD": 1000.0,
    "BRENT": 85.0, "WTI": 80.0, "NGAS": 2.5, "GASOLINE": 2.3,
    "HEATOIL": 2.6,
    "CORN": 450.0, "SOYBEANS": 1150.0, "SOYMEAL": 330.0, "SOYOIL": 45.0,
    "WHEAT": 560.0, "OATS": 330.0, "RRICE": 17.5,
    "SUGAR": 22.0, "COFFEE": 185.0, "COCOA": 4200.0, "COTTON": 88.0,
    "OJ": 250.0, "FCATTLE": 250.0, "LCATTLE": 185.0, "LHOGS": 75.0,
}

# Reference prices are for candle backfill only -- they are never shown as
# a live price. Extended from data.cryptomarkets so the crypto universe,
# its venues and its backfill seeds all come from one definition.
BASE_PRICE.update({sym: px for (sym, _n, _e, _c, _s, px) in CRYPTO_SYMBOLS})
BASE_PRICE.update({sym: px for (sym, _n, _b, _q, _v, _s, _t, _l, px) in FX_SYMBOLS})


def seed_candles(conn, bars_per_symbol=1440):
    """Backfill the last ~24h of 1m bars for symbols that have no candles.
    Deterministic per symbol (seeded RNG). No-op on populated databases.

    These rows are written with `source='synthetic'`, and the backtest
    refuses to report a figure from them without saying so. A seeded random
    walk is useful for exercising a code path and worthless as evidence about
    a market, and the failure mode is silent: the Sharpe comes out plausible
    and the trades look real.
    """
    now = int(time.time())
    empty = [r[0] for r in conn.execute(
        "SELECT symbol FROM symbols WHERE NOT EXISTS"
        " (SELECT 1 FROM candles_1m c WHERE c.symbol = symbols.symbol)"
        " AND enabled = 1").fetchall()]
    ts0 = now - bars_per_symbol * 60
    for sym in empty:
        base = float(BASE_PRICE.get(sym, 100.0))
        rng = random.Random("mafutani-seed-" + sym)
        vol_per_min = max(50, int(base * 30))
        price = base
        # Inject realistic anomalies into a subset of bars so the
        # anomaly radar has something to find. Without this every bar
        # moves <0.2% and every detector threshold is unreachable --
        # the radar reads as broken because the data is too smooth,
        # not because the detectors are wrong.
        anomaly_bars = set(rng.sample(range(20, bars_per_symbol - 20),
                                      max(3, bars_per_symbol // 60)))
        # The detectors mostly evaluate the *last* bar, so a random
        # scatter leaves the recent window quiet and the radar reads as
        # broken. Guarantee a few anomalies in the most recent bars.
        # These need to be far stronger than the background scatter:
        # the z-score detector uses a 20-bar window, so mixed
        # anomalies dilute each other below the threshold.
        for i in range(min(8, bars_per_symbol // 20)):
            anomaly_bars.add(bars_per_symbol - 1 - i * 3)
        recent_anomaly = {bars_per_symbol - 1, bars_per_symbol - 4,
                           bars_per_symbol - 7}
        rows = []
        for i in range(bars_per_symbol):
            is_anomaly = i in anomaly_bars
            # Normal: ~0.06% vol. Background anomalies: 10-30x.
            # Recent anomalies: 50-100x so the z-score, velocity, and
            # volume detectors all fire on the last bar.
            if i in recent_anomaly:
                vol_mult = rng.uniform(200.0, 500.0)
            elif is_anomaly:
                vol_mult = rng.uniform(10.0, 30.0)
            else:
                vol_mult = 1.0
            ret = rng.gauss(0.0, 0.0006 * vol_mult)
            op = price
            cl = max(0.0001, op * (1.0 + ret))
            hi = op * (1.0 + abs(rng.gauss(0.0004, 0.0004)) * vol_mult)
            lo = op * (1.0 - abs(rng.gauss(0.0004, 0.0004)) * vol_mult)
            vol = max(1, int(vol_per_min * rng.uniform(0.3, 1.8) * vol_mult))
            rows.append((sym, ts0 + i * 60, round(op, 6),
                         round(max(hi, cl), 6), round(min(lo, op), 6),
                         round(cl, 6), vol))
            price = cl
        # One transaction per symbol. 56k individual commits is what made
        # the seeder time out; executemany is the standard bulk-load fix.
        conn.executemany(
            "INSERT OR IGNORE INTO candles_1m"
            " (symbol, ts, open, high, low, close, volume, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'synthetic')", rows)
        conn.commit()
        print("  candles: backfilled {} - {} bars".format(sym, bars_per_symbol))


# ------------------------------------------------------------------ main
def print_accounts(conn):
    """Print the full account/password ledger (delegates to devaccounts)."""
    for line in format_ledger(conn):
        print(line)


def main():
    from data.db import connect
    conn = connect()
    seed_users(conn)
    seed_symbols(conn)
    seed_candles(conn)
    # every account gets risk limits + a default watchlist + starter
    # strategies, exactly as a fresh signup does
    try:
        from core.provision import provision_all
        done = provision_all(conn)
        if done:
            print("provisioned {} account(s): {}".format(
                len(done), ", ".join(d["email"] for d in done)))
    except Exception as e:
        print("provisioning skipped: {}".format(e))
    print_accounts(conn)
    n_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    n_syms = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    n_bars = conn.execute("SELECT COUNT(*) FROM candles_1m").fetchone()[0]
    # Rebuild the materialised volume summary now that candles exist, so
    # the first screener request does not pay the aggregation.
    n_vols = 0
    try:
        from api.routes import refresh_symbol_volume
        refresh_symbol_volume(conn)
        n_vols = conn.execute(
            "SELECT COUNT(*) FROM symbol_volume WHERE stale = 0").fetchone()[0]
    except Exception as e:
        print("volume summary skipped: {}".format(e))
    conn.close()
    print("\nseed done: {} users, {} symbols, {} candles, {} volume rows".format(
        n_users, n_syms, n_bars, n_vols))


if __name__ == "__main__":
    main()