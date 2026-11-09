# Mafutani Policies & Security Stance

**Scope:** Mafutani platform, APIs, web UI, terminal, CommCoin/MFT chain, PiNet/node sync, compositor desktop, wallet/mining, M-Pesa/Daraja off-ramp, zero-dependency KYC workflow hooks, and any record/anchor features.

> Operational safeguards and disclosures — **not legal advice**. Operators must obtain qualified legal, tax, and compliance advice for their jurisdictions.

## 1. Risk & No-Advice Disclaimer

> **No system safety, value, liquidity, settlement, consensus correctness, or compliance is guaranteed by this software.** Technical controls reduce preventable risk; they do not create audits, legal compliance, market value, liquidity, or fitness for any industry or discipline.

- **Not hardened or certified for all industries and disciplines.** No code, test, document, or control in this repository certifies suitability for finance, payments, land/title/registry, healthcare, government, safety-critical, critical infrastructure, or any regulated use.
- **Not financial, investment, trading, legal, tax, or real-estate/title advice.** Outputs are informational/experimental.
- **Paper trading is default** for trading features. Live trading is gated/audited; real loss is possible.
- **Digital assets / CommCoin (MFT) carry extreme risk.** Total loss possible. Transfers may be irreversible. Bugs, consensus/PQC failures, key loss, stale PQC state reuse, network/payment failures, fraud, forks/stalls, and regulatory action can cause loss.
- **No guarantee of security, consensus, value, liquidity, convertibility, or stability.** The configured CommCoin/MFT reference value (default **$100,000 USD**, `MAFUTANI_COIN_USD_VALUE`) is **reference/config metadata only** — not a market price, quote, guarantee, offer, or payout promise.
- **Design goals are not guarantees.** Deterministic epoch seeding/expiry replacement, SBC/PiNet capture, validation controls, and “no unnecessary token loss / no nonsense hacks” are design intents and technical controls — **not** proof of security, stability, consensus correctness, or economic outcomes.
- **CPIP/external reference tracking is report-only.** No safe auto-update or auto-apply from CPIP or other external sources. Auto-apply is intentionally excluded. External code/config must never be fetched into the runtime automatically; manual security review is required.
- **No replacement for audits.** Production reliance requires independent crypto/consensus/security audits, formal/threat modeling, adversarial/fuzz/fork-stall testing, vetted standardized PQC migration (e.g., SLH-DSA/ML-DSA or audited implementations) with versioned test vectors, and operator compliance/custody controls.

## 2. CommCoin / MFT / PiNet Policy

- Chain/node/compositor/wallet/miner/PiNet code is **experimental and not externally audited**.
- Consensus is deterministic by code (`chain/consensus.py`, `chain/ledger.py`): 182-day epochs, **100,000,000,000 MFT** epoch seed, token expiry/replacement as implemented. Changes require explicit, versioned migration/review.
- **PQC (hash-based):** `MFT-PQC-WOTS-MERKLE-SHAKE256-v1` — **not externally audited**, **stateful**. Never reuse signature indexes; never restore stale wallet backups/state.
- **PiNet sync/import:** block export/import/pull use stdlib JSON/HTTP and validated tip-append import in the current model. Treat peer data as untrusted; harden for adversarial/public P2P use.

## 3. M-Pesa / Daraja, Off-ramps, and Zero-dep KYC

- **M-Pesa/Daraja** is operator-configured (third-party). User/admin off-ramp flows require auth/CSRF/audit and explicit `confirm:true` as implemented. **Acceptance is not settlement.** Callbacks/reconciliation, idempotency, float/limits, recipient validity, and compliance are required.
- **Secrets:** store Daraja credentials in secret stores/env; never in source control, logs, or client responses. `SecurityCredential` handling is operator-controlled.
- **Zero-dependency KYC hooks** (`minting/kyc.py`, `/api/kyc/*`) provide workflow status, hashed document references, admin review, and optional off-ramp gating. **Not** a substitute for legal/regulatory compliance, qualified identity verification, sanctions/AML screening, or professional advice.

## 3a. Land / Real-Estate / Asset-Record Transactions (experimental; NOT a registry)

> **This blockchain does NOT ensure legal validity, title, ownership, enforceability, registry acceptance, or fitness for land/real-estate transactions of any size. NOT legal title.** Technical record anchoring is not a substitute for authoritative land registries, conveyancing, identity/authority verification, surveys, liens/encumbrance checks, escrow/settlement, dispute resolution, or jurisdiction-specific law.

- **Technical capability only, where implemented:** the chain may carry bounded transaction metadata and/or hash/reference anchors for off-chain records. This is **record-anchoring plumbing**, not proof of ownership or legal transfer.
- **“Any size” is technically bounded and context-dependent:** value transfers are limited by available unexpired spendable UTXOs, consensus/transaction/block bounds, wallet PQC signature capacity, and chain rules. Data/records are bounded for safety/DoS resistance; large records should be represented by cryptographic hashes/anchors of off-chain content, not unbounded on-chain payloads.
- **Token expiry warning:** current consensus rules may expire outputs after the configured token lifetime (e.g., next epoch in the current MFT model). Durable land/title semantics require explicit, reviewed consensus/design changes, migration, and legal/registry integration — **not** assumed from the current chain behavior.
- **Operator/legal requirements:** any real-world land use requires qualified legal advice, authoritative registry integration/processes, KYC/AML/sanctions/tax compliance as applicable, identity and signing authority controls, secure custody, and independent audits. Do not deploy for real property transfer without those controls.

