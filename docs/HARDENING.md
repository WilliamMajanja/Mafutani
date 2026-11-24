# Mafutani Hardening Baseline (CPIP-informed)

Reference: `WilliamMajanja/CPIP-` is a defensive, report-only source. Mafutani
does **not** vendor, depend on, or auto-apply CPIP code or configuration.
Ideas are adopted only after manual review and are implemented in Mafutani's
own stdlib-only / TypeScript code.

## Control baseline

| ID | Area | Control | Status | Evidence |
| --- | --- | --- | --- | --- |
| H-01 | Execution | Hosted is live-only; paper orders rejected | pass | `TRADING_MODE=live`, 403 on paper |
| H-02 | Access | Setup endpoints require ADMIN_SETUP_TOKEN | pass | `/api/admin/init`, `/api/admin/seed` |
| H-03 | Access | Bootstrap admins from env, no shared demo passwords | pass | `GLOBAL_ADMIN_EMAILS`, demo users deleted |
| H-04 | Access | Admin tier on every admin route | pass | `adminUser()` gate |
| H-05 | Access | Session revocation per user | pass | `POST /api/admin/users/:id/revoke` |
| H-06 | Access | TOTP / SSO | fail | enrolment returns 501 in Netlify port |
| H-07 | Logging | Audit log for auth, orders, admin, bridge tests | pass | `audit_log` |
| H-08 | Logging | Journal for trades, backtests, AI reviews, UX errors | pass | `journal.kind` |
| H-09 | Transport | HTTPS + HSTS + CSP + frame-deny + nosniff | pass | `netlify.toml` headers |
| H-10 | Transport | Live bridge token only over HTTPS | pass | `lib/mt5.ts` rejects non-loopback HTTP |
| H-11 | Availability | API rate limit | partial | edge per-isolate, 300/min; not global |
| H-12 | Input | Request body size limit | partial | add to `readBody` |
| H-13 | Execution | Live venue configured | fail | no Alpaca/MT5 credentials yet |
| H-14 | Chain | CommCoin service reachable | fail | `CHAIN_API_URL` not set |
| H-15 | Assurance | External penetration test | manual | not recorded |
| H-16 | Compliance | KYC/AML/sanctions program | manual | hooks only, no certification |
| H-17 | Continuity | Backup/restore test and RTO/RPO | manual | Neon PITR; chain volume |
| H-18 | Data | Delayed daily history labelled; partial bar flagged | pass | `/api/universe` coverage |

## CPIP concepts adopted

- **Explicit posture visibility** -> Admin hardening baseline and gap register.
- **Separate simulated/draft from real capability** -> setup panels instead of
  fake chain balances or live fills.
- **Transport hardening mindset** -> HTTPS-only bridge, CSP, HSTS, origin-safe
  session cookies, no secrets in URLs.
- **Honest crypto labelling** -> CommCoin marked experimental/unaudited; PQC is
  not FIPS-certified.
- **Report-only external references** -> no auto-update/auto-apply of CPIP.

## Next actions (ordered)

1. Implement TOTP or SSO (H-06).
2. Replace per-isolate rate limiting with a shared store (H-11).
3. Add request body limits at the function boundary (H-12).
4. Configure Alpaca or MT5 and complete a reconciled live test order (H-13).
5. Expose the chain service with TLS and token auth (H-14).
6. Commission an external penetration test and remediate (H-15).
7. Complete KYC/AML/sanctions and custody/legal review (H-16).
8. Test a Neon PITR restore and chain-state backup (H-17).

## Verification

```bash
curl -sI https://mafutani.org/ | grep -Ei 'content-security|x-frame|x-content|strict-transport'
curl -s https://mafutani.org/api/status
curl -s https://mafutani.org/api/admin/hardening -H "X-Session: $TOKEN"
```

No control in this document is a certification or guarantee. Production
reliance still requires independent audits, threat modeling, adversarial
testing and operator compliance programs.
