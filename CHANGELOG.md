# Changelog

All notable changes to this repository should be documented here.

The format is inspired by [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Dates use `YYYY-MM-DD`. This project is experimental; versions/tags may be
added when release discipline is established.

## [Unreleased]

### Added (preventive controls; not guarantees/compliance)
- Off-ramp: `idempotency_key` support, velocity/cooldown/pending limits (env-configured), Daraja callback reconciliation scaffold (`POST /api/offramp/mpesa/callback`, admin callback list), maker-checker approve→submit hook (`MAFUTANI_OFFRAMP_MAKER_CHECKER`), step-up 2FA enforcement hook for sensitive admin actions (`MAFUTANI_REQUIRE_2FA_FOR_SENSITIVE`), admin readiness checklist (`GET /api/admin/readiness`).
- Wallet/PQC protocol safety (stdlib-only, experimental): sign on deep-copied key state, commit only after atomic save, best-effort append-only signing journal, fail-safe rollback in memory on save error; tx carries `pqc_safety` metadata.
- Docs: explicit CVE/CWE/implementation/state/fault-injection risk context for stateful HBS/WOTS+/XMSS components; no unsafe from-scratch PQC replacement.

### Added
- **CommCoin/MFT native integration** (stdlib-only): PiNet SBC block-capture node (`node/pinet.py`, `node/sync.py`), mempool/tx submit, chain export/import/headers, peer pull, and proof verification APIs.
- **Zero-dependency KYC workflow hooks** (`minting/kyc.py`): case status, hashed document references, admin review/audit events, and optional off-ramp gating (`MAFUTANI_KYC_REQUIRED_FOR_OFFRAMP`). Technical plumbing only — not legal/regulatory compliance.
- **CommCoin reference market symbols**: `MFTUSD`, `MFTKES` served as local/operator-configured reference quotes (`commcoin_reference`), clearly labeled — not external market liquidity/price guarantees.
- **User-facing CommCoin → M-Pesa (Daraja) off-ramp**: request APIs/UI, admin actions, CSRF/audit/explicit confirmation controls; acceptance is not settlement.
- **Merkle/proofs/compositor/node** capabilities surfaced and documented (experimental).
- Community/repo hygiene docs: `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, `LICENSE` (owner-defined status), `CHANGELOG.md`.

### Changed
- **Binance integration removed** (broker/feed/venue/scripts) and replaced with CommCoin/MFT-native/reference paths and neutral venue handling.
- **Minima integration removed**; funding stance is CommCoin/MFT + Daraja/operator-configured off-ramp controls.
- Documentation refreshed across README, policies, chain docs, RFC/RFF mirrors for current stance and honest risk/compliance posture.

### Security / Risk Notes
- CommCoin/MFT chain, PQC, PiNet sync/import, off-ramps, and admin controls are **experimental and unaudited**. No guarantees of value, stability, security, or compliance.
- `$100,000/CommCoin` is **reference/config metadata only** (`MAFUTANI_COIN_USD_VALUE`).
- Do not commit wallet seeds, `.env`, Daraja credentials, or local DBs.
