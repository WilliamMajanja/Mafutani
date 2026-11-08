# Mafutani Policies

**Effective scope:** applies to the Mafutani platform, APIs, web UI, terminal, Mafutani Chain (MFT/CommCoin), node/compositor desktop, wallet/mining features, Minima funding ledger, and integrated payment/off-ramp services (including M-Pesa via Daraja when configured).

> These policies are operational safeguards and disclosures. They are **not legal advice**. Operators deploying Mafutani in a jurisdiction must obtain qualified legal, tax, and compliance advice.

## 1. Risk & No-Advice Disclaimer
- **Not financial, investment, trading, legal, or tax advice.** Analytics, signals, backtests, arbitrage views, chain data, prices, proofs, and outputs are informational/experimental.
- **Paper trading is the default** for trading features. Live trading is gated/audited and requires explicit operator enablement/controls; a live order can result in real loss.
- **Digital assets / MFT carry extreme risk.** You may lose all value. Transfers may be irreversible. Software bugs, consensus failures, key loss, stale PQC state reuse, network issues, exchange/payment failures, fraud, and regulatory action can cause total loss.
- **No guarantee of value, liquidity, tradability, or convertibility.** The configured CommCoin/MFT reference value (default **$100,000 USD** per coin, `MAFUTANI_COIN_USD_VALUE`) is **reference/config metadata only** — not a market price, quote, guarantee, offer, or payout promise.

## 2. Mafutani Chain / MFT / CommCoin Policy
- Chain/node/compositor/wallet/miner code is **experimental and not externally audited**. Use at your own risk.
- Consensus is deterministic by code (see `chain/consensus.py`, `chain/ledger.py`): 182-day epochs, **100,000,000,000 MFT** epoch seed (one deterministic seed tx per epoch window/rules), and token expiry/replacement rules as implemented. Monetary/consensus changes require explicit, versioned migration and review.
- **Post-quantum signatures are hash-based** (`MFT-PQC-WOTS-MERKLE-SHAKE256-v1`, WOTS+/Merkle over SHAKE256). They are a real hash-based PQC approach but **not externally audited**. Wallet state is **stateful**: never reuse signature indexes and never restore old wallet backups/state.
- **Merkle abstractions/proofs** (tx/block/UTXO/commitments) are implementation features for integrity/proof workflows; they do not eliminate consensus, implementation, or operational risk.
- Mining is permissionless in protocol design but operationally constrained by resources/network; no profitability is implied.

## 3. Payments, Minima Funding, M-Pesa / Daraja, and Off-Ramps
- **Minima funding ledger** is an internal auditable liability/withdrawal workflow, not a mainnet minter. Withdrawals follow explicit lifecycle controls (request/reserve, approval, node-backed execution paths as implemented) and require a funded/reachable node where applicable.
- **M-Pesa/Daraja** is an admin-guarded payout bridge when configured by the operator with valid credentials/certificates and compliant processes. It uses stdlib networking and requires explicit confirmation for payout actions.
- **Daraja credentials/secrets** must be stored in environment/secret stores, never in source control, logs, or client responses. `SecurityCredential` is accepted as an operator pre-encrypted value in the zero-dependency design — operators remain responsible for correct encryption/material handling.
- **Fiat payout success is not guaranteed** by chain/ledger state. Payout acceptance is not settlement; callbacks/reconciliation, sufficient float/limits, valid recipient details, idempotency, and compliance checks are required.
- Operators are responsible for KYC/AML/sanctions screening, tax handling, consumer disclosures, dispute processes, and compliance with Safaricom, CBK, communications/data laws, and all applicable jurisdictions.

## 4. Security Controls & Privileged-Attack Resistance (current stance)

Mafutani implements layered controls intended to reduce common web/admin/operational risks, including **privileged/key-compromise-style attack vectors seen in the crypto industry** (including high-profile exchange incidents). This section describes controls **as implemented/design intent** and residual gaps honestly; it does **not** claim immunity and does **not** fabricate incident-specific details.

### Controls present (verify in code/config before relying on them)
- **Authentication & session controls:** session-based auth; CSRF protection on state-changing flows; session cookies use `HttpOnly; SameSite=Lax` and add `Secure` when served appropriately (see `_session_cookie_flags` / auth helpers).
- **Authorization:** admin-gated sensitive operations (including funding admin actions and Daraja status/withdraw routes); audit logging for important transitions/actions.
- **2FA capability:** 2FA endpoints exist (`/api/auth/2fa*`) — operators/users must enable and enforce step-up use according to deployment policy.
- **Funding withdrawal lifecycle (Minima):** request records/reserves liability, approval is a separate explicit step, and execution is node-backed; reservation/failure semantics are designed to avoid silent fund movement and to fail loudly on node/refusal conditions.
- **Daraja payouts:** admin + CSRF + audit + explicit `confirm:true` required for payout actions; configuration/status endpoints avoid exposing secrets.
- **Trading/risk controls:** paper/live separation, risk/rate-limit/fingerprinting and allowlist/limit mechanisms exist in relevant paths (review implementation for exact enforcement scope).
- **Key/file handling:** server-managed MFT wallet files are written with restrictive permissions (`chmod 0o600`); PQC state is stateful and must not be reused/restored from stale backups.
- **Zero-dependency supply chain reduction:** stdlib-only runtime reduces third-party package dependency surface (not a substitute for secure coding, review, or infrastructure hardening).

### Residual imbalances / risks (operator must mitigate)
- **Single-admin / insufficient dual control:** admin+CSRF+`confirm` is not the same as enforced maker-checker/dual authorization. High-value funding approvals and Daraja payouts may be vulnerable to a single privileged account/session compromise unless operators add independent approval, mandatory step-up 2FA, hardware-backed auth, allowlists, velocity limits, withdrawal delays/cooling-off, out-of-band confirmation, and separation of duties.
- **Server-side key/wallet custody:** server-managed wallets and payout credentials create custody risk. Production deployments should use HSMs/secret stores, encrypted key management, minimal-access hosts, backup controls that preserve PQC state safely, and incident response procedures.
- **2FA not universally enforced as step-up:** 2FA capability exists, but operators must verify and enforce step-up 2FA for admin/payout/withdrawal actions in their deployment process.
- **Compositor action model:** compositor actions are a shared service/UI model; treat as an operational surface and review authorization/session assumptions before exposing beyond trusted contexts.
- **Chain/PQC/protocol implementation risk:** MFT chain, PQC, Merkle proofs, and node components are experimental/unaudited; consensus/crypto/serialization bugs can have security or fund-loss impact.
- **Third-party/payment/network outages:** Daraja, Minima nodes, data feeds, and networks are failure-prone; reconciliation, idempotency, timeout handling, and monitoring are required.
- **Public deployment hardening:** input validation/rate limits/local assumptions are insufficient alone. Use TLS, reverse-proxy/WAF controls, hardened OS/runtime, secret rotation, observability, backups, and tested incident playbooks.

## 5. Data, Locale, and User Protection
- Global locale/i18n is supported as a capability (`core/i18n.py`, locale files, `/api/i18n*`), but legal/compliance notices must be reviewed per locale/jurisdiction.
- Collect minimum necessary personal/payment data. Protect logs from secrets and sensitive personal data. Follow applicable privacy/data-protection laws.

## 6. Change Control
- Changes to consensus, monetary policy, key custody/PQC handling, payout flow, privileged controls, or live-trading gates require code review, tests, migration notes, and updated policy/docs.
- Core application code is zero-dependency by design. New runtime dependencies must not be introduced without an explicit, reviewed decision and updated zero-dependency enforcement.
