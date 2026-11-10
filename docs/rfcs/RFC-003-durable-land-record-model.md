# RFC-003: Durable Land/Asset Record Model (proposal; not implemented)

> **Status:** Proposed — design only. No consensus, API, or UI changes implemented.
> **Scope:** Mafutani Chain (CommCoin/MFT), land/asset record anchoring.
> **Related:** docs/POLICIES.md §3a, README.md "Honest Risk Summary", chain/consensus.py, chain/ledger.py.

## 1. Problem

The current MFT chain has `TOKEN_LIFETIME_EPOCHS = 1`: every output expires at
the next epoch boundary and is replaced by the deterministic per-epoch seed.
This is by design for CommCoin/MFT (prevents unbounded stale accumulation) but
makes the chain **unsuitable for durable land/title/asset records** — any
record anchor carried in a transfer output would become unspendable after one
epoch, and the on-chain proof of a land transaction could be lost or invalidated.

The current `record` field (added in commit `ee754fb`) is a **bounded metadata
anchor** in transfer transactions. It is not a legal title, registry, or proof
of ownership. It survives only as long as the containing UTXO is spendable.

## 2. Goals

1. Allow **durable, non-expiring record anchors** for land/asset transactions
   without changing the monetary policy (epoch reward, token expiry for
   CommCoin/MFT value transfers).
2. Keep the chain **experimental/unaudited** — no claim of legal title, registry,
   or ownership enforceability.
3. Preserve stdlib-only, zero-dependency runtime.
4. Maintain backward compatibility with existing txs/blocks (additive only).

## 3. Non-goals

- Legal title, registry integration, or enforceability (operator/legal responsibility).
- Replacing audits, formal/threat modeling, or PQC migration.
- Changing `TOKEN_LIFETIME_EPOCHS` or epoch reward for value transfers.
- Auto-applying external code/config (CPIP remains report-only).

## 4. Proposed design

### 4.1 Separate record layer (not UTXO-bound)

Introduce a distinct transaction type `record_anchor` (or a block-level
commitment) that:

- Has **no inputs and no spendable outputs** (or outputs with
  `TOKEN_LIFETIME_EPOCHS = infinity` / a special "permanent" flag).
- Carries a bounded `record` payload (hashes/references, parcel ID, jurisdiction,
  signer authority, timestamp, etc.).
- Is **not spendable** and **not subject to token expiry** — it is an
  append-only audit entry, not a value transfer.
- Is included in block Merkle roots and chain commitments (existing
  `merkle_root`, `state_root` abstractions).

### 4.2 Consensus changes (additive)

- New tx type: `record_anchor` (or `anchor`).
- Validation rules:
  - No inputs (or inputs optional for authority attestation).
  - Outputs: zero or a single "null" output (amount 0, non-spendable marker).
  - `record` required and bounded by `MAX_RECORD_BYTES`.
  - Does NOT consume UTXOs or PQC signature capacity for value (but may require
    a separate authority signature).
- Block validation: `record_anchor` txs counted in Merkle root; no effect on
  UTXO set or balance.
- Difficulty/PoW unchanged.

### 4.3 Authority and identity

- Land records require **signing authority** (not just any wallet). Options:
  - A dedicated "authority" keypair (admin/operator-controlled, separate from
    user wallets).
  - Multi-sig or threshold signatures (future).
  - KYC/identity verification is operator responsibility (zero-dep hooks exist).
- Authority signatures use the same PQC scheme (WOTS+/Merkle) or a vetted
  standardized scheme (SLH-DSA/ML-DSA) after migration.

### 4.4 Size and storage

- On-chain: bounded `record` (hashes + references, not full documents).
- Off-chain: full documents stored by operator; on-chain anchor = hash +
  metadata pointer.
- `MAX_RECORD_BYTES` env-configurable (default 16384; may increase for
  land-specific fields).
- Block size limits: `MAX_BLOCK_TXS` already bounds txs per block; record txs
  count toward this limit.

### 4.5 Indexing and query

- New index: `record_anchors` table (txid, height, epoch, parcel_ref, hash,
  jurisdiction, signer, ts) for efficient lookup.
- API: `GET /api/chain/records?parcel=...` (read-only, no guarantees of
  legal validity).
- Proof: Merkle proof of inclusion in block (existing `ProofService`).

### 4.6 Token expiry separation

- Value transfers: `TOKEN_LIFETIME_EPOCHS = 1` (unchanged).
- Record anchors: `TOKEN_LIFETIME_EPOCHS = ∞` (never expire) OR a separate
  "permanent" flag in the output/tx.
- This requires either:
  a) A new output type with `permanent=True`, or
  b) A separate storage path (not UTXO-based) for records.

Option (b) is preferred: records are not UTXOs, they're audit entries. No
spendability, no expiry, no balance effect.

## 5. Migration and compatibility

- Additive: existing txs/blocks remain valid (new tx type is optional).
- New `record_anchors` table created via safe migration (like `state_root`
  column addition in `_init_db`).
- Old nodes that don't understand `record_anchor` tx type would reject blocks
  containing them → requires a version bump or soft-fork coordination.
- Consensus version bump recommended (e.g., `CHAIN_ID` suffix or block version).

## 6. Risk and caveats (must be documented)

- **Not legal title, registry, or proof of ownership.** Record anchors are
  technical audit entries. Legal validity requires jurisdictional process,
  authoritative registries, and qualified legal advice.
- **Experimental/unaudited.** No guaranteed security, consensus correctness,
  value, or compliance. No certification for finance, land/title, healthcare,
  government, or safety-critical use.
- **PQC stateful.** Authority signatures consume PQC indexes; same wallet
  safety rules apply (never reuse/restore stale state).
- **No replacement for audits.** Production reliance requires independent
  crypto/consensus/security audits, formal/threat modeling, adversarial/fuzz/
  fork-stall testing, vetted standardized PQC migration (SLH-DSA/ML-DSA or
  audited implementations) with versioned test vectors, and operator
  compliance/custody controls.
- **CPIP/external refs:** report-only. No safe auto-update/auto-apply.

## 7. Implementation order (if approved)

1. RFC ratification + threat model + legal review.
2. Consensus spec update (`chain/consensus.py`, `chain/ledger.py`).
3. `record_anchor` tx type + validation + Merkle inclusion.
4. `record_anchors` table + migration + index.
5. Authority keypair management (separate from user wallets).
6. API endpoints (read-only query + proof verification).
7. UI (anchor form + query + proof display).
8. Tests (canonical bytes, validation, Merkle proof, expiry separation).
9. Docs update (README, POLICIES, SECURITY, CHANGELOG).
10. Audit + PQC migration plan.

## 8. Open questions

- Should record anchors require a fee (anti-spam)? If so, in MFT or a
  separate fee token?
- Should there be a maximum number of record anchors per block?
- How to handle record amendments/corrections (new anchor referencing old)?
- Should authority keys be HSM-backed or software (operator choice)?
- What jurisdictional requirements apply (data protection, retention, etc.)?

## 9. References

- RFC-001 (minting/off-ramp, superseded Minima framing)
- RFC-002 (GitHub Pages release)
- docs/POLICIES.md §3a (land/asset records)
- chain/consensus.py (TOKEN_LIFETIME_EPOCHS, EPOCH_REWARD)
- chain/ledger.py (canonical_tx_bytes, validate_tx, MAX_RECORD_BYTES)
- engines/dataprov.py (provenance gating)
