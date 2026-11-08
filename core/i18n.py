"""Global locale/i18n support (zero-dependency).

Design goals:
- Locale negotiation: explicit param/user pref > Accept-Language > default.
- Message catalogs as JSON files under data/locales/<locale>.json, with
  fallback to language-only and then default locale.
- Simple interpolation: "Hello {name}".
- Formatting helpers (numbers/currency) use locale-aware rules where practical
  with stdlib; fall back safely.
- API returns locale metadata; web can fetch catalogs/messages.

This makes Mafutani globally locale-integratable without external i18n deps.
"""
import json
import os
import re

DEFAULT_LOCALE = "en"
SUPPORTED = ("en", "sw", "fr", "es", "ar", "pt", "de", "zh", "hi")
RTL = {"ar", "he", "fa", "ur"}

_LOCALE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "locales")
_CACHE = {}


def normalize_locale(tag):
    if not tag:
        return DEFAULT_LOCALE
    t = str(tag).strip().replace("_", "-")
    if not t:
        return DEFAULT_LOCALE
    # take primary language subtag; keep full tag if exact catalog exists
    return t


def parse_accept_language(header):
    """Parse Accept-Language into [(locale, q)] sorted by q desc."""
    if not header:
        return []
    out = []
    for part in str(header).split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^([A-Za-z0-9*-]+)(?:\s*;\s*q=([0-9.]+))?$", part)
        if not m:
            continue
        loc = normalize_locale(m.group(1))
        try:
            q = float(m.group(2)) if m.group(2) is not None else 1.0
        except Exception:
            q = 1.0
        out.append((loc, q))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def negotiate(accept_language=None, requested=None, user_locale=None, supported=None):
    sup = set(supported or SUPPORTED)
    for cand in (requested, user_locale):
        if not cand:
            continue
        c = normalize_locale(cand)
        if c in sup:
            return c
        base = c.split("-")[0].lower()
        if base in sup:
            return base
    for loc, _q in parse_accept_language(accept_language or ""):
        if loc == "*":
            return DEFAULT_LOCALE
        if loc in sup:
            return loc
        base = loc.split("-")[0].lower()
        if base in sup:
            return base
    return DEFAULT_LOCALE


def _load_catalog(locale):
    loc = normalize_locale(locale).split("-")[0].lower()
    if loc in _CACHE:
        return _CACHE[loc]
    path = os.path.join(_LOCALE_DIR, loc + ".json")
    data = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                data = {}
    except Exception:
        data = {}
    _CACHE[loc] = data
    return data


def t(key, locale=None, default=None, **kwargs):
    loc = negotiate(requested=locale)
    msg = _load_catalog(loc).get(key)
    if msg is None and loc != DEFAULT_LOCALE:
        msg = _load_catalog(DEFAULT_LOCALE).get(key)
    if msg is None:
        msg = default if default is not None else key
    if kwargs and isinstance(msg, str):
        try:
            msg = msg.format(**kwargs)
        except Exception:
            pass
    return msg


def catalog(locale=None):
    loc = negotiate(requested=locale)
    base = _load_catalog(DEFAULT_LOCALE).copy()
    if loc != DEFAULT_LOCALE:
        base.update(_load_catalog(loc))
    return base


def locale_info(locale=None):
    loc = negotiate(requested=locale)
    return {
        "locale": loc,
        "default": DEFAULT_LOCALE,
        "supported": list(SUPPORTED),
        "rtl": loc in RTL,
        "dir": "rtl" if loc in RTL else "ltr",
    }


def format_number(value, locale=None):
    try:
        n = float(value)
    except Exception:
        return str(value)
    loc = negotiate(requested=locale)
    # Simple locale-aware separators (stdlib-only, no babel).
    if loc in ("fr", "de", "es", "pt"):
        s = ("{:,.2f}".format(n)).replace(",", "X").replace(".", ",").replace("X", ".")
        return s
    return "{:,.2f}".format(n)


def format_currency(amount, currency="MFT", locale=None):
    return "{} {}".format(format_number(amount, locale), currency)
