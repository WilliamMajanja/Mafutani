# Mafutani RFC/RFF Summary (current stance)

> **Update (2026-11-08):** Mafutani has expanded beyond the original minting/funding and docs/release scope to include **experimental Mafutani Chain / CommCoin (MFT)**, hash-based PQC, SBC-mineable PoW, 182-day epoch seeding (**100B MFT**), deterministic token expiry/replacement, Merkle abstractions/proofs, node services, a **Wayland-like node compositor desktop** (stdlib model + web renderer, not libwayland/DRM/KMS), locale/i18n, and **Daraja/M-Pesa** admin-guarded off-ramp integration. All remain **stdlib-only** (enforced by `tests/test_zero_dependency.py`). `$100,000`/CommCoin is **reference/config metadata only**.

## RFCs
1. **RFC-001: Funding ledger and Minima off-ramp (zero-dependency)** — implemented in substance as a funding ledger + withdrawal lifecycle (not a mainnet minter). Preserves the distinction between internal ledger liability and node-backed chain movement.
   - Addendum (current stance): `minting/` also includes Daraja/M-Pesa B2C bridge support (admin+CSRF+audit+`confirm`) and bridge metadata. Minima funding is separate from **Mafutani Chain (MFT/CommCoin)**, which is an independent chain implementation in `chain/`.
2. **RFC-002: GitHub Pages Hosting for Release + Package Docs** — docs/workflows guidance; static docs live under `docs/`. Review workflow files/config before treating release automation as production-complete.

## RFFs
1. **RFF-001: Funding ledger and Minima off-ramp** — implemented (admin/CSRF/audit, integer/text amount handling, idempotent credits, request→approve→execute lifecycle, reservation semantics, node refusal/failure handling as designed). Operational blocker noted historically: funded/reachable Minima node required for real execution.
   - Addendum: privileged controls reduce risk but do **not** provide universal maker-checker/dual control by default; operators must add separation of duties, step-up 2FA/allowlists/velocity/delays/reconciliation for high-value flows. Daraja payouts carry separate settlement/compliance responsibilities.
2. **RFF-002: OpenTerminal Refresh (Zero-Dep)** — vanilla JS terminal + stdlib backend, zero-dependency maintained (verify against current implementation/tests).

## Implementation (high level)
- **Trading/platform:** stdlib app/API/UI, paper-first trading, risk controls, admin/auth/audit, terminal.
- **Funding/bridges:** `minting/` (Minima transports/ledger-facing code split; Daraja client/bridge where configured). Admin funding routes; Daraja admin routes `GET/POST /api/admin/daraja/*`.
- **Mafutani Chain:** `chain/` (consensus, PQC, Merkle, ledger, wallet, miner), `node/` (mempool/proofs), `compositor/` (Wayland-like model/service), `core/i18n.py`, locale catalogs, web `#/chain` + `#/desktop` + locale/policies UI.
- **APIs added/expanded:** `/api/chain/*`, `/api/chain/proof/tx`, `/api/chain/commitment`, `/api/node/status`, `/api/compositor*`, `/api/i18n*`, `/api/policies`, `/api/admin/daraja/*` (in addition to existing trading/funding/auth/admin routes).
- **Docs:** `README.md`, `docs/index.md`, `docs/POLICIES.md`, `docs/MAFUTANI_CHAIN.md`, RFC/RFF copies updated to reflect current stance.

## Zero-Dependency
- New/expanded code remains stdlib-only. `tests/test_zero_dependency.py` covers the application tree (including `chain,node,compositor,minting`).
- Do not introduce runtime dependencies without an explicit reviewed decision and updated enforcement.

## Security & risk posture (summary)
- Controls: admin+CSRF, auth/session cookie protections, 2FA capability, audit logging, funding approval-before-execute, Daraja `confirm`+admin, rate/risk/fingerprinting in relevant paths, restrictive wallet file perms.
- Residual risks: single-admin/privileged compromise paths, server-side custody/secret handling, PQC stateful backup risk, unaudited experimental chain/PQC, third-party/payment settlement/reconciliation, and production hardening needs (HSM/secret stores, dual control/step-up, allowlists/velocity/delays, monitoring). See `docs/POLICIES.md`.
