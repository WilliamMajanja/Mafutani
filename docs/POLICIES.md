# Mafutani Policies & Security Stance

**Scope:** Mafutani platform, APIs, web UI, terminal, CommCoin/MFT chain, PiNet/node sync, compositor desktop, wallet/mining, M-Pesa/Daraja off-ramp, and zero-dependency KYC workflow hooks.

> Operational safeguards and disclosures — **not legal advice**. Operators must obtain qualified legal, tax, and compliance advice for their jurisdictions.

## 1. Risk & No-Advice Disclaimer

- **Not financial, investment, trading, legal, or tax advice.** Outputs are informational/experimental.
- **Paper trading is default** for trading features. Live trading is gated/audited; real loss is possible.
- **Digital assets / CommCoin (MFT) carry extreme risk.** Total loss possible. Transfers may be irreversible. Bugs, consensus/PQC failures, key loss, stale PQC state reuse, network/payment failures, fraud, and regulatory action can cause loss.
- **No guarantee of value, liquidity, convertibility, or stability.** The configured CommCoin/MFT reference value (default **$100,000 USD**, `MAFUTANI_COIN_USD_VALUE`) is **reference/config metadata only** — not a market price, quote, guarantee, offer, or payout promise.
- **Design goals are not guarantees.** Deterministic epoch seeding/expiry replacement, SBC/PiNet capture, validation controls, and “no unnecessary token loss / no nonsense hacks” are design intents and technical controls — **not** proof of security, stability, or economic outcomes.

## 2. CommCoin / MFT / PiNet Policy

- Chain/node/compositor/wallet/miner/PiNet code is **experimental and not externally audited**.
- Consensus is deterministic by code (`chain/consensus.py`, `chain/ledger.py`): 182-day epochs, **100,000,000,000 MFT** epoch seed, token expiry/replacement as implemented. Changes require explicit, versioned migration/review.
- **PQC (hash-based):** `MFT-PQC-WOTS-MERKLE-SHAKE256-v1` — **not externally audited**, **stateful**. Never reuse signature indexes; never restore stale wallet backups/state.
- **PiNet sync/import:** block export/import/pull use stdlib JSON/HTTP and validated tip-append import in the current model. Treat peer data as untrusted; harden for adversarial/public P2P use.

## 3. M-Pesa / Daraja, Off-ramps, and Zero-dep KYC

- **M-Pesa/Daraja** is operator-configured (third-party). User/admin off-ramp flows require auth/CSRF/audit and explicit `confirm:true` as implemented. **Acceptance is not settlement.** Callbacks/reconciliation, idempotency, float/limits, recipient validity, and compliance are required.
- **Secrets:** store Daraja credentials in secret stores/env; never in source control, logs, or client responses. `SecurityCredential` handling is operator-controlled.
- **Zero-dependency KYC hooks** (`minting/kyc.py`, `/api/kyc/*`) provide workflow status, hashed document references, admin review, and optional off-ramp gating. **Not** a substitute for legal/regulatory compliance, qualified identity verification, sanctions/AML screening, or professional advice.

## 4. Security Controls & Privileged-Attack Resistance (honest stance)

Layered controls aim to reduce common web/admin/operational risks, including privileged/key-compromise-style vectors seen in the crypto industry. **No immunity claimed; no fabricated incident details.**

### Controls present (verify in code/config)
- Session auth, CSRF on mutating flows, `HttpOnly`/`SameSite` cookies (and `Secure` where applicable).
- Admin-gated sensitive operations (Daraja/off-ramp admin actions, KYC review, chain import/sync pull, capture/admin chain actions as implemented).
- Audit logging for key transitions.
- 2FA capability exists (`/api/auth/2fa*`) — operators must enforce step-up for sensitive actions by deployment policy.
- Explicit `confirm:true` for payouts/off-ramp/KYC submit paths where implemented.
- Restrictive permissions for server-managed wallet files where implemented; stateful PQC warnings.
- Stdlib-only runtime reduces third-party dependency surface (not a substitute for review/hardening).

### Residual risks (operator must mitigate)
- **Insufficient dual control:** admin+CSRF+confirm ≠ enforced maker-checker. Add separation of duties, step-up 2FA, allowlists, velocity limits, delays/cooling-off, out-of-band confirmation, and reconciliation.
- **Custody/secret risk:** use secret stores/HSM-backed processes where appropriate; protect backups without breaking PQC state safety.
- **2FA not universally enforced as step-up:** verify and enforce.
- **Experimental/unaudited components:** chain, PQC, Merkle, PiNet, compositor, off-ramp/KYC integrations.
- **Third-party/network risk:** Daraja, peers, feeds, infrastructure. Monitor, reconcile, handle timeouts/idempotency.
- **Deployment hardening:** TLS, reverse proxy/WAF, OS/runtime hardening, logging/observability (no secrets), backups, incident playbooks.

## 5. Data, Locale, User Protection

- i18n is supported (`core/i18n.py`, locale files, `/api/i18n*`); legal/compliance notices must be localized and reviewed per jurisdiction.
- Collect minimum necessary data. Protect logs from secrets and sensitive personal data. Follow applicable privacy/data-protection laws.

## 6. Change Control

- Consensus, monetary policy, PQC/key custody, payout/off-ramp, KYC/admin controls, and live-trading gates require review, tests, migration notes, and updated docs.
- Core app remains **stdlib-only**; dependency additions require explicit reviewed decisions and updated enforcement.
