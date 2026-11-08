# RFF-001: Off-ramp and Funding Workflow (Superseded)

> **Current status (2026-11):** **Minima integration has been removed.** The active implementation provides **CommCoin/MFT (PiNet)** plus an operator-configured **M-Pesa/Daraja off-ramp** (`minting/mpesa_offramp.py`, `minting/daraja.py`) and **zero-dependency KYC workflow hooks** (`minting/kyc.py`).  
> Historical Minima funding-ledger/off-ramp content below is preserved for context only; do **not** treat it as current runtime behavior.

## Current Implementation (summary)

- **User off-ramp:** `GET/POST /api/offramp/mpesa`, `GET /api/offramp/mpesa/info` — auth/CSRF/audit, explicit `confirm:true`; operator-configured reference conversion (`MAFUTANI_MFT_KES_RATE`), limits/treasury instructions; optional auto-submit (explicit, **not settlement**).
- **Admin off-ramp:** `GET /api/admin/offramp/mpesa`, `POST /api/admin/offramp/mpesa/<id>/{submit|reject|complete|pending|failed}` — admin+CSRF; submit requires `confirm:true`.
- **KYC hooks (zero-dep):** `GET/POST /api/kyc/me`, `GET /api/admin/kyc`, `POST /api/admin/kyc/<user_id>/review`; optional gate `MAFUTANI_KYC_REQUIRED_FOR_OFFRAMP=1`. **Not legal/compliance verification.**
- **Settlement/compliance:** Daraja acceptance ≠ settlement. Operators own callbacks/reconciliation, credentials, idempotency, and KYC/AML/sanctions/tax/compliance.
- **Privileged controls:** admin+CSRF+confirm/audit reduce risk but are **not** universal maker-checker/dual control. Add step-up 2FA, separation of duties, allowlists/velocity limits/delays, and reconciliation for high-value flows.

## Historical (Minima, removed)

Earlier drafts/implementations described a Minima funding ledger and node-backed withdrawal lifecycle (`minting/minima.py`, `service.py`, `watcher.py`, Minima env vars, `docs/minima-node-findings.md`). Those modules and findings are **no longer part of the active integration**. Keep this section only as historical design context.

## Evidence / References

- `api/routes.py` (authoritative routes/auth/CSRF/confirm behavior)
- `minting/mpesa_offramp.py`, `minting/daraja.py`, `minting/kyc.py`
- `tests/test_minting.py`, `tests/test_zero_dependency.py`
- [docs/POLICIES.md](../docs/POLICIES.md), [docs/MAFUTANI_CHAIN.md](../docs/MAFUTANI_CHAIN.md)
