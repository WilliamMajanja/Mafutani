# Mafutani Chain (CommCoin / MFT)

Mafutani Chain is a **zero-dependency**, stdlib-only blockchain in `chain/`, with node/PiNet services in `node/`, and a Wayland-like compositor desktop in `compositor/`/`web`. It is **experimental and not externally audited**.

## Consensus & Tokenomics (normative: `chain/consensus.py`, `chain/ledger.py`)

- **Coin:** MFT (alias **CommCoin**), `1 coin = 10^8` base units. Config: `MAFUTANI_COIN_SYMBOL`, `MAFUTANI_COIN_NAME`, `MAFUTANI_COIN_ALIAS`.
- **Reference valuation (metadata only):** `MAFUTANI_COIN_USD_VALUE` (default `100000`, i.e. **$100,000 USD per coin reference value**). Config/reference display/accounting metadata — **not** a market price, quote, guarantee, offer, liquidity promise, stability guarantee, or financial advice.
- **Epoch:** `182 days` (6 months) from fixed genesis reference.
- **Epoch seeding:** each epoch may include **exactly one** deterministic `epoch_seed` transaction minting **100,000,000,000 MFT (100B)**.
- **Token expiry / replacement:** outputs expire after `TOKEN_LIFETIME_EPOCHS` (default `1`). **Expired outputs are not spendable** by consensus rules. The per-epoch seed replaces supply deterministically, avoiding unbounded stale accumulation **by protocol design** (not a guarantee of economic outcome or user safety).
- **PoW:** `SHAKE256(header||nonce)` vs adjustable target (`chain.consensus.meets_target`). SBC-friendly defaults (pure-Python mineable), difficulty adjusts by interval. Not designed to compete with ASIC networks.
- **Storage/model:** separate SQLite DB (`MAFUTANI_CHAIN_DB`, default `chain/mft.db`). UTXO model; amounts stored as integer strings.

## Cryptography (Hash-based PQC approach)

Implemented in `chain/pqc.py` (stdlib only): **`MFT-PQC-WOTS-MERKLE-SHAKE256-v1`** (WOTS+ / Winternitz `w=16`, Merkle-authenticated, SHAKE256).

- **Design intent:** hash-based signatures rely on hash preimage/collision resistance assumptions (different from RSA/ECDSA factoring/discrete-log assumptions).
- **Critical limitations (honest):** custom implementation, **not externally audited**. Wallet operation is **stateful** (`next_index`).
  - **State reuse breaks security:** **Never reuse a signature index** and **never restore an old wallet backup/state/copy**. Use one authoritative wallet state; rotate before exhaustion (`H=10` default = 1024 signatures/key).
  - **Implementation/CWE/CVE risk:** stateful hash-based signature *implementations* and protocols (XMSS/XMSS^MT/LMS/WOTS+ components) have had real-world vulnerabilities in specific products/libraries (e.g., memory-safety/input-validation bugs, and state-management failures). Fault-injection research has also targeted WOTS/OTS operations. **This codebase has not been audited** and may contain similar or different defects.
  - **Not a production recommendation:** stateful HBS signing is safest with robust state controls (often HSM-grade operational controls). Standardized stateless options (e.g., SLH-DSA/FIPS 205) or vetted audited implementations (e.g., ML-DSA/FIPS 204 or audited HBS libraries) are preferable for production migrations — but replacing crypto requires expert review, test vectors, and a versioned migration; do **not** ship a from-scratch replacement as “safe.”

## Merkle Abstractions & Proofs

`chain/merkle.py`, `node/proofs.py` (stdlib only): binary Merkle trees (SHA3-256, domain separation), tx/block/UTXO/state commitments, inclusion proofs, and proof verification. Proofs support integrity workflows — **not** a substitute for consensus validation, audits, or secure operations.

## PiNet / Node Services (SBC-friendly)

`node/` provides:
- `node/mempool.py`: staging mempool (admission not consensus authority).
- `node/proofs.py`: proof service.
- `node/sync.py`: block/header export, validated **tip-append** import, peer pull over stdlib HTTP JSON.
- `node/pinet.py`: PiNet SBC capture/status (bounded mining rounds, mempool drain with validation, safety metadata).

