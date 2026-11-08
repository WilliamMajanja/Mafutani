"""Mafutani off-ramp integrations (stdlib only).

Current stance: CommCoin/MFT is the primary chain. Minima integration has been
removed. This package provides Daraja/M-Pesa payout integration and bridge
metadata for operator-configured off-ramps.
"""

from minting.bridges import BridgeConfig, OfframpBridge  # noqa: F401
from minting.daraja import DarajaClient, DarajaError  # noqa: F401

__all__ = [
    "BridgeConfig",
    "OfframpBridge",
    "DarajaClient",
    "DarajaError",
]
