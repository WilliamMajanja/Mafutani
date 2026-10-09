"""Global markets configuration.

Single source of truth for the world's trading regions, their sessions
(UTC open/close), the venues inside each region, and asset-class coverage.
Used by the API (`/api/markets`, screener `region=` filter) and the UI so
"Europe", "Asia-Pacific", "Africa" and the Americas are first-class config
rather than hardcoded strings scattered across the codebase.

Pure stdlib, import-safe, no DB access.
"""

# Region code -> metadata. `tz` is the canonical IANA timezone for the
# region's primary session open.
REGIONS = {
    "AMER": {
        "code": "AMER",
        "name": "Americas",
        "tz": "America/New_York",
        "open_utc": 13.5,   # 09:30 America/New_York (EDT) in UTC hours
        "close_utc": 20.0,  # 16:00 America/New_York (EDT) in UTC hours
        "venues": ["NASDAQ", "EBS", "ICE", "CBOT", "CME", "COMEX", "NYMEX"],
        "asset_classes": ["equity", "commodity", "fx", "crypto", "index"],
    },
    "EMEA": {
        "code": "EMEA",
        "name": "Europe / Middle East / Africa",
        "tz": "Europe/London",
        "open_utc": 8.0,    # 08:00 London (BST) in UTC hours
        "close_utc": 16.5,  # 16:30 London (BST) in UTC hours
        "venues": ["LME", "EURONEXT", "EUREX", "ICE-EU", "JSE", "JPMORGAN-AFRICA"],
        "asset_classes": ["equity", "commodity", "fx", "index", "rate"],
    },
    "APAC": {
        "code": "APAC",
        "name": "Asia-Pacific",
        "tz": "Asia/Singapore",
        "open_utc": 1.0,    # 09:00 Singapore (SGT) in UTC hours
        "close_utc": 9.0,   # 17:00 Singapore (SGT) in UTC hours
        "venues": ["TSE", "HKEX", "SGX", "ASX", "KRX", "NSE", "BINANCE", "COINBASE"],
        "asset_classes": ["equity", "crypto", "fx", "index", "commodity"],
    },
}

# Aliases so callers can use friendly names ("europe", "africa", "asia").
ALIASES = {
    "america": "AMER", "americas": "AMER", "us": "AMER", "amer": "AMER",
    "eu": "EMEA", "europe": "EMEA", "emea": "EMEA", "uk": "EMEA", "london": "EMEA",
    "africa": "EMEA", "south-africa": "EMEA", "jse": "EMEA",
    "asia": "APAC", "apac": "APAC", "asia-pacific": "APAC", "pacific": "APAC",
    "middle-east": "EMEA", "me": "EMEA",
}

# Venue code -> region. Seeded exchanges that are not in the lists above
# fall back to their DB `region` column.
VENUE_REGION = {}
for _r in REGIONS.values():
    for _v in _r["venues"]:
        VENUE_REGION[_v] = _r["code"]


def normalize_region(value):
    """'europe' | 'EMEA' | 'asia' | None -> canonical code or None."""
    if not value:
        return None
    v = str(value).strip()
    if not v:
        return None
    up = v.upper()
    if up in REGIONS:
        return up
    return ALIASES.get(v.lower())


def is_open(code, hour_utc):
    """True if the region's session is open at the given UTC hour."""
    r = REGIONS.get(normalize_region(code) or "")
    if r is None:
        return None  # unknown region
    o, c = r["open_utc"], r["close_utc"]
    h = float(hour_utc)
    if o <= c:
        return o <= h < c
    return h >= o or h < c  # session crosses midnight UTC (APAC)


def market_hours(code, hour_utc=None):
    """Session status dict for a region at the given UTC hour."""
    rc = normalize_region(code)
    r = REGIONS.get(rc)
    if r is None:
        return {"region": None, "open": None}
    if hour_utc is None:
        import time
        hour_utc = (time.time() % 86400) / 3600.0
    return {
        "region": rc,
        "name": r["name"],
        "tz": r["tz"],
        "open_utc": r["open_utc"],
        "close_utc": r["close_utc"],
        "open": bool(is_open(rc, hour_utc)),
    }


def all_markets():
    """Full config payload for /api/markets."""
    out = []
    for code, r in REGIONS.items():
        out.append({
            "code": code, "name": r["name"], "tz": r["tz"],
            "open_utc": r["open_utc"], "close_utc": r["close_utc"],
            "venues": list(r["venues"]),
            "asset_classes": list(r["asset_classes"]),
        })
    return out


def region_for_venue(venue_code, db_region=""):
    """Map a venue to a region.

    Order of truth: explicit config (VENUE_REGION) -> the venue's DB
    `region` column -> a coarse match on the raw region string. Returns
    None only when the venue genuinely cannot be placed.
    """
    v = (venue_code or "").strip().upper()
    if v in VENUE_REGION:
        return VENUE_REGION[v]
    rc = normalize_region(db_region)
    if rc:
        return rc
    raw = (db_region or "").strip().upper()
    for needle, code in (("US", "AMER"), ("AMERICA", "AMER"),
                         ("UK", "EMEA"), ("EU", "EMEA"), ("LONDON", "EMEA"),
                         ("JSE", "EMEA"), ("ASIA", "APAC"), ("SG", "APAC"),
                         ("HK", "APAC"), ("JP", "APAC"), ("AU", "APAC")):
        if needle in raw:
            return code
    return None
