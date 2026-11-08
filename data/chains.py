"""EVM chain + Tether (USD₮) token registry. Stdlib only, import-safe.

Single source of truth for the wallet page. The browser reads this via
``GET /api/chains`` rather than hardcoding addresses of its own: a token
address that exists in two places is a token address that silently drifts
in one of them, and on-chain reads that display a *balance* are exactly
the place where a wrong address must not be tolerated.

Addresses are the canonical ones published by Tether
(https://tether.to/en/supported-protocols/), except for BNB Smart Chain,
where bridged USD₮ representations are commonly used.

Two things this module deliberately does NOT do:

* It holds no keys, mnemonics or RPC secrets. Every read the wallet makes
  is an unsigned ``eth_call`` or ``eth_getBalance`` through the user's own
  injected provider, so there is nothing here worth stealing.
* It never signs or sends anything. There is no transfer path here at
  all. A wallet connection is read-only; a seed phrase must never be
  requested by, or transmitted to, this server.

``expected_decimals`` is documentation and a UI hint only. It is NOT
trusted for arithmetic: the wallet reads ``decimals()`` from the contract
and refuses to show a balance if the contract does not identify itself as
USDT. The two really do differ (6 on Ethereum, 18 on BNB Chain), so
hardcoding either would misreport balances by twelve orders of magnitude.
"""

# USD₮ canonical contract, per chain. Only EVM chains appear here; TRON's
# USD₮ (TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t) is not EVM and is therefore
# out of scope for an injected EVM provider.
USDT = "USDT"

CHAINS = (
    {
        "chain_id": 1,
        "hex": "0x1",
        "name": "Ethereum",
        "native_symbol": "ETH",
        "native_decimals": 18,
        "explorer": "https://etherscan.io",
        "rpc": None,          # the wallet's own provider; no server RPC needed
        "usdt_address": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "usdt_expected_decimals": 6,
        "note": "Canonical Tether-published USD₮ (ERC-20).",
    },
    {
        "chain_id": 56,
        "hex": "0x38",
        "name": "BNB Smart Chain",
        "native_symbol": "BNB",
        "native_decimals": 18,
        "explorer": "https://bscscan.com",
        "rpc": None,
        "usdt_address": "0x55d398326f99059fF775485246999027B3197955",
        # 18, not 6: this is a common bridged BSC representation, not the
        # Ethereum contract. Reading 6 here is the classic USDT bug.
        "usdt_expected_decimals": 18,
        "note": "Bridged USDT (BEP-20). Note 18 decimals. Verify the contract you intend to use.",
    },
    {
        "chain_id": 137,
        "hex": "0x89",
        "name": "Polygon PoS",
        "native_symbol": "POL",
        "native_decimals": 18,
        "explorer": "https://polygonscan.com",
        "rpc": None,
        "usdt_address": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        "usdt_expected_decimals": 6,
        "note": "USDT0 at the original USD₮ address (upgraded in place).",
    },
    {
        "chain_id": 43114,
        "hex": "0xa86a",
        "name": "Avalanche C-Chain",
        "native_symbol": "AVAX",
        "native_decimals": 18,
        "explorer": "https://snowtrace.io",
        "rpc": None,
        "usdt_address": "0x9702230A8Ea53601f5Cd2dc00fDbc13d4dF4A8c7",
        "usdt_expected_decimals": 6,
        "note": "Canonical Tether-published USD₮ (ERC-20).",
    },
    {
        "chain_id": 42161,
        "hex": "0xa4b1",
        "name": "Arbitrum One",
        "native_symbol": "ETH",
        "native_decimals": 18,
        "explorer": "https://arbiscan.io",
        "rpc": None,
        "usdt_address": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        "usdt_expected_decimals": 6,
        "note": "USDT0 at the original USD₮ address (upgraded in place).",
    },
)

_BY_ID = {c["chain_id"]: c for c in CHAINS}
_BY_HEX = {c["hex"].lower(): c for c in CHAINS}


def get_chain(chain_id):
    """Look up a chain by numeric id or hex id. None when unknown.

    Hex is compared case-insensitively: MetaMask reports chainId as
    lowercase '0x1', and a lookup that missed on case alone would show a
    user on Ethereum as being on an unrecognised network.
    """
    if chain_id is None:
        return None
    if isinstance(chain_id, str):
        text = chain_id.strip().lower()
        if text in _BY_HEX:
            return _BY_HEX[text]
        if text.startswith("0x"):
            try:
                return _BY_ID.get(int(text, 16))
            except (TypeError, ValueError):
                return None
        # A bare numeric string is read as decimal, not hex. MetaMask
        # reports chainId as hex, so "137" arriving here means a caller
        # formatting a chain id for display -- and reading it as hex
        # would silently resolve it to chain 311, or to nothing at all.
        try:
            return _BY_ID.get(int(text, 10))
        except (TypeError, ValueError):
            try:
                return _BY_ID.get(int(text, 16))
            except (TypeError, ValueError):
                return None
    try:
        return _BY_ID.get(int(chain_id))
    except (TypeError, ValueError):
        return None


def usdt_address_for(chain_id):
    chain = get_chain(chain_id)
    return chain["usdt_address"] if chain else None


def list_chains():
    """Public payload for the browser. Contains no secrets by construction.

    `rpc` is null for every supported chain: reads go through the user's
    own wallet provider, so this server never needs an RPC endpoint (and
    never becomes a relay for someone else's traffic).
    """
    out = []
    for c in CHAINS:
        row = dict(c)
        row["token_symbol"] = USDT
        out.append(row)
    return out
