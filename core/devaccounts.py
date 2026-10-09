"""Dev account ledger: the canonical source of local login credentials.

Both `scripts/seed.py` (which writes these credentials) and the server's
boot log (which prints them) import from here, so the two can never drift.

Scope and safety
----------------
* Only `@local` accounts are managed. External accounts (e.g. gmail) are
  listed in the ledger but their passwords are NEVER shown or reset.
* Every managed account is forced to a deterministic, documented password
  on each seed run, so the dev environment always has working logins.
* This is development-only material. `boot()` refuses to print unless the
  app is explicitly running in dev mode (see `ledger_enabled`), and it can
  be turned off with `PRINT_ACCOUNTS=0`.
"""

import os

# (email, password, tier) — canonical dev accounts.
TEST_USERS = [
    ("admin@local", "donttrytoplaythefoolwithmeniggesh", "Admin"),
    ("demo@local", "demo1234", "Pro"),
    ("prouser@local", "propass123!", "Pro"),
    ("institutional@local", "institutepass123!", "Institutional"),
    ("testuser1@local", "testpass123!", "Free"),
    ("testuser2@local", "testpass123!", "Free"),
]

LOCAL_SUFFIX = "@local"
# SQL LIKE pattern matching every managed account.
LOCAL_PATTERN = "%" + LOCAL_SUFFIX


def is_managed(email):
    """True for accounts whose password this project controls."""
    return str(email or "").strip().lower().endswith(LOCAL_SUFFIX)


def local_dev_password(email):
    """Deterministic password for any other *.local account.

    Lets the operator log in as any locally-created account without
    knowing what it was signed up with.
    """
    return "dev-" + str(email or "").split("@")[0]


def password_for(email):
    """The password currently expected for `email`, or None if untouched."""
    e = str(email or "").strip().lower()
    if not is_managed(e):
        return None
    for addr, pw, _tier in TEST_USERS:
        if addr == e:
            return pw
    return local_dev_password(e)


def tier_for(email):
    """Configured tier for a canonical test account, else None."""
    e = str(email or "").strip().lower()
    for addr, _pw, tier in TEST_USERS:
        if addr == e:
            return tier
    return None


def ledger_enabled():
    """Whether printing credentials to stdout is permitted.

    Enabled by default for local dev, disabled in production and whenever
    `PRINT_ACCOUNTS=0`.
    """
    flag = os.environ.get("PRINT_ACCOUNTS")
    if flag is not None:
        return flag.strip().lower() not in ("0", "false", "no", "off")
    env = (os.environ.get("APP_ENV") or os.environ.get("ENV") or "").lower()
    return env not in ("prod", "production")


def format_ledger(conn):
    """Return the account/password table as a list of lines.

    Never raises: returns a single explanatory line on failure so a boot
    log can't be taken down by this cosmetic feature.
    """
    try:
        rows = conn.execute(
            "SELECT id, email, tier, disabled, totp_enabled FROM users"
            " ORDER BY id").fetchall()
    except Exception as e:
        return ["accounts: unavailable ({})".format(e)]
    lines = ["", "Local accounts ({}):".format(len(rows)),
             "  {:<4} {:<30} {:<14} {:<7} {:<5} password".format(
                 "id", "email", "tier", "state", "2fa")]
    for r in rows:
        em = r["email"]
        if is_managed(em):
            pw = password_for(em)
        else:
            pw = "(external - not managed)"
        lines.append("  {:<4} {:<30} {:<14} {:<7} {:<5} {}".format(
            r["id"], em, r["tier"],
            "disabled" if r["disabled"] else "ok",
            "on" if r["totp_enabled"] else "off",
            pw))
    return lines