## 4. Remediations, Controls & Residual Risk (honest stance)

These are **technical mitigations**, not guarantees. They reduce preventable errors but **cannot eliminate** unaudited/experimental risk, value/stability outcomes, legal/compliance obligations, third-party settlement, or user/operator custody mistakes. **No immunity claimed; no fabricated incident details.**

### Technical remediations implemented (verify in code/config)
- **Explicit confirmation (`confirm:true`)** for high-impact/irreversible actions where implemented: CommCoin send (`/api/chain/send`), chain import (`/api/chain/import`, admin), PiNet sync pull (`/api/node/sync/pull`, admin), M-Pesa off-ramp create, admin off-ramp submit, and other gated flows as documented.
- **Stateful PQC wallet safety (`chain/wallet.py`):** checks signature index capacity before signing, signs inputs then performs atomic state save (`tmp`+`fsync`+`replace`, restrictive mode), and **fails safe** if state cannot be persisted (raises a critical error instructing not to broadcast). **Cannot prevent** stale backup restore, copied wallet files, or external misuse.
- **Reference-only / no-guarantee labeling:** `MAFUTANI_COIN_USD_VALUE` (default $100,000/coin) and MFT market/reference quotes are labeled metadata/reference only across chain/node/off-ramp APIs and docs — not market price, quote, guarantee, offer, liquidity, or stability.
- **Settlement honesty:** off-ramp responses/docs state **Daraja/request acceptance is not settlement**; reconciliation, callbacks/idempotency, valid credentials/limits, and operator compliance are required.
- **Access controls:** session auth, CSRF on mutating flows, admin gates for sensitive operations, `HttpOnly`/`SameSite` cookies (and `Secure` where applicable), 2FA capability (`/api/auth/2fa*`).
- **Audit logging:** key transitions/actions recorded where implemented.
- **PiNet/sync validation:** import/pull validates blocks and uses a pragmatic tip-append model; peers treated as untrusted (further adversarial hardening required for public deployment).
- **Zero-dep KYC workflow hooks:** status/hashed references/admin review/audit and optional off-ramp gate (`MAFUTANI_KYC_REQUIRED_FOR_OFFRAMP`) — **technical workflow only, not legal/regulatory compliance**.
- **Stdlib-only runtime:** reduces third-party dependency surface (not a substitute for review/hardening). Enforced by `tests/test_zero_dependency.py`.

### Residual risks (cannot be fully remediated by code; operator/user responsibility)
- **Experimental/unaudited; not hardened for all industries/disciplines:** chain, PQC, Merkle, PiNet, compositor, trading live paths, off-ramp/KYC integrations, and any record/anchor features may contain defects causing loss or incorrect behavior. No certification for finance, land/title, healthcare, government, safety-critical, or regulated use.
- **Irreversibility / total loss:** blockchain transfers may be irreversible; key loss, stale PQC state reuse, consensus bugs, forks/stalls, or operational errors can cause total loss.
- **No value/stability/consensus guarantees:** reference metadata and deterministic protocol rules do not guarantee market value, liquidity, convertibility, stability, adoption, or consensus/security correctness.
- **Custody/state misuse:** users/operators can restore stale PQC backups, copy state, leak secrets, or misconfigure hosts. Use secure custody practices; never commit seeds/credentials.
- **Third-party settlement & compliance:** Daraja/M-Pesa and other services are operator-configured third parties. **Acceptance ≠ settlement.** Operators own callbacks/reconciliation, credentials, fraud controls, KYC/AML/sanctions/tax/compliance, and lawful deployment.
- **Insufficient dual control:** admin+CSRF+confirm ≠ enforced maker-checker. Add separation of duties, step-up 2FA, allowlists, velocity limits, delays/cooling-off, out-of-band confirmation, and reconciliation.
- **Deployment hardening:** TLS, reverse proxy/WAF, OS/runtime hardening, secret stores, logging/observability (no secrets), backups, and incident playbooks are required.
- **Land/asset records:** on-chain anchors/metadata do not create legal title, ownership, or registry effect. Current token expiry/UTXO semantics may be unsuitable for durable title. Requires authoritative registries, legal process, and independent controls.
- **External updates/CPIP:** report-only only. No safe auto-update/auto-apply; manual security review required. Unreviewed external changes can introduce compromise.
- **Audits/PQC migration required for production reliance:** independent crypto/consensus/security audits, formal/threat modeling, adversarial/fuzz/fork-stall testing, vetted standardized PQC migration (e.g., SLH-DSA/ML-DSA or audited implementations) with versioned test vectors, and operator compliance/custody controls are required before relying on this system in production.

## 5. Data, Locale, User Protection

- i18n is supported (`core/i18n.py`, locale files, `/api/i18n*`); legal/compliance notices must be localized and reviewed per jurisdiction.
- Collect minimum necessary data. Protect logs from secrets and sensitive personal data. Follow applicable privacy/data-protection laws.

## 6. Change Control

- Consensus, monetary policy, PQC/key custody, payout/off-ramp, KYC/admin controls, and live-trading gates require review, tests, migration notes, and updated docs.
- Core app remains **stdlib-only**; dependency additions require explicit reviewed decisions and updated enforcement.
