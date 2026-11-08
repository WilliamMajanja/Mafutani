# RFC-002: GitHub Pages Hosting for Release + Package Docs

## Status
Draft (review implementation state before treating as complete)

## Context
Configure for GitHub Pages hosting (release + package). Provide documentation site and release artifacts publishing.

## Requirements
- Static docs site (can live under `docs/` or `web/` adapted)
- Release automation guidance (GitHub Actions)
- Package metadata if distributed
- Zero-dependency static assets

## Design
- Use GitHub Pages from `docs/` branch or `docs/` folder
- Generate API docs from code (stdlib tools) if needed
- Link to releases, badges, runbook
- Ensure CSP/headers appropriate for static site

## Implementation Plan
1. Create `docs/index.md`, `_config.yml` or rely on default
2. Add GitHub Actions workflow for pages (or configure manually)
3. Document release process
4. Add package distribution notes

## Current stance addendum (2026-11-08)
- `docs/` contains the primary platform documentation, including current Mafutani Chain (MFT/CommCoin), Merkle/compositor/node, locale/i18n, Daraja, and policies/security stance. Keep published docs aligned with `README.md` and `docs/*`.
- If Pages/release workflows exist (`.github/workflows/*`), verify they match the repository's actual release process, permissions, and zero-dependency posture before enabling in production.
- Security/policy pages are disclosures, not legal/compliance guarantees; operators must add jurisdiction-specific notices.

## References
- Existing `../research-subagent-trading.md`
- [Policies & security stance](../POLICIES.md)
- [Mafutani Chain](../MAFUTANI_CHAIN.md)
