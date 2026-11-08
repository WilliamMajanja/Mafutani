# RFC-001: Funding ledger and Minima off-ramp (zero-dependency)

Status: implemented; superseded the "minting service" framing this file
originally proposed.

## Decision

Do not build a minter. Build a funding ledger plus an off-ramp.

Mainnet supply is a consensus constant and `send` moves existing coins. A web
service cannot add to it, so a "mint" endpoint can only ever be a database
write wearing the wrong name — and an operator who reads `ok: true` as money
is worse off than one who is told plainly that nothing was created.

What an operator actually needs is:

1. a durable record of what the platform owes each account,
2. a withdrawal request that reserves those funds,
3. an approval step that a single click cannot skip,
4. a broadcast that fails loudly when the node cannot pay it, and
5. an audit trail that survives all four.

## Shape

`minting/` is stdlib-only and split so the chain-facing and ledger-facing code
cannot reach each other's tables:

- `minima.py` — transports. `MinimaRPC` for desktop nodes, `MinimaMDS` for
  Android nodes (no RPC listener), `MinimaNode` for the amount and address
  rules plus the send preflight.
- `service.py` — the ledger and the lifecycle. No network calls.

Withdrawal states: `pending -> approved -> submitted -> confirmed`, with
`failed` and `rejected` reachable from any pre-broadcast state. Reservation is
taken at request time and released on rejection or failure; a submitted
withdrawal cannot be unwound locally, because the chain is the authority.

## Rejected alternatives

- **Mint endpoint.** Cannot work on mainnet, and the name hides that.
- **Float amounts.** Minima has 44 decimal places; `4e6` survives a float round
  trip, which is exactly why a float would not have been caught until a real
  transfer moved dust.
- **`INTEGER` columns.** SQLite's is 64-bit and one coin is 10**44, so the
  first realistic credit raises `OverflowError`. Stored as decimal `TEXT`.
- **Auto-approve on request.** Then the approval step is decoration.
- **Trusting the node's TLS by default.** Minima uses a self-signed
  `CN=localhost`, which invites turning verification off permanently. Pinning
  keeps the check.

## Consequence for the product

Off-ramp is now honest about its dependency: it needs a node holding Minima.
That is an operational fact, not a code gap. See
`docs/minima-node-findings.md` §7.

## Addendum (current stance, 2026-11-08)

- **Separate from Mafutani Chain (MFT/CommCoin).** Minima funding/off-ramp is for the **Minima** network. Mafutani also implements an independent experimental chain in `chain/` (MFT/CommCoin, hash-based PQC, SBC-mineable PoW, 182-day epoch seeding of 100B MFT, deterministic expiry/replacement, Merkle/proofs, Wayland-like node compositor desktop). Do not conflate Minima and MFT balances, addresses, wallets, or consensus.
- **Additional bridge path:** `minting/daraja.py` provides a stdlib Daraja/M-Pesa B2C payout bridge (admin+CSRF+audit+explicit `confirm:true`). It is a separate fiat/mobile-money operational path, not Minima settlement and not MFT consensus. Request acceptance is not settlement; operators own callbacks/reconciliation, credentials, and KYC/AML/sanctions/tax/compliance.
- **Privileged-attack resistance / residual risk:** RFC-001's approval step remains important, but admin+CSRF+confirmation alone is **not** enforced maker-checker/dual control. To reduce privileged/key-compromise-style attack vectors seen in the crypto industry, operators should add separation of duties, step-up 2FA, allowlists/velocity limits/delays/cooling-off, out-of-band confirmation, secret stores/HSM-backed custody where appropriate, and monitoring/reconciliation. See `docs/POLICIES.md`.
- **No fabrication:** This addendum does not assert incident-specific details about any third-party breach; it states controls and residual gaps relevant to Mafutani's current design.
