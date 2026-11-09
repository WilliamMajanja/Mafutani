# Mafutani & CommCoin — Internal Security/Correctness Audit (Non-Certified)

**Date:** 2026-11-09  
**Scope:** Mafutani repository (stdlib-only app, API, `chain/`, `node/`, `compositor/`, `minting/`, `core/`, `web/`, `docs/`, `tests/`) with focus on **CommCoin/MFT**, PiNet/sync, wallet/PQC, auth/admin, and M-Pesa/Daraja off-ramp/KYC hooks.  
**Method:** static code/config review, targeted grep, API/flow inspection, and execution of available tests.  
**Limitation:** This is an **internal engineering review**, **not** an independent security audit, cryptographic audit, penetration test, financial audit, or legal/compliance certification. Findings may be incomplete. **No guarantees of safety, security, value, liquidity, or compliance** are made.

> **Honesty note:** “WOTS+ led to the downfall of Minima and Binance” is **not verified** here and is not asserted. Stateful hash-based signature *implementations* and state management have known risk classes and real-world CVE/CWE examples in specific products/libraries (see `docs/MAFUTANI_CHAIN.md`, `chain/pqc.py`); that does **not** imply a specific incident attribution.

## Evidence (tests executed)
```bash
python3 -m pytest tests/test_zero_dependency.py tests/test_chain_mft.py tests/test_minting.py -v  # 16 passed
python3 -m pytest tests/test_auth.py tests/test_admin.py -q                                      # 26 passed
```
Total observed in this review: **42 passed** (subset). Full test suite not exhaustively reported here.

## Summary
Mafutani has meaningful **preventive controls** (stdlib-only enforcement, auth/CSRF/admin gates, parameterized SQL in most reviewed paths, explicit `confirm:true` for high-impact actions, PQC wallet capacity/atomic-save/fail-safe + journal, off-ramp reference-only/settlement disclaimers, idempotency/velocity/maker-checker/step-up hooks, readiness/callback scaffold). **Residual risk remains high** for experimental/unaudited chain/PQC, irreversible key/tx/custody mistakes, third-party settlement/compliance, and operator configuration.

## Findings

| Severity | Area | Finding | Evidence | Status / Remediation |
|---|---|---|---|---|
| **High (inherent)** | CommCoin PQC/wallet | Custom **unaudited, stateful** `MFT-PQC-WOTS-MERKLE-SHAKE256-v1` (WOTS+/Merkle/SHAKE256). OTS index reuse, stale backup/copy restore, or implementation defects can cause catastrophic loss. Similar implementation/state/fault classes have had CVEs/CWEs in other products. | `chain/pqc.py`, `chain/wallet.py`; docs updated with CVE/CWE context | **Partially mitigated:** capacity checks, deep-copy signing, atomic save, in-memory rollback on save failure, fail-safe “do not broadcast”, signing journal, warnings. **Not eliminated.** Requires independent crypto audit, HSM-grade state controls, and/or vetted standardized migration. |
| **High (operational)** | Daraja callback scaffold | `POST /api/offramp/mpesa/callback` stores raw payloads for reconciliation; **authenticity not fully verified** (optional shared secret only). **Not settlement.** Risk of spoofed/forged callback data if endpoint is exposed without strong controls. | `api/routes.py` (`_offramp_mpesa_callback`), `minting/mpesa_offramp.py` (`record_callback`) | **Scaffold only.** Mitigate with network/TLS controls, IP allowlists/WAF, provider verification best practices, idempotent reconciliation, and manual/admin review before settlement. Never treat as verified settlement. |
| **Medium** | Admin PiNet sync pull (SSRF/control) | Admin can trigger fetch/import from `peer` URL (`/api/node/sync/pull`). Now gated by admin+CSRF+`confirm`+optional step-up 2FA; no URL scheme/host allowlist or egress restrictions observed. | `api/routes.py` (`_node_sync_pull`) | **Mitigated but residual:** restrict to trusted peers/allowlists, enforce HTTPS/schemes, egress filtering, and audit. Treat peers as untrusted; import is validated tip-append (not full adversarial consensus). |
| **Medium** | Policy-enforced controls are configurable | Maker-checker, sensitive step-up 2FA, KYC gate, velocity limits, callback secret depend on operator config; not universally enforced by default. | `api/routes.py`, `minting/mpesa_offramp.py`, `README.md` | **By design.** Use `GET /api/admin/readiness` (checklist, not certification). Enforce strict policies in production-like deployments. |
| **Medium** | Cookie `Secure` flag depends on env | Session cookie adds `Secure` only when `HTTPS=1`. Behind TLS-terminating proxy, misconfiguration can weaken transport protections. | `api/routes.py` (`_session_cookie_flags`) | Set `HTTPS=1` appropriately; prefer proxy-aware secure deployment and HSTS at edge. |
| **Low** | SQL identifier formatting in migration | `ALTER TABLE ... ADD COLUMN %s %s` uses string formatting; values come from a **hardcoded internal list** (not external input). | `minting/mpesa_offramp.py:118` area | **Low practical injection risk.** Prefer explicit constant DDL statements to avoid unsafe patterns. |
| **Low** | Static file traversal (reviewed) | Static serving normalizes path and verifies resolved path remains under `WEB_DIR`; SPA fallback to index. | `app.py` (`_serve_static`) | **Appears mitigated.** Verify edge cases/symlinks in deployment-specific tests. |
| **Informational** | CSRF/auth baseline | CSRF uses header token with `hmac.compare_digest`; mutating endpoints generally require session + CSRF; admin checks present. | `api/routes.py` (`_check_csrf`, `_require_*`) | Continue enforcing on all state-changing routes; verify exemptions are intentional. |
| **Informational** | Terminal execution | Terminal commands handled by internal DSL (`core.terminal.execute`), not shell `eval/exec/subprocess` in reviewed paths. | `api/routes.py` (`_terminal_command`), `core/terminal.py` | Keep DSL parsing strict; risk-check order placement paths. |
| **Informational** | No obvious unsafe deserialization/shell patterns found (targeted grep) | Targeted search did not show `pickle.load`, `yaml.load`, `subprocess`, `os.system`, Python `eval/exec` misuse in reviewed modules. | `api/`, `core/`, `chain/`, `node/`, `minting/`, `compositor/` | Not exhaustive; maintain stdlib-only discipline and code review. |

