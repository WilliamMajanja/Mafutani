# Security Policy

## Supported Status

Mafutani is **experimental software**. Components including the CommCoin/MFT
chain, hash-based PQC wallet/signature implementation, PiNet/sync flows,
compositor node model, trading/risk/live-order paths, and payment/off-ramp
integrations are **not externally audited** and may contain security defects.

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
  Signature state reuse or stale-backup restore can cause catastrophic loss.
- **Admin/off-ramp controls:** M-Pesa/Daraja actions, funding/admin approvals,
  KYC review, chain import/sync controls. Require admin+CSRF+explicit confirm
  where implemented; operators should add step-up 2FA, dual control, allowlists,
  velocity limits, and reconciliation.
- **Chain/consensus/PQC/proofs:** experimental, unaudited; validation bugs may
  affect integrity or funds.
- **PiNet sync/import:** import validates and appends at tip in the current model;
  do not treat peer data as trusted. Harden for adversarial networks before
  production use.
- **Secrets:** never commit `.env`, credentials, Daraja secrets, or wallet seed
  files (`chain/mft.db`, `data/trading.db`, `data/wallets/*seed*.json` are ignored
  by `.gitignore` patterns where applicable).

## Disclosure Guidance

- Do not fabricate exploit claims or incident details.
- Do not publish wallet seeds, private keys, Daraja credentials, or user data.
- Coordinate disclosure timing with maintainers when feasible.
- Operators are responsible for incident response, user notification, and legal
  obligations in their jurisdictions.
