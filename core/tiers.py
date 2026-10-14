"""Tier entitlements. Stdlib only, import-safe (no server/db imports)."""
import json

ENTITLEMENTS = {
    "Free": {
        "max_symbols": 10,
        "live_enabled": False,
        "arbitrage": False,
        "methods_allowed": ["ema_trend", "dual_ema"],
        "api_rate": 60,
    },
    "Pro": {
        "max_symbols": 50,
        "live_enabled": True,
        # The arbitrage scanner is Pro-gated because a scan reveals the
        # whole venue map: which venues this deployment watches and what
        # each is quoting at the same moment. Handing that to the
        # smallest tier turns the product into a free venue-price feed.
        # It is paper-only either way -- the gate is about the
        # information, not about the money.
        "arbitrage": True,
        "methods_allowed": [
            "ema_trend", "dual_ema", "vwap_pullback", "vwap_bands",
            "ema_trend_cot",
            "manual", "orb_breakout", "rsi_fade", "mean_reversion",
        ],
        "api_rate": 300,
    },
    "Institutional": {
        "max_symbols": 100,
        "live_enabled": True,
        "arbitrage": True,
        "methods_allowed": [
            "ema_trend", "dual_ema", "vwap_pullback", "vwap_bands",
            "manual", "orb_breakout", "rsi_fade", "mean_reversion",
            "ema_trend_cot", "book_imbalance", "news_momentum",
        ],
        "api_rate": 1200,
    },
    "Admin": {
        "max_symbols": 100,
        "live_enabled": True,
        "arbitrage": True,
        "methods_allowed": [
            "ema_trend", "dual_ema", "vwap_pullback", "vwap_bands",
            "manual", "orb_breakout", "rsi_fade", "mean_reversion",
            "ema_trend_cot", "book_imbalance", "news_momentum",
        ],
        "api_rate": 3600,
    },
}

TIER_ORDER = ["Free", "Pro", "Institutional", "Admin"]


def _norm_tier(tier):
    if not tier:
        return "Free"
    t = str(tier).strip()
    # case-insensitive match
    for name in ENTITLEMENTS:
        if name.lower() == t.lower():
            return name
    return "Free"


def get_entitlements(user_tier):
    t = _norm_tier(user_tier)
    e = ENTITLEMENTS[t]
    out = {
        "tier": t,
        "max_symbols": e["max_symbols"],
        "live_enabled": e["live_enabled"],
        "arbitrage": e["arbitrage"],
        "methods_allowed": list(e["methods_allowed"]),
        "api_rate": e["api_rate"],
    }
    # The backtest window is a single product-wide setting, not a tier
    # entitlement. It used to be a per-tier `backtest_days` (30/365/1825/
    # 3650) that nothing read -- all four tiers silently got the same
    # 20,000-bar cap -- so those numbers described a product that did not
    # exist. Reporting the one real window is honest; reporting four fake
    # ones was not.
    try:
        from engines.backtest import BACKTEST_WINDOW_DAYS
        out["backtest_window_days"] = BACKTEST_WINDOW_DAYS
    except Exception:
        pass
    return out


def has_entitlement(user_tier, feature):
    """Boolean check. `feature` is an entitlement key like 'live_enabled'."""
    t = _norm_tier(user_tier)
    return bool(ENTITLEMENTS[t].get(feature))


def require_entitlement(user_tier, feature):
    """Raise PermissionError(403-style) if tier lacks feature. Else return True."""
    if not has_entitlement(user_tier, feature):
        raise PermissionError("tier '{}' lacks entitlement '{}'".format(_norm_tier(user_tier), feature))
    return True


def _strategy_name(strategy):
    if isinstance(strategy, str):
        return strategy
    if isinstance(strategy, dict):
        return strategy.get("name", "")
    try:
        return strategy["name"]
    except Exception:
        return ""


def can_use_strategy(user_tier, strategy):
    """True if strategy (name or row dict) is in tier's methods_allowed."""
    t = _norm_tier(user_tier)
    name = _strategy_name(strategy)
    return name in ENTITLEMENTS[t]["methods_allowed"]


def require_strategy(user_tier, strategy):
    if not can_use_strategy(user_tier, strategy):
        raise PermissionError(
            "tier '{}' cannot use strategy '{}'".format(_norm_tier(user_tier), _strategy_name(strategy))
        )
    return True


def can_use_live(user_tier):
    return bool(ENTITLEMENTS[_norm_tier(user_tier)]["live_enabled"])


def require_live(user_tier):
    return require_entitlement(user_tier, "live_enabled")


def tier_at_least(user_tier, minimum):
    """True if user_tier >= minimum in TIER_ORDER."""
    try:
        return TIER_ORDER.index(_norm_tier(user_tier)) >= TIER_ORDER.index(_norm_tier(minimum))
    except ValueError:
        return False


# ---------------------------------------------------------------- max_symbols
#
# RFC-001 lists max_symbols in the entitlement table and every tier sets a
# value, but until now nothing read it: /api/me/entitlements advertised it,
# no code consulted it. This is the same shape as `backtest_days`, which was
# removed from the map rather than left as a number describing a product that
# did not exist (see get_entitlements). The difference is that this one is
# enforceable at a real boundary, so it is enforced rather than deleted.
#
# The boundary is watchlist creation, not reads. A user declares which
# symbols they track when they build a watchlist; that is the only place they
# assert a symbol set. Capping *reads* would instead mean hiding quotes from
# a Free user, which breaks their dashboard outright over a number that was
# never priced. Track-set is the thing the limit is actually about.
def max_symbols(user_tier):
    """How many distinct symbols this tier may track. Never zero."""
    try:
        n = int(ENTITLEMENTS[_norm_tier(user_tier)].get("max_symbols", 0))
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else 1


def tracked_symbols(conn, user_id):
    """Distinct symbols across every watchlist the user owns.

    Distinct, not summed: the same symbol on two watchlists is one symbol
    the user is tracking. Counting occurrences would let a Free user with a
    10-symbol limit hold 30 by putting the same 10 on three lists.
    """
    import json as _json
    out = set()
    try:
        rows = conn.execute(
            "SELECT symbols_json FROM watchlists WHERE user_id = ?",
            (user_id,)).fetchall()
    except Exception:
        return out
    for r in rows:
        try:
            syms = _json.loads(r[0] or "[]")
        except Exception:
            continue
        if not isinstance(syms, list):
            continue
        for s in syms:
            s = str(s or "").strip().upper()
            if s:
                out.add(s)
    return out


def check_symbol_budget(conn, user_id, user_tier, incoming):
    """Would adding `incoming` push this user past their tier's max_symbols?

    Returns (ok, reason). `reason` is empty when ok, and names the limit and
    the tier when not -- a refusal that does not say what the limit is makes
    the caller guess, and the guess is usually "this is broken".
    """
    limit = max_symbols(user_tier)
    existing = tracked_symbols(conn, user_id)
    wanted = set()
    for s in (incoming or []):
        s = str(s or "").strip().upper()
        if s:
            wanted.add(s)
    merged = existing | wanted
    if len(merged) <= limit:
        return True, ""
    return False, (
        "{} tier may track {} symbols; this request would make {} "
        "({} already tracked, {} new). Remove symbols or upgrade."
        .format(_norm_tier(user_tier), limit, len(merged),
                len(existing), len(wanted - existing)))
