# Mafutani Chain (MFT / CommCoin)

Mafutani Chain is a **zero-dependency**, stdlib-only blockchain implementation in `chain/`, with node services in `node/` and a Wayland-like compositor desktop in `compositor/`/`web`. It is **experimental and not externally audited**.

## Consensus & Tokenomics (normative: `chain/consensus.py`, `chain/ledger.py`)
- **Coin:** MFT (alias **CommCoin**), `1 coin = 10^8` base units. Config: `MAFUTANI_COIN_SYMBOL`, `MAFUTANI_COIN_NAME`, `MAFUTANI_COIN_ALIAS`.
- **Reference valuation (metadata only):** `MAFUTANI_COIN_USD_VALUE` (default `100000`, i.e. **$100,000 USD per coin reference value**). This is configured/reference display/accounting metadata — **not a market price, quote, guarantee, offer, or financial advice**.
- **Epoch:** `182 days` (6 months) from fixed genesis reference.
- **Epoch seeding:** each epoch may include **exactly one** deterministic `epoch_seed` transaction minting **100,000,000,000 MFT (100B)**. Seed outputs are created at the current epoch.
- **Token expiry / replacement:** outputs expire after `TOKEN_LIFETIME_EPOCHS` (default `1`, i.e. at the next epoch boundary as implemented). **Expired outputs are not spendable**. The per-epoch seed replaces expiring supply under deterministic rules for all nodes, avoiding unbounded stale-token accumulation without discretionary minting.
- **PoW:** `SHAKE256(header||nonce)` compared to an adjustable target (`chain.consensus.meets_target`). SBC-friendly defaults (pure-Python mineable), difficulty adjusts by interval. Not designed to compete with ASIC networks.
- **Storage/model:** separate SQLite DB (`MAFUTANI_CHAIN_DB`, default `chain/mft.db`). UTXO model; amounts stored as integer strings (safe beyond SQLite signed 64-bit). Block headers support Merkle/state-root abstractions (v2+ fields) while preserving legacy compatibility for existing seeded/genesis-era blocks as implemented.

## Cryptography (Hash-based PQC approach)
Implemented in `chain/pqc.py` (stdlib only): **`MFT-PQC-WOTS-MERKLE-SHAKE256-v1`** (WOTS+ with Winternitz `w=16`, authenticated by a Merkle tree, SHAKE256 hashes).

- **Design intent:** hash-based signatures are a conservative post-quantum approach relative to RSA/ECDSA assumptions.
- **Important limitations:** custom implementation for this project, **not externally audited**. Wallet operation is **stateful** (WOTS one-time leaves; wallet tracks `next_index`). **Never reuse a signature index** and **never restore an old wallet backup/state**. Default height (`H=10`) gives 1024 signatures/key; rotate keys before exhaustion.

## Merkle Abstractions & Proofs
Implemented in `chain/merkle.py` and `node/proofs.py` (stdlib only):
- Generic binary Merkle tree (SHA3-256/domain separation, deterministic caller ordering).
- Tx/block/state/UTXO-style commitments and inclusion proofs via API/service.
- Purpose: integrity/proof workflows and composable abstractions across chain state — **not** a substitute for consensus validation, audits, or secure operations.

## Node Services
`node/` provides node-level components beyond raw chain DB: mempool staging (`node/mempool.py`, admission is not consensus authority), Merkle proof service (`node/proofs.py`), and transport-agnostic P2P/sync placeholders where present.

## Wayland-like Node Compositor Desktop
`compositor/` implements a **Wayland-inspired** in-process model (not libwayland, not DRM/KMS): `wl_display`/registry/globals, `wl_compositor`+surface, output, seat/pointer/keyboard abstractions, `xdg_wm_base`/toplevel/popup semantics, commit/damage, scene graph, and desktop shell tasks/workspaces. `compositor/service.py` exposes state/actions for the web renderer at `#/desktop` and compositor/node APIs.

## M-Pesa / Daraja Withdrawals
Implemented in `minting/daraja.py` via stdlib `urllib`. Admin-only endpoints:
- `GET /api/admin/daraja/status` — config presence (no secrets returned).
- `POST /api/admin/daraja/withdraw` — B2C payout request. Requires admin, CSRF, audit, and `confirm:true`. Requires operator configuration (`DARAJA_CONSUMER_KEY/SECRET`, shortcode, initiator, `DARAJA_SECURITY_CREDENTIAL` pre-encrypted per Safaricom requirements, callbacks).

**Operational note:** Fiat settlement depends on Daraja callbacks/reconciliation, float, recipient validity, idempotency, and compliance. Request acceptance is not final settlement.

## API Surface (current)
- Chain: `GET /api/chain/info`, `GET /api/chain/blocks`, `GET /api/chain/balance?address=...`, `GET /api/chain/wallets`, `POST /api/chain/wallet/create`, `POST /api/chain/tx/send`, `POST /api/chain/mine` (admin), `POST /api/chain/epoch/seed` (admin)
- Merkle/proofs/commitments: `GET /api/chain/proof/tx`, `GET /api/chain/commitment`
- Node: `GET /api/node/status`
- Compositor: `GET /api/compositor`, `POST /api/compositor/action`
- Locale/i18n: `GET /api/i18n`, `GET /api/i18n/messages?lang=...`
- Policies: `GET /api/policies`
- Admin Daraja: `GET /api/admin/daraja/status`, `POST /api/admin/daraja/withdraw`

See `api/routes.py` for authoritative dispatch and auth requirements.

## Warnings
This chain, PQC, wallet, node, compositor, and payment integration code is for controlled/experimental use. Do not treat balances, proofs, or reference values as guaranteed financial outcomes. Secure hosts, backups, PQC wallet state, credentials, and admin access. Review [Policies & security stance](POLICIES.md) for privileged-attack controls and residual imbalances.
