"""SQLite wrapper. Stdlib only. Import-safe (no server imports)."""
import os
import sqlite3

SCHEMA_FILENAME = "schema.sql"


def _default_db_path():
    return os.environ.get("DB_PATH", os.path.join("data", "trading.db"))


DB_PATH = _default_db_path()

_connection = None


def _resolve_db_path(db_path=None):
    return db_path or os.environ.get("DB_PATH") or DB_PATH


def ensure_dirs(db_path=None):
    path = _resolve_db_path(db_path)
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    return path


def connect(db_path=None):
    """Return a new sqlite3 connection with Row factory + FK on."""
    path = ensure_dirs(db_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
    except Exception:
        pass
    return conn


def get(db_path=None):
    """Return a shared process-wide connection (lazy)."""
    global _connection
    path = _resolve_db_path(db_path)
    if _connection is None:
        _connection = connect(path)
    return _connection


def close():
    global _connection
    if _connection is not None:
        try:
            _connection.close()
        except Exception:
            pass
        _connection = None


def _migrate(conn):
    """Additive migrations for pre-existing DBs (idempotent, sqlite-safe)."""
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols)").fetchall()]
        for name, ddl in (
            ("exchange", "TEXT NOT NULL DEFAULT ''"),
            ("currency", "TEXT NOT NULL DEFAULT 'USD'"),
            ("sector",   "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in cols:
                conn.execute("ALTER TABLE symbols ADD COLUMN {} {}".format(name, ddl))
        conn.commit()
    except Exception:
        # table may not exist yet on very first boot; init_db's executescript handles it
        pass
    # -- 2FA + login throttle columns on users (idempotent additive migration)
    try:
        ucols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        for name, ddl in (
            ("totp_secret",    "TEXT NOT NULL DEFAULT ''"),
            ("totp_enabled",   "INTEGER NOT NULL DEFAULT 0"),
            ("recovery_codes", "TEXT NOT NULL DEFAULT '[]'"),
            ("last_login_ts",  "INTEGER NOT NULL DEFAULT 0"),
            ("failed_logins",  "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in ucols:
                conn.execute("ALTER TABLE users ADD COLUMN {} {}".format(name, ddl))
        conn.commit()
    except Exception:
        pass
    # -- exchange region column (global markets config)
    try:
        ecols = [r[1] for r in conn.execute("PRAGMA table_info(exchanges)").fetchall()]
        if "region" not in ecols:
            conn.execute("ALTER TABLE exchanges ADD COLUMN region TEXT NOT NULL DEFAULT ''")
        conn.commit()
    except Exception:
        pass
    # -- covering index for the screener's volume aggregation.
    #
    # SUM(volume) GROUP BY symbol has to read every candle row when the
    # only index is (symbol, ts), which cost ~17s on a 1.5M-row table and
    # made /api/screener unusable. Including `volume` makes the index
    # covering (~0.3s). Built at boot, so the first run pays the one-off
    # index build and every later query is fast.
    try:
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='candles_1m'").fetchall()}
        if "idx_candles_sym_vol" not in have:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_candles_sym_vol"
                " ON candles_1m(symbol, volume)")
            conn.commit()
    except Exception:
        pass
    # -- quote provenance: which feed produced each tick
    #
    # 'synthetic' vs a real venue name. Added because the feed loop
    # generates ticks and also polls real books, and the two were stored
    # identically -- so nothing downstream could tell a simulated price
    # from a live one. Defaulting to 'synthetic' means existing rows are
    # described honestly: they *were* generated.
    try:
        qcols = [r[1] for r in conn.execute(
            "PRAGMA table_info(quotes_l1)").fetchall()]
        if "source" not in qcols:
            conn.execute(
                "ALTER TABLE quotes_l1 ADD COLUMN source"
                " TEXT NOT NULL DEFAULT 'synthetic'")
            conn.commit()
    except Exception:
        pass
    # -- candle provenance: which feed produced each bar
    #
    # `quotes_l1` got this treatment already and the reasoning applies
    # verbatim to candles. `scripts/seed.py` writes 1-minute bars from a
    # seeded RNG for any symbol with no history, and they land in the same
    # table as a Binance backfill. Nothing downstream could tell them apart,
    # so a backtest over AAPL read a random walk and reported its Sharpe as
    # though it measured a strategy.
    #
    # Default 'synthetic' is the honest description of existing rows: they
    # were generated unless something says otherwise. Real writers must pass
    # their source explicitly.
    for tbl in ("candles_1m", "candles_1d"):
        try:
            ccols = [r[1] for r in conn.execute(
                "PRAGMA table_info(%s)" % tbl).fetchall()]
            if "source" not in ccols:
                conn.execute(
                    "ALTER TABLE %s ADD COLUMN source"
                    " TEXT NOT NULL DEFAULT 'synthetic'" % tbl)
                conn.commit()
        except Exception:
            pass
    # -- orders remember the exchange's own order id
    #
    # A live cancel has to reach the venue, and a venue cancel needs the
    # venue's id. Without this column the cancel path could only flip the
    # local row, so the order kept working at the exchange and filled
    # later with nothing in the app to explain it.
    try:
        ocols = [r[1] for r in conn.execute(
            "PRAGMA table_info(orders)").fetchall()]
        if "venue_order_id" not in ocols:
            conn.execute(
                "ALTER TABLE orders ADD COLUMN venue_order_id TEXT NOT NULL DEFAULT ''")
            conn.commit()
    except Exception:
        pass

    # -- fills record their own direction
    #
    # The table never had a side column, so every consumer either joined
    # orders or rendered a blank cell. A fill is the record of a trade and
    # "which way" is not derivable from it: inferring direction from the
    # sign of a position delta gives the wrong answer as soon as the
    # position flips through zero. Existing rows backfill from their order
    # so history is not left blank where it can be recovered.
    try:
        fcols = [r[1] for r in conn.execute(
            "PRAGMA table_info(fills)").fetchall()]
        if "side" not in fcols:
            conn.execute("ALTER TABLE fills ADD COLUMN side TEXT NOT NULL DEFAULT ''")
            conn.commit()
            conn.execute(
                "UPDATE fills SET side = COALESCE((SELECT o.side FROM orders o"
                " WHERE o.id = fills.order_id), '')")
            conn.commit()
    except Exception:
        pass

    # -- strategy catalogue: keep mode and params in step with the engine
    #
    # schema.sql seeds these with INSERT OR IGNORE, so an existing database
    # keeps whatever it was seeded with forever. That is correct for rows a
    # user has customised and wrong for rows describing what the engine can
    # actually do: when book_imbalance went from stub to wired in
    # engines/signals.py, the database kept advertising "stub" and the
    # catalogue reported a method that existed and a method that did not.
    #
    # Only `mode` and the two descriptive fields are synced. params_json is
    # deliberately left alone: those are the user's defaults to tune, and
    # overwriting them on every boot would discard real configuration.
    try:
        from engines.signals import STRATEGIES as _CANON
        for _s in _CANON:
            conn.execute(
                "UPDATE strategies SET mode = ?, tier_required = ?,"
                " display_name = COALESCE(NULLIF(display_name, ''), ?)"
                " WHERE name = ?",
                (_s.get("mode", "wired"), _s.get("tier_required", "Free"),
                 _s.get("display_name") or "", _s.get("name")))
        conn.commit()
    except Exception:
        pass

    # -- crypto/stablecoin markets (USDT and USDT pairs)
    #
    # Installed on boot so an existing database picks up USDT after a
    # code update without needing a re-seed. INSERT OR IGNORE inside
    # data.cryptomarkets.install, so local edits to these rows survive.
    try:
        from data import cryptomarkets as _cm
        _cm.install(conn)
    except Exception:
        pass

    # -- FX universe for cross-rate arbitrage
    #
    # Only EURUSD existed before, which makes every cross triangle
    # impossible (a cross needs three legs). Installed on boot for the
    # same reason as crypto: an existing DB picks them up without a
    # re-seed.
    try:
        from data import fxmarkets as _fxm
        _fxm.install(conn)
    except Exception:
        pass


def init_db(db_path=None, schema_path=None):
    """Create dirs + apply schema.sql idempotently. Returns connection."""
    path = ensure_dirs(db_path)
    if schema_path is None:
        schema_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), SCHEMA_FILENAME)
    conn = connect(path)
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            sql = f.read()
        try:
            conn.executescript(sql)
        except sqlite3.OperationalError:
            # stale DB: pre-existing tables lack newer columns (exchange,
            # currency, sector) so index/DDL statements fail. Add the columns
            # first, then replay the schema.
            _migrate(conn)
            conn.executescript(sql)
        _migrate(conn)
        conn.commit()
    finally:
        # keep shared handle in sync if it points at same path
        pass
    return conn


def execute(conn, sql, params=()):
    cur = conn.execute(sql, params)
    conn.commit()
    return cur


def executemany(conn, sql, seq):
    cur = conn.executemany(sql, seq)
    conn.commit()
    return cur


def query(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()


def query_one(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()
