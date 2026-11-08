# Mafutani Documentation

> **Mafutani** is a zero-dependency, stdlib-only Python platform for experimental trading, **CommCoin/MFT (PiNet)**, and operator-guarded off-ramps.

## Current Stance

- **Stdlib-only runtime** (enforced by `tests/test_zero_dependency.py`). No npm build for the core app.
- **Trading:** paper default; live paths gated/audited. Not financial advice.
- **CommCoin/MFT:** experimental, unaudited chain (SHAKE256 PoW, hash-based PQC WOTS+/Merkle, 182-day epochs, **100B MFT** deterministic seed, token expiry/replacement, Merkle proofs, PiNet SBC capture/sync). `$100,000/CommCoin` is **reference/config metadata only** — not a market price or guarantee.
- **Off-ramps:** CommCoin → M-Pesa (Daraja) user/admin flows when operator-configured; **acceptance ≠ settlement**. Zero-dep KYC hooks are workflow plumbing, not legal compliance.
- **Security:** layered controls exist, but privileged-attack resistance is **not absolute**. See [Policies](POLICIES.md) and [SECURITY.md](../SECURITY.md).

## Read First

| Doc | Purpose |
|---|---|
| [../README.md](../README.md) | Quick start, config, API overview, project layout |
| [POLICIES.md](POLICIES.md) | Risk, payments/KYC, security controls & residual risks |
| [MAFUTANI_CHAIN.md](MAFUTANI_CHAIN.md) | Chain, PQC, Merkle, PiNet, compositor, off-ramp/KYC APIs |
| [../SECURITY.md](../SECURITY.md) | Vulnerability reporting & sensitive areas |
| [../CONTRIBUTING.md](../CONTRIBUTING.md) | Contribution workflow & quality checklist |
| [../CHANGELOG.md](../CHANGELOG.md) | Notable changes |

## Design Records

- [RFCs](rfcs/) and [../rfcs](../rfcs/) (mirrored copies may exist)
- [RFFs](rffs/) and [../rffs](../rffs/)
- [Research: Sub-agent trading](research-subagent-trading.md)
- [MAFUTANI_RFC_RFF_SUMMARY.md](../MAFUTANI_RFC_RFF_SUMMARY.md)

## Quick Start

```bash
python3 scripts/seed.py
python3 app.py
# SBC/local: PORT=8000 MAFUTANI_NO_FEED=1 MAFUTANI_PINET_ROUNDS=100000 python3 app.py
```

## UI Areas

Markets, Chain (`#/chain`), Desktop (`#/desktop`), Wallet, Policies (`#/policies`), plus trading/admin views depending on role/config.

## GitHub Pages

Published via GitHub Pages workflows when configured (workflow files may require a token with `workflow` scope to push). See repository Actions/settings.
