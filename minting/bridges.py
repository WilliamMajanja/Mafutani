"""Bridge abstractions for off-ramp (zero-dependency)."""


class BridgeConfig:
    def __init__(self, name, chain, enabled=True, **kw):
        self.name = name
        self.chain = chain
        self.enabled = enabled
        self.extra = kw


class OfframpBridge:
    def __init__(self, config=None):
        self.config = config or {}

    def list(self):
        bridges = [
            {
                "name": "mafutani",
                "chain": "mafutani",
                "enabled": True,
                "type": "crypto",
                "notes": "Native CommCoin/MFT chain: hash-based PQC, SBC-mineable PoW, epoch seed/expiry, Merkle abstractions. Experimental.",
            },
            {
                "name": "mpesa_daraja",
                "chain": "fiat",
                "enabled": True,
                "type": "mobile_money",
                "provider": "Safaricom Daraja",
                "currency": "KES",
                "locale": "global-configurable",
                "notes": "Operator-configured. Requires Daraja credentials, callbacks/reconciliation, and KYC/AML/compliance controls.",
            },
        ]
        try:
            from minting.daraja import DarajaClient

            st = DarajaClient().status()
            for b in bridges:
                if b["name"] == "mpesa_daraja":
                    b["configured"] = st.get("configured")
                    b["b2c_ready"] = st.get("b2c_ready")
                    b["env"] = st.get("env")
        except Exception:
            pass
        return bridges
