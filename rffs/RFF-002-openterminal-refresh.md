# RFF-002: OpenTerminal Refresh (Zero-Dep)

## Feature
Refresh/build OpenTerminal properly - Bloomberg-style terminal with commands, fully zero-dep (vanilla JS + Python backend).

## Acceptance Criteria
- Vanilla JS only in web/terminal.js (no npm deps)
- Backend in core/terminal.py integrated cleanly
- Commands work as documented
- Zero-dependency maintained
- Tests pass for terminal functionality

## Current stance addendum (2026-11-08)
- OpenTerminal remains part of the broader stdlib-only platform, which now also includes Mafutani Chain (MFT/CommCoin), Merkle/proof/compositor/node surfaces, locale/i18n, and admin-guarded funding/off-ramp flows. Terminal command coverage should be verified against current `core/terminal.py`/`web/terminal.js`/API behavior before documenting specific commands as supported.
- Zero-dependency enforcement applies to the application tree (`tests/test_zero_dependency.py`); do not add npm/runtime deps for terminal features.
- Terminal/admin surfaces are privileged operational interfaces; apply the same auth/CSRF/audit and deployment hardening expectations described in `docs/POLICIES.md`.
