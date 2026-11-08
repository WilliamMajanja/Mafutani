# RFF-001: Funding ledger and Minima off-ramp

Status: implemented, with one measured blocker (no funded node)
Supersedes the earlier draft of this file, which described a minting service.

## What this is

An admin-controlled record of what the platform owes each account, and a
withdrawal pipeline that only reaches a chain after an explicit approval and
only from a node that can prove it holds the funds.

## What this is not

It is not a minter, and the previous draft of this RFF was wrong to imply
otherwise. Mainnet supply is fixed by consensus; `send` moves existing coins
and cannot create them. A "seed" that writes a database row does not put
Minima in a wallet, and calling that a mint is what produced the earlier
confusion where an operator saw `ok: true` and a destination balance of zero.

The two operations are kept apart on purpose:

| operation | moves a coin | tables | code |
|---|---|---|---|
| ledger credit | no | `mint_accounts`, `mint_entries` | `MintingService.credit` |
| withdrawal | yes, via the node | `mint_withdrawals` | `MintingService.execute` |

## Acceptance criteria

- [x] No third-party import in `minting/`; covered by `tests/test_zero_dependency.py`
      and a package-local guard, both with a positive control.
- [x] Admin tier + CSRF on every write; every transition audited to `audit_log`.
- [x] Amounts handled as exact integers at the token's real precision
      (1 MINIMA = 10**44 base units, read from the node's `tokens`).
- [x] Raw amounts stored as decimal `TEXT`, because SQLite `INTEGER` is 64-bit
      and overflows on a single coin.
- [x] Credits idempotent via `request_key`; a retry cannot double-credit.
- [x] A pending withdrawal cannot be executed; approval is a separate act.
- [x] A node refusal marks the withdrawal `failed` and returns the reservation.
- [x] A broadcast withdrawal cannot be unwound locally.
- [x] Destination addresses checksum-verified locally before anything is signed.
- [x] `reconcile` rebuilds a balance from the append-only entries.
- [x] UI at `#/funding`, vanilla JS, no build step.
- [ ] **Funded node.** Measured: the reachable node has `coins: 0`. Execution is
      implemented and refuses cleanly; it cannot succeed until the node holds
      Minima.

## Operational notes

Credentials are read from the environment, never argv:

```
MINIMA_HOST, MINIMA_MDS_PORT | MINIMA_RPC_PORT
MINIMA_MDS_PASSWORD           # Android: the MDS password
MINIMA_RPC_PASSWORD           # desktop: -rpcpassword, user is always 'minima'
MINIMA_CA_FILE                # pin the node's self-signed cert
MINIMA_TLS_VERIFY=0           # last resort, not a default
```

TLS defaults to verified. Minima ships a self-signed `CN=localhost`
certificate, so a node reached by LAN address cannot satisfy hostname
verification; pinning the certificate as the sole trust anchor keeps the
authenticity check without disabling it.

Broadcasting from the CLI needs `MAFUTANI_ALLOW_BROADCAST=1` in addition to
`--broadcast`, so a stray flag cannot move funds.

## Evidence

See `docs/minima-node-findings.md` for the protocol read out of the node
source, the live measurements, and the address cross-checks.

## Addendum (current stance, 2026-11-08)

- **Minima vs Mafutani Chain:** this RFF covers Minima funding/off-ramp behavior. Mafutani Chain (MFT/CommCoin) is a separate stdlib chain in `chain/` with its own consensus/PQC/wallet model and must not be conflated with Minima.
- **Daraja/M-Pesa bridge:** `minting/daraja.py` and admin Daraja routes provide an additional admin-guarded payout path (`confirm:true`, CSRF, audit). It has separate settlement/compliance/reconciliation requirements.
- **Privileged controls / residual risks:** current controls (admin+CSRF, audit, separate approval before execution, idempotency/reservation semantics, explicit broadcast gates) reduce risk but do not provide universal maker-checker/dual control. For high-value operations, operators should add separation of duties, mandatory step-up 2FA, allowlists, velocity limits/delays, out-of-band confirmation, secret stores/HSM custody where appropriate, and reconciliation/monitoring. This is relevant to privileged/key-compromise-style attack vectors seen in the crypto industry; this document does not fabricate incident-specific details.
- **Status hygiene:** "measured blocker" and node measurements in evidence docs are point-in-time observations; verify the current node state before operating.
