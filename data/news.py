"""Free public news provider. Stdlib only (urllib + ElementTree).

Sources (no keys, same philosophy as ErTasselli/OpenTerminal):
  - Yahoo Finance RSS per symbol (primary)
  - Google News RSS per symbol (fallback / merge)
Results are de-duplicated by normalized title and cached (TTL 10 min)
with stale-while-revalidate: a failed refresh still serves stale items.
"""

import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import OrderedDict

TTL = 600
_TIMEOUT = 8
_UA = {"User-Agent": "mafutani-tcc/1.1 (personal-research; contact: local)"}

_CACHE = {}  # key -> (ts, items)


def _get(url):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return r.read()


def _parse_rss(raw, default_source):
    items = []
    try:
        root = ET.fromstring(raw)
    except Exception:
        return items
    for it in root.iter("item"):
        try:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub = (it.findtext("pubDate") or "").strip()
            src_el = it.find("source")
            src = (src_el.text.strip() if src_el is not None and src_el.text else "")
            if not title or not link:
                continue
            ts = _parse_date(pub)
            items.append({"title": title, "link": link, "ts": ts,
                          "published": pub, "source": src or default_source})
        except Exception:
            continue
    return items


def _parse_date(s):
    if not s:
        return 0
    try:
        from email.utils import parsedate_to_datetime
        return int(parsedate_to_datetime(s).timestamp())
    except Exception:
        return 0


def _norm(title):
    return "".join(ch.lower() for ch in title if ch.isalnum())


def dedupe(items):
    seen = OrderedDict()
    for it in items:
        k = _norm(it.get("title", ""))
        if k and k not in seen:
            seen[k] = it
    return list(seen.values())


# symbol -> natural-language keyword so commodity/fx symbols still surface
# usable headlines (Yahoo RSS is thin for XAUUSD/NGAS-style tickers)
COMMODITY_KEYWORDS = {
    "XAUUSD": "gold price", "XAGUSD": "silver price", "XPTUSD": "platinum price",
    "XPDUSD": "palladium price", "WTI": "crude oil WTI", "BRENT": "brent crude oil",
    "NGAS": "natural gas prices", "COPPER": "copper price",
    "WHEAT": "wheat futures", "CORN": "corn futures",
    "COFFEE": "coffee futures arabica", "COCOA": "cocoa prices",
    "ALUM": "aluminium price LME", "NICKEL": "nickel price LME",
    "ZINC": "zinc price LME", "TIN": "tin price LME", "LEAD": "lead price LME",
    "GASOLINE": "RBOB gasoline futures", "HEATOIL": "heating oil futures",
    "SOYBEANS": "soybean futures", "SOYMEAL": "soybean meal futures",
    "SOYOIL": "soybean oil futures", "OATS": "oats futures", "RRICE": "rough rice futures",
    "SUGAR": "sugar futures price", "COTTON": "cotton futures",
    "OJ": "orange juice futures", "LCATTLE": "live cattle futures",
    "LHOGS": "lean hogs futures", "FCATTLE": "feeder cattle futures",
    "EURUSD": "euro dollar forex", "BTCUSD": "bitcoin", "ETHUSD": "ethereum",
}


def _fetch_symbol(symbol):
    sym = urllib.parse.quote(str(symbol).upper())
    out = []
    try:
        out += _parse_rss(_get(
            "https://feeds.finance.yahoo.com/rss/2.0/headline?s={}&region=US&lang=en-US".format(sym)),
            "Yahoo Finance")
    except Exception:
        pass
    try:
        out += _parse_rss(_get(
            "https://news.google.com/rss/search?q={}%20stock%20market&hl=en-US&gl=US&ceid=US%3Aen".format(sym)),
            "Google News")
    except Exception:
        pass
    kw = COMMODITY_KEYWORDS.get(str(symbol).upper())
    if kw and len(out) < 10:
        try:
            out += _parse_rss(_get(
                "https://news.google.com/rss/search?q={}&hl=en-US&gl=US&ceid=US%3Aen".format(
                    urllib.parse.quote(kw))),
                "Google News")
        except Exception:
            pass
    out.sort(key=lambda d: d.get("ts", 0), reverse=True)
    return dedupe(out)[:40]


def _fetch_global():
    out = []
    try:
        out += _parse_rss(_get(
            "https://news.google.com/rss/search?q=stock%20market&hl=en-US&gl=US&ceid=US%3Aen"),
            "Google News")
    except Exception:
        pass
    out.sort(key=lambda d: d.get("ts", 0), reverse=True)
    return dedupe(out)[:40]


def get_news(symbol=None, limit=20):
    """Return {items, updated_at, stale, symbol}. Never raises."""
    key = "sym:" + str(symbol or "").upper() if symbol else "global"
    try:
        limit = max(1, min(int(limit or 20), 50))
    except (TypeError, ValueError):
        limit = 20
    now = int(time.time())
    cached = _CACHE.get(key)
    if cached and now - cached[0] < TTL:
        return {"items": cached[1][:limit], "updated_at": cached[0],
                "stale": False, "symbol": symbol or ""}
    fresh = []
    try:
        fresh = _fetch_symbol(symbol) if symbol else _fetch_global()
    except Exception:
        fresh = []
    if fresh:
        _CACHE[key] = (now, fresh)
        return {"items": fresh[:limit], "updated_at": now,
                "stale": False, "symbol": symbol or ""}
    if cached:
        return {"items": cached[1][:limit], "updated_at": cached[0],
                "stale": True, "symbol": symbol or ""}
    return {"items": [], "updated_at": 0, "stale": True, "symbol": symbol or ""}
