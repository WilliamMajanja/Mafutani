"""Per-user risk limits CRUD + kill-switch. Stdlib only, import-safe.

Operates on a sqlite3.Connection; never imports server/db layers.
"""
import json
import time

DEFAULTS = {
    "max_position_qty": 100.0,
    "max_notional": 100000.0,
    "max_daily_loss": 1000.0,
    "max_trades_per_hour": 20,
    "cooldown_after_losses": 2,
    "allowlist_json": "[]",
    "eod_flatten": 0,
    "kill_switch": 0,
}

INT_FIELDS = {"max_trades_per_hour", "cooldown_after_losses", "eod_flatten", "kill_switch"}


def default_limits():
    return dict(DEFAULTS)


def _row_to_dict(row, user_id=None):
    if row is None:
        d = dict(DEFAULTS)
        if user_id is not None:
            d["user_id"] = int(user_id)
        return d
    d = dict(row) if not isinstance(row, dict) else dict(row)
    # sqlite3.Row -> dict
    try:
        d = dict(d)
    except Exception:
        pass
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        if k in d and d[k] is not None:
            out[k] = d[k]
    # coerce numeric types
    for k in out:
        if k in INT_FIELDS:
            try:
                out[k] = int(out[k])
            except (TypeError, ValueError):
                out[k] = int(DEFAULTS[k])
        elif k != "allowlist_json":
            try:
                out[k] = float(out[k])
            except (TypeError, ValueError):
                out[k] = float(DEFAULTS[k])
    if user_id is not None:
        out["user_id"] = int(user_id)
    elif "user_id" in d:
        out["user_id"] = d["user_id"]
    return out


def get_limits(conn, user_id):
    row = conn.execute("SELECT * FROM risk_limits WHERE user_id = ?", (int(user_id),)).fetchone()
    if row is None:
        return _row_to_dict(None, user_id)
    # sqlite3.Row needs keys()
    try:
        d = {k: row[k] for k in row.keys()}
    except Exception:
        d = dict(row)
    d["user_id"] = int(user_id)
    return _row_to_dict(d, user_id)


def set_limits(conn, user_id, patch):
    """Upsert allowed limit fields from patch dict. Returns updated dict."""
    current = get_limits(conn, user_id)
    allowed = set(DEFAULTS.keys())
    updates = {}
    for k, v in (patch or {}).items():
        if k not in allowed:
            continue
        if k == "allowlist_json":
            if isinstance(v, (list, tuple)):
                updates[k] = json.dumps(list(v))
            else:
                updates[k] = str(v)
        elif k in INT_FIELDS:
            # accept 0/1/bool
            if isinstance(v, bool):
                updates[k] = int(v)
            else:
                updates[k] = int(float(v))
        else:
            updates[k] = float(v)
    now = int(time.time())
    existing = conn.execute("SELECT user_id FROM risk_limits WHERE user_id = ?", (int(user_id),)).fetchone()
    if existing is None:
        merged = dict(DEFAULTS)
        merged.update(updates)
        conn.execute(
            "INSERT INTO risk_limits (user_id, max_position_qty, max_notional, max_daily_loss,"
            " max_trades_per_hour, cooldown_after_losses, allowlist_json, eod_flatten,"
            " kill_switch, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(user_id), merged["max_position_qty"], merged["max_notional"],
             merged["max_daily_loss"], merged["max_trades_per_hour"],
             merged["cooldown_after_losses"], merged["allowlist_json"],
             merged["eod_flatten"], merged["kill_switch"], now),
        )
    else:
        if updates:
            cols = ", ".join("{} = ?".format(k) for k in updates)
            params = list(updates.values()) + [now, int(user_id)]
            conn.execute("UPDATE risk_limits SET {}, updated_at = ? WHERE user_id = ?".format(cols), params)
        else:
            conn.execute("UPDATE risk_limits SET updated_at = ? WHERE user_id = ?", (now, int(user_id)))
    conn.commit()
    return get_limits(conn, user_id)


def check_kill(conn, user_id):
    """True if global kill-switch is ON for user (blocks new orders)."""
    row = conn.execute("SELECT kill_switch FROM risk_limits WHERE user_id = ?", (int(user_id),)).fetchone()
    if row is None:
        return False
    try:
        val = row["kill_switch"] if "kill_switch" in row.keys() else row[0]
    except Exception:
        val = row[0]
    return bool(int(val or 0))


def set_kill(conn, user_id, on=True):
    return set_limits(conn, user_id, {"kill_switch": 1 if on else 0})
