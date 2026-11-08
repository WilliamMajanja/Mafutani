"""Zero-dependency KYC workflow hooks for CommCoin off-ramps (stdlib only).

This is TECHNICAL PLUMBING for operator review/audit: identity case records,
status, document/reference hashes, and off-ramp gating hooks. It is NOT a
substitute for legal/regulatory compliance, qualified identity verification,
sanctions/AML screening, or professional advice. Operators are responsible for
lawful use and compliance in their jurisdictions.
"""
import hashlib
import json
import os
import time

STATUS_NONE = "none"
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_RESTRICTED = "restricted"
STATUSES = {STATUS_NONE, STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_RESTRICTED}


def _env(name, default=""):
    v = os.environ.get(name)
    return default if v is None else str(v)


def required_for_offramp():
    # Explicit operator opt-in: off-ramp can require approved KYC.
    return _env("MAFUTANI_KYC_REQUIRED_FOR_OFFRAMP", "").strip() == "1"


def ensure_schema(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mft_kyc_cases ("
        " user_id INTEGER PRIMARY KEY,"
        " status TEXT NOT NULL DEFAULT 'none',"
        " full_name TEXT NOT NULL DEFAULT '',"
        " country TEXT NOT NULL DEFAULT '',"
        " doc_type TEXT NOT NULL DEFAULT '',"
        " doc_ref_hash TEXT NOT NULL DEFAULT '',"
        " notes TEXT NOT NULL DEFAULT '',"
        " reviewer_notes TEXT NOT NULL DEFAULT '',"
        " submitted_at INTEGER,"
        " reviewed_at INTEGER,"
        " updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now')))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS mft_kyc_events ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " user_id INTEGER NOT NULL,"
        " actor_id INTEGER,"
        " action TEXT NOT NULL,"
        " details TEXT NOT NULL DEFAULT '{}',"
        " ts INTEGER NOT NULL DEFAULT (strftime('%s','now')))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mft_kyc_events_user ON mft_kyc_events(user_id,id)")


def hash_ref(value):
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def get_case(conn, user_id):
    ensure_schema(conn)
    r = conn.execute("SELECT * FROM mft_kyc_cases WHERE user_id=?", (int(user_id),)).fetchone()
    if not r:
        return {"user_id": int(user_id), "status": STATUS_NONE, "required_for_offramp": required_for_offramp()}
    d = dict(r)
    d["required_for_offramp"] = required_for_offramp()
    return d


def submit(conn, user_id, full_name="", country="", doc_type="", doc_ref="", notes=""):
    ensure_schema(conn)
    now = int(time.time())
    doc_hash = hash_ref(doc_ref) if doc_ref else ""
    conn.execute(
        "INSERT INTO mft_kyc_cases (user_id,status,full_name,country,doc_type,doc_ref_hash,notes,submitted_at,updated_at)"
        " VALUES (?,?,?,?,?,?,?, ?,?)"
        " ON CONFLICT(user_id) DO UPDATE SET status=excluded.status, full_name=excluded.full_name,"
        " country=excluded.country, doc_type=excluded.doc_type, doc_ref_hash=CASE WHEN excluded.doc_ref_hash!='' THEN excluded.doc_ref_hash ELSE mft_kyc_cases.doc_ref_hash END,"
        " notes=excluded.notes, submitted_at=excluded.submitted_at, updated_at=excluded.updated_at",
        (int(user_id), STATUS_PENDING, str(full_name or "")[:200], str(country or "")[:120],
         str(doc_type or "")[:120], doc_hash, str(notes or "")[:1000], now, now),
    )
    conn.execute("INSERT INTO mft_kyc_events (user_id,actor_id,action,details,ts) VALUES (?,?,?,?,?)",
                 (int(user_id), int(user_id), "submit", json.dumps({"country": country, "doc_type": doc_type, "doc_ref_hash": doc_hash})[:4000], now))
    conn.commit()
    return get_case(conn, user_id)


def review(conn, user_id, status, actor_id=None, reviewer_notes=""):
    ensure_schema(conn)
    if status not in STATUSES or status == STATUS_NONE:
        raise ValueError("invalid status")
    now = int(time.time())
    conn.execute(
        "INSERT INTO mft_kyc_cases (user_id,status,updated_at,reviewed_at,reviewer_notes) VALUES (?,?,?,?,?)"
        " ON CONFLICT(user_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at,"
        " reviewed_at=excluded.reviewed_at, reviewer_notes=excluded.reviewer_notes",
        (int(user_id), status, now, now, str(reviewer_notes or "")[:1000]),
    )
    conn.execute("INSERT INTO mft_kyc_events (user_id,actor_id,action,details,ts) VALUES (?,?,?,?,?)",
                 (int(user_id), actor_id, "review", json.dumps({"status": status, "reviewer_notes": reviewer_notes})[:4000], now))
    conn.commit()
    return get_case(conn, user_id)


def list_cases(conn, status=None, limit=200):
    ensure_schema(conn)
    q = "SELECT * FROM mft_kyc_cases"
    args = []
    if status:
        q += " WHERE status=?"; args.append(str(status))
    q += " ORDER BY updated_at DESC LIMIT ?"; args.append(int(limit))
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def check_offramp_allowed(conn, user_id):
    if not required_for_offramp():
        return True, None
    case = get_case(conn, user_id)
    if case.get("status") == STATUS_APPROVED:
        return True, None
    return False, "KYC approval required for off-ramp (status=%s). Submit KYC and await operator review. This is a workflow control, not legal compliance advice." % case.get("status")
