"""Tier entitlements. Stdlib only, import-safe (no server/db imports)."""
import json

# 'forecast' is an entitlement but is deliberately absent from every tier's
# `methods_allowed`. Those two lists mean different things: an entitlement
# controls whether a *page* is reachable, a method is what a *backtest or
# signal* may run. Forecasts get the first and not the second, so no
# strategy, signal or backtest can be built on a model's output. That is the
# mechanism behind the display-only rule -- not a convention, and not a check
# somewhere downstream that could be forgotten.

ENTITLEMENTS = {
    "Free": {
        "max_symbols": 10,
        "live_enabled": False,
        "arbitrage": False,
        # Forecasts cost a model server, so they ride the paid tiers.
        "forecast": False,
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
        "forecast": True,
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
        "forecast": True,
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
        "forecast": True,
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
        "forecast": e["forecast"],
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