## CommCoin-Specific Notes
- **Consensus/ledger**: deterministic rules implemented in `chain/`; experimental/unaudited. Validate economic/security assumptions independently before production use.
- **Reference value**: `$100,000`/CommCoin (`MAFUTANI_COIN_USD_VALUE`) is **reference/config metadata only** — no market price/quote/guarantee/offer/liquidity/stability.
- **PiNet/sync**: validated tip-append import/pull is pragmatic for SBC capture; **peers untrusted**. Adversarial/public P2P requires stronger fork-choice, peer scoring, DoS/rate limits, and replay/network protections.
- **Proofs/Merkle**: integrity aids, **not** a substitute for consensus validation or audits.

## Off-ramp/KYC/Compliance Notes
- **M-Pesa/Daraja**: third-party/operator-configured. **Request/API acceptance ≠ settlement.** Operators own credentials, callbacks, reconciliation, idempotency, fraud controls, and KYC/AML/sanctions/tax/legal compliance.
- **KYC hooks**: zero-dependency workflow only (`minting/kyc.py`) — **not** legal/regulatory compliance or qualified identity/sanctions screening.

## Recommendations (prioritized)
1. **Independent crypto/security audit** of `chain/pqc.py`, `chain/wallet.py`, tx/consensus validation, and key-state operations; define migration path to vetted standardized schemes (e.g., SLH-DSA/ML-DSA) with test vectors and versioning.
2. **Harden callback & settlement**: authenticated/verified provider callbacks, reconciliation state machine, idempotency, out-of-band confirmation for payouts, and strict “not settled until reconciled” handling.
3. **Restrict high-risk admin network actions**: peer URL allowlists/schemes, egress controls, and mandatory step-up 2FA/maker-checker for production operators.
4. **Enforce deployment baseline**: TLS/HSTS, `HTTPS=1` cookie security, secret stores (no `.env`/seeds in repo), backups safe for PQC state, monitoring/log redaction, rate limits/WAF.
5. **Compliance program (operator-owned)**: legal/regulatory review per jurisdiction; KYC/AML/sanctions/tax controls beyond workflow hooks; documented incident response.
6. **Adversarial testing**: fuzz/negative tests for PQC parsing, tx/block validation, sync/import, callback payloads, and authz/CSRF boundaries.

## Final Statement
Mafutani/CommCoin contain **useful preventive controls** but remain **experimental and unaudited**. This review **reduces unknowns**; it does **not** certify safety, security, compliance, correctness, value, or liquidity.
