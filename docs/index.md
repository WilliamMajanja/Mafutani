# Mafutani Documentation

> Zero-dependency, stdlib-only platform. Experimental trading + Mafutani Chain (CommCoin/MFT) features.

## Overview
Mafutani is a zero-dependency platform built with Python stdlib only (`http.server`, `sqlite3`, `urllib`, etc.). It includes trading/automation (paper-first, gated live controls), a web UI, terminal, admin/auth/audit tooling, and **experimental** Mafutani Chain capabilities (MFT/CommCoin): hash-based PQC signatures, SBC-mineable PoW, deterministic epoch seeding/token expiry replacement, Merkle commitments/proofs, a node compositor (Wayland-like model + web desktop), locale/i18n, and admin-guarded off-ramp bridges.

## Current Stance (summary)
- **No external runtime dependencies** in the app tree; enforced by `tests/test_zero_dependency.py`.
- **Trading:** paper by default; live paths are gated/audited and risk-managed — not financial advice.
- **Mafutani Chain / MFT / CommCoin:** experimental, unaudited. 182-day epoch; **100,000,000,000 MFT** seed per epoch; outputs expire and are replaced by deterministic seeding rules (see [Mafutani Chain](MAFUTANI_CHAIN.md)).
- **Valuation:** `$100,000` per CommCoin/MFT is **reference/config metadata only** (`MAFUTANI_COIN_USD_VALUE`), not a market price or guarantee.
- **Payments:** Daraja/M-Pesa and Minima funding flows are admin-guarded; acceptance != settlement; operator owns compliance and secure operations.
- **Security:** layered controls exist (auth/admin/CSRF, audit logging, approval-before-execute funding lifecycle, risk checks), but privileged-attack resistance is **not absolute**. Review residual risks and operator mitigations in [Policies](POLICIES.md).

## Docs
- [Policies & security stance](POLICIES.md)
- [Mafutani Chain (MFT/CommCoin)](MAFUTANI_CHAIN.md)
- [Minima node findings](minima-node-findings.md)
- [RFCs](rfcs/)
- [RFFs](rffs/)
- [Research: Sub-agent trading](research-subagent-trading.md)

## Quick Start
```bash
python3 scripts/seed.py
python3 app.py
# optional: disable feed for local/CI
# PORT=8000 MAFUTANI_NO_FEED=1 python3 app.py
```

## Key UI Areas
Markets, Chain (`#/chain`), Desktop (`#/desktop`), Wallet, Funding (`#/funding`), Policies (`#/policies`), plus trading/admin views depending on role/config.

## GitHub Pages
This site is published via GitHub Pages. Release/package links may be added from workflows/releases.
