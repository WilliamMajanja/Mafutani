# Mafutani RFC/RFF Summary (current stance)

> **Current (2026-11):** Mafutani is **stdlib-only** and centered on **CommCoin/MFT (PiNet, SBC block capture)** plus an operator-configured **M-Pesa/Daraja off-ramp** and **zero-dependency KYC workflow hooks**. **Minima and Binance integrations are removed** from the active runtime.  
> Chain/PQC/PiNet/compositor/off-ramp/KYC/record anchors are **experimental and unaudited**. **Not hardened/certified for all industries or disciplines.** No guaranteed security, consensus correctness, value, liquidity, settlement, or compliance. `$100,000/CommCoin` is **reference/config metadata only** (not a market price/guarantee). Daraja acceptance ≠ settlement; KYC hooks ≠ legal compliance. Land/asset anchors are **not legal title/registry/ownership proof**; technical size is bounded and token expiry may be unsuitable for durable title. **CPIP/external refs: report-only; no safe auto-update/auto-apply.** Production reliance requires audits, formal/threat modeling, adversarial/fuzz/fork-stall testing, vetted standardized PQC migration with versioned test vectors, and operator controls.

## RFCs

1. **RFC-001: Funding/Off-ramp + KYC hooks (superseded Minima framing)** — current implementation provides CommCoin → M-Pesa (Daraja) off-ramp and zero-dep KYC workflow hooks. Historical Minima-specific design is preserved in the RFC as context only. See [`rfcs/RFC-001-minting-offramp.md`](rfcs/RFC-001-minting-offramp.md).
2. **RFC-002: GitHub Pages Hosting for Release + Package Docs** — static docs under `docs/`; review workflow/config before treating release automation as production-complete. Note: pushing workflow files may require a token with `workflow` scope. See [`rfcs/RFC-002-github-pages-release.md`](rfcs/RFC-002-github-pages-release.md).

## RFFs

1. **RFF-001: Off-ramp and Funding Workflow (superseded)** — active: Daraja/M-Pesa off-ramp + zero-dep KYC hooks (admin/user controls, CSRF/audit/confirm as implemented). Minima modules removed. See [`rffs/RFF-001-minting-service.md`](rffs/RFF-001-minting-service.md).
2. **RFF-002: OpenTerminal Refresh (Zero-Dep)** — vanilla JS terminal + stdlib backend; zero-dependency maintained. See [`rffs/RFF-002-openterminal-refresh.md`](rffs/RFF-002-openterminal-refresh.md).

## Implementation (high level)

- **Trading/platform:** stdlib app/API/UI, paper-first trading, risk controls, admin/auth/audit, terminal.
- **CommCoin/MFT:** `chain/` (consensus, SHAKE256 PoW, PQC WOTS+/Merkle, Merkle, ledger, wallet, miner), `node/` (mempool, proofs, sync/export/import, PiNet), `compositor/` (Wayland-like in-process model), i18n/locales, web `#/chain` + `#/desktop`.
- **Off-ramp/KYC:** `minting/daraja.py`, `minting/mpesa_offramp.py`, `minting/kyc.py` (zero-dep workflow hooks).
- **APIs (added/expanded):** `/api/chain/*`, `/api/chain/export|import|headers|proof/verify`, `/api/node/status|pinet|mempool`, `/api/node/pinet/capture`, `/api/node/tx/submit`, `/api/node/sync/pull`, `/api/compositor*`, `/api/offramp/mpesa*`, `/api/admin/offramp/mpesa*`, `/api/kyc/*`, `/api/admin/kyc*`, `/api/admin/daraja/*`, `/api/i18n*`, `/api/policies`.
- **Docs:** [README.md](README.md), [docs/](docs/), [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md), [CHANGELOG.md](CHANGELOG.md).

## Zero-Dependency & Security

- **Stdlib-only runtime** enforced by `tests/test_zero_dependency.py` (covers `chain,node,compositor,minting` and app tree).
- **No guarantees:** experimental/unaudited; reference pricing metadata only; admin controls reduce but do not eliminate privileged/custody/settlement/compliance risks. See [docs/POLICIES.md](docs/POLICIES.md).