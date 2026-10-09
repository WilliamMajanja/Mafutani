"""Tiered fee schedule. Stdlib only, import-safe."""

TIER_FEE_BPS = {
    "Free": 5.0,
    "Pro": 2.0,
    "Institutional": 1.0,
    "Admin": 0.0,
}

VALID_MODES = ("paper", "live")


def _norm_tier(tier):
    if not tier:
        return "Free"
    t = str(tier).strip()
    for name in TIER_FEE_BPS:
        if name.lower() == t.lower():
            return name
    return "Free"


def fee_bps(tier="Free", mode="paper"):
    """Return commission in basis points for a tier/mode pair.

    Paper simulates live pricing, so mode does not change the rate;
    both are accepted (unknown modes fall back to 'paper').
    """
    if mode not in VALID_MODES:
        mode = "paper"
    return float(TIER_FEE_BPS[_norm_tier(tier)])


def apply_fee(notional, tier="Free", mode="paper"):
    """Return absolute fee currency amount for a trade notional."""
    try:
        n = abs(float(notional))
    except (TypeError, ValueError):
        n = 0.0
    return n * fee_bps(tier, mode) / 10000.0
