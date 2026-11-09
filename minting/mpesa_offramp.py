"""CommCoin/MFT -> M-Pesa (Daraja) off-ramp requests (stdlib only).

This module records user-initiated off-ramp requests and can submit them to
Daraja when operator-configured. It is intentionally conservative:
- explicit confirmation required
- auth/CSRF/audit enforced by API routes
- request acceptance by Daraja is NOT settlement (callbacks/reconciliation)
- conversion rate is operator-configured reference metadata, not a market price
- operator must handle KYC/AML/sanctions/tax/compliance
"""

import json
import os
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from minting.daraja import DarajaClient, DarajaError

STATUS_PENDING = "pending"          # recorded, awaiting operator processing / verification
STATUS_SUBMITTED = "submitted"      # accepted by Daraja API (not settled)
STATUS_COMPLETED = "completed"      # operator/callback reconciliation marked complete
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected"

TERMINAL = {STATUS_COMPLETED, STATUS_FAILED, STATUS_REJECTED}


class OfframpError(Exception):
    pass


def _env(name, default=""):
    v = os.environ.get(name)
    return default if v is None else str(v)


def config():
    # KES per 1 MFT. 0/empty => disabled (must be explicitly configured).
    raw_rate = str(_env("MAFUTANI_MFT_KES_RATE", "0")).strip() or "0"
    try:
        rate = Decimal(raw_rate)
    except (InvalidOperation, ValueError):
        rate = Decimal("0")
    if rate < 0:
        rate = Decimal("0")
    min_mft = str(_env("MAFUTANI_MPESA_OFFRAMP_MIN_MFT", "0.00000001")).strip() or "0.00000001"
    max_mft = str(_env("MAFUTANI_MPESA_OFFRAMP_MAX_MFT", "1000000000")).strip() or "1000000000"
    try:
        min_mft_d = Decimal(min_mft)
    except Exception:
        min_mft_d = Decimal("0.00000001")
    try:
        max_mft_d = Decimal(max_mft)
    except Exception:
        max_mft_d = Decimal("1000000000")
    return {
        "enabled": rate > 0,
        "rate_kes_per_mft": str(rate),
        "rate_configured": rate > 0,
        "min_mft": str(min_mft_d),
        "max_mft": str(max_mft_d),
        "treasury_mft_address": _env("MAFUTANI_OFFRAMP_TREASURY_MFT_ADDRESS", "").strip(),
        "auto_submit_daraja": _env("MAFUTANI_MPESA_OFFRAMP_AUTO_SUBMIT", "").strip() == "1",
        "currency": "KES",
        "provider": "daraja",
        "disclaimer": (
            "Operator-configured reference conversion and third-party payout path. "
            "Not a market price, quote, guarantee, or settlement confirmation. "
            "Request acceptance is NOT settlement. NOT a compliance certification. "
            "Operator must handle callbacks/reconciliation, idempotency, credentials, and KYC/AML/sanctions/tax/legal compliance."
        ),
        "guarantees": False,
        "compliance_certified": False,
        "liquidity_guaranteed": False,
        "value_guaranteed": False,
    }


def ensure_schema(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mft_mpesa_requests ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id INTEGER NOT NULL,"
        " mft_address TEXT NOT NULL DEFAULT '',"
        " phone TEXT NOT NULL,"
        " amount_mft TEXT NOT NULL,"
        " amount_kes INTEGER NOT NULL,"
        " rate_kes_per_mft TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " daraja_response TEXT NOT NULL DEFAULT '{}',"
        " conversation_id TEXT NOT NULL DEFAULT '',"
        " originator_conversation_id TEXT NOT NULL DEFAULT '',"
        " note TEXT NOT NULL DEFAULT '',"
        " admin_note TEXT NOT NULL DEFAULT '',"
        " created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),"
        " updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),"
        " confirmed_at INTEGER)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mft_mpesa_user ON mft_mpesa_requests(user_id, id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_mft_mpesa_status ON mft_mpesa_requests(status, id)"
    )
    # idempotency (best-effort; prevents accidental duplicate user submissions)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(mft_mpesa_requests)").fetchall()]
        if "idempotency_key" not in cols:
            conn.execute("ALTER TABLE mft_mpesa_requests ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mft_mpesa_idem ON mft_mpesa_requests(user_id, idempotency_key)")
    except Exception:
        pass


def _dec_amount_mft(v):
    try:
        d = Decimal(str(v).strip())
    except Exception:
        raise OfframpError("invalid MFT amount")
    if d <= 0:
        raise OfframpError("MFT amount must be positive")
    # keep reasonable precision (MFT base units are 10^8; allow decimals)
    return d.quantize(Decimal("0.00000001"))


def _kes_for_mft(amount_mft, rate_kes_per_mft):
    try:
        rate = Decimal(str(rate_kes_per_mft))
        amt = Decimal(str(amount_mft))
    except Exception:
        raise OfframpError("invalid conversion inputs")
    if rate <= 0:
        raise OfframpError("M-Pesa off-ramp rate is not configured")
    kes = (amt * rate).quantize(Decimal("1"), rounding=ROUND_DOWN)
    if kes <= 0:
        raise OfframpError("amount too small after KES conversion")
    return int(kes)


