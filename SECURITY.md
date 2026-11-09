# Security Policy

## Supported Status

Mafutani is **experimental software**. Components including the CommCoin/MFT
chain, hash-based PQC wallet/signature implementation, PiNet/sync flows,
compositor node model, trading/risk/live-order paths, payment/off-ramp, KYC
workflow hooks, and any record/anchor features are **not externally audited**
and may contain security defects.

**Not hardened or certified for all industries and disciplines.** No guarantee
of security, consensus correctness, value, liquidity, convertibility,
settlement, or compliance. Not suitable to claim fitness for finance,
land/title/registry, healthcare, government, safety-critical, or regulated use
without independent audits, formal/threat modeling, adversarial/fuzz/fork-stall
testing, vetted standardized PQC migration (e.g., SLH-DSA/ML-DSA or audited
implementations) with versioned test vectors, and operator compliance/custody
controls.

**CPIP/external references:** report-only. **No safe auto-update or auto-apply**
from CPIP/external sources; auto-apply is intentionally excluded. Never fetch or
apply external code/config into the runtime automatically.

There is currently **no guarantee** of security updates, response times, or
supported release branches. Operators and users should deploy with appropriate
controls and assume risk.

## Reporting a Vulnerability

Please **do not open a public GitHub issue** for security vulnerabilities.

Preferred reporting path:
1. Use GitHub's **private vulnerability reporting / Security Advisory** feature
   for this repository if it is enabled.
2. If private reporting is not available, contact the repository owner/maintainer
   directly through a private channel and include `[SECURITY]` in the subject.

Include, when possible:
- affected component/file/endpoint
- impact and threat model
- reproduction steps or proof of concept
- suggested mitigation (optional)
- whether the issue affects funds, keys, auth/admin controls, payouts, or consensus

## Security-Sensitive Areas

Please treat these as high impact:

- **Keys/custody/PQC state:** MFT PQC wallets are stateful (`next_index`).
  Signature state reuse or stale-backup restore can cause catastrophic/irreversible loss.
  `chain/wallet.py` enforces signature capacity checks, atomic state saves, and a fail-safe
  if state cannot be persisted (do not broadcast). **This does not prevent** stale backup
  restore or copied wallet state misuse.
- **Admin/off-ramp controls:** M-Pesa/Daraja actions, funding/admin approvals,
  KYC review, chain import/sync controls. High-impact flows require auth/CSRF and
  explicit `confirm:true` where implemented (e.g., chain send/import, sync pull,
  off-ramp create/admin submit). **Acceptance is not settlement.** Operators must add
  step-up 2FA, dual control/maker-checker, allowlists, velocity limits, delays,
  out-of-band confirmation, callbacks/reconciliation, and compliance controls.
- **Chain/consensus/PQC/proofs:** experimental, unaudited; validation bugs may
  affect integrity or funds.
- **PiNet sync/import:** import/pull validates and uses a pragmatic tip-append model;
  peers are untrusted. Admin+CSRF+`confirm:true` required for mutating sync/import APIs
  as implemented. Harden for adversarial/public networks before production use.
- **Secrets:** never commit `.env`, credentials, Daraja secrets, or wallet seed
  files (`chain/mft.db`, `data/trading.db`, `data/wallets/*seed*.json` are ignored
  by `.gitignore` patterns where applicable).

## Disclosure Guidance

- Do not fabricate exploit claims or incident details.
- Do not publish wallet seeds, private keys, Daraja credentials, or user data.
- Coordinate disclosure timing with maintainers when feasible.
- Operators are responsible for incident response, user notification, and legal
  obligations in their jurisdictions.
