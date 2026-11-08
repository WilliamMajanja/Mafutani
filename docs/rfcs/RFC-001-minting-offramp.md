# RFC-001: Funding Ledger, Off-ramp, and KYC Workflow Hooks

> **Current status (2026-11):** **Minima integration has been removed.** Mafutani's primary crypto path is **CommCoin/MFT (stdlib-only, PiNet/SBC capture)** with an operator-configured **M-Pesa/Daraja** off-ramp and **zero-dependency KYC workflow hooks** (`minting/kyc.py`).  
> The historical Minima-specific design below is preserved for context only; do **not** treat it as current runtime behavior. See [README](../README.md), [docs/MAFUTANI_CHAIN.md](../docs/MAFUTANI_CHAIN.md), and [docs/POLICIES.md](../docs/POLICIES.md).

---

## Historical: Minima funding ledger framing (superseded)

Status: historical context; superseded by CommCoin/MFT + Daraja off-ramp + zero-dep KYC hooks.

### Decision (historical)

Do not build a minter. Build a funding ledger plus an off-ramp.

Mainnet supply is a consensus constant and `send` moves existing coins. A web
service cannot add to it, so a "mint" endpoint can only ever be a database
write wearing the wrong name.

### Shape (historical)

`minting/` was originally split for Minima chain-facing and ledger-facing code.
Current `minting/` contains Daraja/M-Pesa off-ramp and zero-dep KYC workflow
hooks; Minima transport/service/watcher modules were removed.

### Addendum (current stance)

- **CommCoin/MFT is separate** from any Minima concepts. Do not conflate balances, addresses, wallets, or consensus.
- **Daraja/M-Pesa off-ramp:** admin/user flows as implemented (`/api/offramp/mpesa*`, admin actions) require auth/CSRF/audit and explicit `confirm:true` where applicable. **Acceptance is not settlement.** Operators own callbacks/reconciliation, credentials, and KYC/AML/sanctions/tax/compliance.
- **Zero-dep KYC hooks:** technical workflow plumbing only; not legal/regulatory compliance.
- **Privileged-attack resistance:** admin+CSRF+confirm is not enforced maker-checker/dual control. Add separation of duties, step-up 2FA, allowlists/velocity limits/delays, out-of-band confirmation, secret stores, and monitoring/reconciliation. See `docs/POLICIES.md`.
- **No fabrication:** no incident-specific claims are made about third-party breaches.