def create_request(conn, user_id, phone, amount_mft, mft_address="", note="", idempotency_key="",
                   daraja_client=None):
    ensure_schema(conn)
    idem = str(idempotency_key or "").strip()[:120]
    if idem:
        existing = conn.execute(
            "SELECT * FROM mft_mpesa_requests WHERE user_id=? AND idempotency_key=? ORDER BY id DESC LIMIT 1",
            (int(user_id), idem)).fetchone()
        if existing:
            row = _row(existing)
            row["idempotent_replay"] = True
            row["disclaimer"] = "Duplicate submission detected for idempotency_key; returning existing request. Acceptance is not settlement. Not compliance-certified."
            return row
    cfg = config()
    if not cfg["rate_configured"]:
        raise OfframpError(
            "M-Pesa off-ramp is not configured (set MAFUTANI_MFT_KES_RATE)")
    amt = _dec_amount_mft(amount_mft)
    min_mft = Decimal(cfg["min_mft"])
    max_mft = Decimal(cfg["max_mft"])
    if amt < min_mft or amt > max_mft:
        raise OfframpError("amount must be between %s and %s MFT" % (min_mft, max_mft))
    client = daraja_client or DarajaClient()
    msisdn = client.normalize_msisdn(phone)
    kes = _kes_for_mft(amt, cfg["rate_kes_per_mft"])
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO mft_mpesa_requests (user_id, mft_address, phone, amount_mft,"
        " amount_kes, rate_kes_per_mft, status, note, idempotency_key, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (int(user_id), str(mft_address or "")[:200], msisdn, str(amt), kes,
         cfg["rate_kes_per_mft"], STATUS_PENDING, str(note or "")[:500], idem, now, now),
    )
    conn.commit()
    req_id = cur.lastrowid
    row = get_request(conn, req_id, user_id=user_id)

    # Auto-submit only when explicitly enabled AND Daraja is ready. Still records
    # everything and never claims settlement.
    if cfg.get("auto_submit_daraja") and client.status().get("b2c_ready"):
        try:
            row = submit_request(conn, req_id, admin_user_id=None, daraja_client=client,
                                 remarks="CommCoin M-Pesa off-ramp #%s" % req_id)
        except Exception as e:
            # keep pending request, record error in note; caller sees pending
            conn.execute(
                "UPDATE mft_mpesa_requests SET admin_note=?, updated_at=? WHERE id=?",
                ("auto-submit failed: %s" % str(e)[:400], int(time.time()), req_id),
            )
            conn.commit()
            row = get_request(conn, req_id, user_id=user_id)
            row["auto_submit_error"] = str(e)
    return row


def submit_request(conn, req_id, admin_user_id=None, daraja_client=None, remarks=None):
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM mft_mpesa_requests WHERE id=?", (int(req_id),)).fetchone()
    if not row:
        raise OfframpError("off-ramp request not found")
    row = dict(row)
    if row["status"] not in (STATUS_PENDING, STATUS_FAILED):
        raise OfframpError("request is %s" % row["status"])
    client = daraja_client or DarajaClient()
    res = client.b2c_payment(
        row["phone"], row["amount_kes"],
        remarks=(remarks or "CommCoin M-Pesa off-ramp #%s" % req_id)[:100],
        occasion="CommCoin",
        originator_conversation_id="MFT%s%s" % (req_id, int(time.time()) % 100000),
    )
    now = int(time.time())
    status = STATUS_SUBMITTED if res.get("ok") else STATUS_FAILED
    conn.execute(
        "UPDATE mft_mpesa_requests SET status=?, daraja_response=?, conversation_id=?,"
        " originator_conversation_id=?, updated_at=? WHERE id=?",
        (status, json.dumps(res, default=str)[:8000],
         str(res.get("conversation_id") or "")[:120],
         str(res.get("originator_conversation_id") or "")[:120],
         now, int(req_id)),
    )
    conn.commit()
    return get_request(conn, req_id)


def get_request(conn, req_id, user_id=None):
    ensure_schema(conn)
    if user_id is None:
        row = conn.execute("SELECT * FROM mft_mpesa_requests WHERE id=?", (int(req_id),)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM mft_mpesa_requests WHERE id=? AND user_id=?",
            (int(req_id), int(user_id)),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["daraja_response"] = json.loads(d.get("daraja_response") or "{}")
    except Exception:
        pass
    return d


def list_requests(conn, user_id=None, status=None, limit=100):
    ensure_schema(conn)
    q = "SELECT * FROM mft_mpesa_requests"
    args = []
    where = []
    if user_id is not None:
        where.append("user_id=?"); args.append(int(user_id))
    if status:
        where.append("status=?"); args.append(str(status))
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    rows = conn.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["daraja_response"] = json.loads(d.get("daraja_response") or "{}")
        except Exception:
            pass
        out.append(d)
    return out


def update_status(conn, req_id, status, admin_note=""):
    ensure_schema(conn)
    if status not in (STATUS_PENDING, STATUS_SUBMITTED, STATUS_COMPLETED, STATUS_FAILED, STATUS_REJECTED):
        raise OfframpError("invalid status")
    now = int(time.time())
    confirmed = now if status == STATUS_COMPLETED else None
    conn.execute(
        "UPDATE mft_mpesa_requests SET status=?, admin_note=?, updated_at=?, confirmed_at=COALESCE(?, confirmed_at) WHERE id=?",
        (status, str(admin_note or "")[:500], now, confirmed, int(req_id)),
    )
    conn.commit()
    return get_request(conn, req_id)
