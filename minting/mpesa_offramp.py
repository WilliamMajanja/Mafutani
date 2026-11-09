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
    def _env_int(name, default):
        try: return int(_env(name, str(default)))
        except Exception: return int(default)
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
        "limits": {
            "max_pending_per_user": _env_int("MAFUTANI_OFFRAMP_MAX_PENDING_PER_USER", 5),
            "cooldown_seconds": _env_int("MAFUTANI_OFFRAMP_COOLDOWN_SECONDS", 0),
            "max_requests_per_day": _env_int("MAFUTANI_OFFRAMP_MAX_PER_DAY", 0),
        },
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
        for col, ddl in [("idempotency_key","TEXT NOT NULL DEFAULT ''"), ("approved_by","INTEGER"), ("approved_at","INTEGER"), ("approval_note","TEXT NOT NULL DEFAULT ''")]:
            if col not in cols:
                conn.execute("ALTER TABLE mft_mpesa_requests ADD COLUMN %s %s" % (col, ddl))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_mft_mpesa_idem ON mft_mpesa_requests(user_id, idempotency_key)")
    except Exception:
        pass
    # Callback reconciliation scaffold (raw events; authenticity NOT verified by this code).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mft_daraja_callbacks ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " received_at INTEGER NOT NULL,"
        " path TEXT NOT NULL DEFAULT '',"
        " remote_note TEXT NOT NULL DEFAULT '',"
        " payload_json TEXT NOT NULL DEFAULT '{}',"
        " conversation_id TEXT NOT NULL DEFAULT '',"
        " originator_conversation_id TEXT NOT NULL DEFAULT '',"
        " result_code TEXT NOT NULL DEFAULT '',"
        " matched_request_id INTEGER,"
        " reconcile_status TEXT NOT NULL DEFAULT 'received',"
        " note TEXT NOT NULL DEFAULT '' )")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mft_daraja_cb_conv ON mft_daraja_callbacks(conversation_id, id)")



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
            row = dict(existing) if hasattr(existing,"keys") else {}
            row["idempotent_replay"] = True
            row["disclaimer"] = "Duplicate submission detected for idempotency_key; returning existing request. Acceptance is not settlement. Not compliance-certified."
            return row
    cfg = config()
    # Preventive velocity/cooldown controls (not compliance guarantees).
    lim = cfg.get("limits") or {}
    now_ts = int(time.time())
    max_pending = int(lim.get("max_pending_per_user") or 0)
    if max_pending > 0:
        n = conn.execute("SELECT COUNT(*) AS c FROM mft_mpesa_requests WHERE user_id=? AND status IN ('pending','submitted')",
                         (int(user_id),)).fetchone()
        if n and int(n["c"] if hasattr(n,"keys") else n[0]) >= max_pending:
            raise OfframpError("too many pending off-ramp requests (limit %s); not a compliance guarantee" % max_pending)
    cooldown = int(lim.get("cooldown_seconds") or 0)
    if cooldown > 0:
        last = conn.execute("SELECT created_at FROM mft_mpesa_requests WHERE user_id=? ORDER BY id DESC LIMIT 1",
                            (int(user_id),)).fetchone()
        if last:
            la = int(last["created_at"] if hasattr(last,"keys") else last[0])
            if now_ts - la < cooldown:
                raise OfframpError("off-ramp cooldown active (%ss remaining); preventive control only" % max(1, cooldown - (now_ts-la)))
    max_day = int(lim.get("max_requests_per_day") or 0)
    if max_day > 0:
        day_ago = now_ts - 86400
        n = conn.execute("SELECT COUNT(*) AS c FROM mft_mpesa_requests WHERE user_id=? AND created_at>=?",
                         (int(user_id), day_ago)).fetchone()
        if n and int(n["c"] if hasattr(n,"keys") else n[0]) >= max_day:
            raise OfframpError("daily off-ramp request limit reached (%s); preventive control only" % max_day)
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
    # Maker-checker hook (preventive control; not compliance/guarantee). When enabled, require
    # a recorded approval by a different admin before submit.
    if str(_env("MAFUTANI_OFFRAMP_MAKER_CHECKER", "")).strip() == "1":
        approved_by = row.get("approved_by")
        if not approved_by:
            raise OfframpError("maker-checker enabled: request must be approved before submit (POST /approve)")
        if admin_user_id is not None and int(approved_by) == int(admin_user_id):
            raise OfframpError("maker-checker enabled: approver and submitter must differ (preventive control)")
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


def record_callback(conn, payload, path="", remote_note=""):
    """Store raw Daraja callback payload for operator reconciliation.
    NOT a settlement confirmation. Authenticity is NOT verified here - operators must
    protect the callback endpoint (network controls, TLS, Daraja practices) and reconcile."""
    ensure_schema(conn)
    if not isinstance(payload, dict):
        payload = {"raw": str(payload)[:4000]}
    raw = json.dumps(payload, default=str)[:12000]
    # best-effort extraction of common Daraja fields
    conv = orig = result = ""
    try:
        res = payload.get("Result") or payload.get("result") or {}
        if isinstance(res, dict):
            conv = str(res.get("ConversationID") or res.get("conversationID") or "")
            orig = str(res.get("OriginatorConversationID") or res.get("originatorConversationID") or "")
            result = str(res.get("ResultCode") if res.get("ResultCode") is not None else res.get("resultCode") or "")
        conv = conv or str(payload.get("ConversationID") or payload.get("conversationID") or "")
        orig = orig or str(payload.get("OriginatorConversationID") or payload.get("originatorConversationID") or "")
    except Exception:
        pass
    matched = None
    if conv or orig:
        row = conn.execute(
            "SELECT id FROM mft_mpesa_requests WHERE (conversation_id<>'' AND conversation_id=?) OR (originator_conversation_id<>'' AND originator_conversation_id=?) ORDER BY id DESC LIMIT 1",
            (conv, orig)).fetchone()
        if row:
            matched = int(row["id"] if hasattr(row,"keys") else row[0])
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO mft_daraja_callbacks(received_at,path,remote_note,payload_json,conversation_id,originator_conversation_id,result_code,matched_request_id,reconcile_status,note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (now, str(path or "")[:200], str(remote_note or "")[:200], raw, conv[:120], orig[:120], str(result)[:40], matched,
         "matched" if matched else "received",
         "Callback stored for reconciliation. Authenticity NOT verified. Acceptance/result is NOT settlement."))
    conn.commit()
    return {"ok": True, "callback_id": cur.lastrowid, "matched_request_id": matched,
            "disclaimer": "Callback stored for reconciliation only. Authenticity not verified; not settlement; not compliance-certified."}


def list_callbacks(conn, limit=100):
    ensure_schema(conn)
    rows = conn.execute("SELECT * FROM mft_daraja_callbacks ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    out = []
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else {}
        try:
            d["payload"] = json.loads(d.get("payload_json") or "{}")
        except Exception:
            d["payload"] = {}
        d.pop("payload_json", None)
        out.append(d)
    return out


def approve_request(conn, req_id, admin_user_id, note=""):
    ensure_schema(conn)
    now = int(time.time())
    conn.execute("UPDATE mft_mpesa_requests SET approved_by=?, approved_at=?, approval_note=?, updated_at=? WHERE id=?",
                 (int(admin_user_id), now, str(note or "")[:500], now, int(req_id)))
    conn.commit()
    return get_request(conn, req_id)
