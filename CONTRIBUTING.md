# Contributing to Mafutani

Thanks for helping improve Mafutani. This project values **correctness, honesty,
security, and zero-dependency discipline**.

## Ground Rules

- **Stdlib-only runtime:** application/runtime code must not add third-party
  Python/JS dependencies. The zero-dependency guard is enforced by
  `tests/test_zero_dependency.py`.
- **No fake guarantees:** do not present CommCoin/MFT, pricing, KYC, off-ramps,
  or security controls as guaranteed, audited (unless they are), or legally
  compliant by default. `$100,000/CommCoin` is reference/config metadata only.
- **Security first:** do not commit secrets, wallet seeds, `.env` files, local
  DBs, or credentials. Use `SECURITY.md` for vulnerability reports.
- **Small, reviewable changes:** prefer focused PRs with tests and doc updates.

## Development Setup

```bash
git clone https://github.com/WilliamMajanja/Mafutani.git
cd Mafutani
python3 --version   # Python 3.x with stdlib
python3 scripts/seed.py
python3 app.py
```

Open `http://localhost:8000` (or `PORT=...`). Demo creds (if seeded): `demo@local` / `demo1234`.

## Tests

```bash
python3 -m pytest -q
# focused checks
python3 -m pytest tests/test_zero_dependency.py tests/test_chain_mft.py tests/test_minting.py -q
python3 -m py_compile app.py api/routes.py
node --check web/app.js
```

## Areas

| Area | Path | Notes |
|---|---|---|
| API/routes | `api/routes.py` | auth/CSRF/admin/risk gates; keep changes audited |
| Mafutani Chain | `chain/` | consensus/PQC/ledger; experimental/unaudited; deterministic rules |
| Node/PiNet | `node/` | mempool, proofs, sync/export/import, SBC capture |
| Compositor | `compositor/` | Wayland-like in-process model + web renderer |
| Off-ramps/KYC | `minting/` | Daraja + M-Pesa off-ramp + zero-dep KYC hooks |
| Web UI | `web/` | vanilla JS/CSS; no npm build required |
| Tests | `tests/` | stdlib `unittest`/`pytest` compatible |

## Pull Requests

1. Describe the problem, behavior change, and risk.
2. Add/update tests for behavior and edge cases.
3. Update docs (`README.md`, `docs/`, `CHANGELOG.md`) when user/operator behavior changes.
4. For consensus/monetary/PQC/custody/payout/admin-control changes, include migration notes and explicit risk review.
5. Keep commits clear; avoid unrelated refactors.

## Documentation "Sticker" Quality Checklist

- [ ] Accurate for current implementation
- [ ] No stale Minima/Binance integration claims
- [ ] Honest security/financial/compliance disclaimers
- [ ] Copy-pasteable commands
- [ ] Config/env vars documented
- [ ] Links work within the repo

## License

Repository licensing is defined by `LICENSE`. Do not assume external
contribution terms beyond what the repository owner has explicitly granted.