**Sync model note:** current import/pull is pragmatic and SBC-friendly (tip extension). Treat peer data as untrusted; harden for adversarial/public P2P before production.

## Wayland-like Node Compositor Desktop

`compositor/` implements a **Wayland-inspired in-process model** (not libwayland/DRM/KMS): display/registry/globals, compositor/surface, output, seat/pointer/keyboard abstractions, `xdg_wm_base`/toplevel semantics, commit/damage, scene graph, desktop shell. `compositor/service.py` exposes state/actions for the web renderer at `#/desktop`.

## M-Pesa / Daraja Off-ramp & Zero-dep KYC Hooks

- `minting/mpesa_offramp.py` + `minting/daraja.py`: CommCoin → M-Pesa request flow and Daraja B2C integration (stdlib `urllib`). Requires operator config, auth/CSRF/audit, explicit `confirm:true`. **Request acceptance is not settlement.**
- `minting/kyc.py`: zero-dependency KYC workflow hooks (status, hashed document references, admin review/audit, optional off-ramp gate `MAFUTANI_KYC_REQUIRED_FOR_OFFRAMP`). **Technical plumbing only** — not legal/regulatory compliance, qualified identity verification, or sanctions/AML screening.

## API Surface (current)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/chain/info` | chain status, epoch, reference metadata |
| GET | `/api/chain/blocks` | recent blocks |
| GET | `/api/chain/balance?address=...` | address balance |
| GET/POST | wallet/tx/mine/seed endpoints | see `api/routes.py` for exact paths/auth |
| GET | `/api/chain/proof/tx`, `/api/chain/commitment` | Merkle/proof data |
| POST | `/api/chain/proof/verify` | verify proof payload |
| GET | `/api/chain/export`, `/api/chain/headers` | sync/export (range-limited) |
| POST | `/api/chain/import` | admin+CSRF, validated tip-append |
| GET | `/api/node/status`, `/api/node/pinet`, `/api/node/mempool` | node/PiNet status |
| POST | `/api/node/pinet/capture` | auth+CSRF, bounded SBC capture |
| POST | `/api/node/tx/submit` | auth+CSRF, validated submit |
| POST | `/api/node/sync/pull` | admin+CSRF, peer pull |
| GET/POST | `/api/compositor`, `/api/compositor/action` | compositor desktop model |
| GET/POST | `/api/offramp/mpesa*`, admin offramp | CommCoin → M-Pesa |
| GET/POST | `/api/kyc/*`, admin KYC | zero-dep workflow hooks |

See `api/routes.py` for authoritative dispatch and auth requirements.

## Warnings

Experimental, unaudited software. Do not treat balances/proofs/reference values as guaranteed financial outcomes. Secure hosts, backups, PQC wallet state, credentials, and admin access. No guarantees of stability, value, or absence of hacks.


## Remediations & Residual Risk (honest)

| Area | Technical mitigations | Residual risk |
|---|---|---|
| Irreversible transfers | `POST /api/chain/send` requires `confirm:true`; auth/CSRF/audit | Tx may be irreversible; wrong recipient/amount can cause total loss |
| PQC wallet state | Capacity checks, atomic state save, fail-safe on save error | Unaudited; stale backup restore/copy misuse can break state safety |
| Value/reference | `MAFUTANI_COIN_USD_VALUE` labeled metadata-only in chain/node/docs | No market price, liquidity, stability, or guarantee |
| PiNet sync/import | Validated tip-append; admin+CSRF+`confirm` for mutating sync/import | Peers untrusted; adversarial/public P2P hardening needed |
| Off-ramp | Auth/CSRF/audit/confirm; KYC workflow gate option; settlement disclaimers | **Acceptance ≠ settlement**; operator owns reconciliation/compliance |
| Design goals | Deterministic rules, SBC capture, validation controls | **Do not guarantee** value, stability, security, adoption, or no bugs/hacks |
