/**
 * Operator policies served by /api/policies and rendered by the Policies page.
 * These are the documents this deployment actually enforces or requires.
 */

export type PolicyBundle = {
  summary: Record<string, string>;
  caveats: string[];
  document: string;
  updated: string;
};

const UPDATED = "2026-11-24";

const DOCUMENT = `MAFUTANI OPERATING POLICIES
Updated ${UPDATED}

1. RISK DISCLOSURE
Trading involves substantial risk of loss. Live orders are executed through the
operator-configured MetaTrader 5 bridge and may be rejected, partially filled,
or filled at a worse price than shown. Market data on this deployment is delayed
daily history from Yahoo Finance and is not a real-time execution feed. Nothing
here is financial, investment, legal, tax, or accounting advice.

2. ORDER HANDLING
Paper order entry is disabled on hosted deployments. Live orders require:
  * a user tier with live_enabled set,
  * a reachable MT5 bridge over HTTPS,
  * explicit confirm:true from the order ticket,
  * passing the account risk limits (max notional, max position, kill switch).
MT5 retcode 10009 is acceptance, not a fill. The terminal reports open until the
EA reports a fill. An order whose result is lost is never retried blindly.

3. ACCESS CONTROL
Sessions are HttpOnly cookies with SameSite=Lax and Secure on HTTPS, plus a
bearer token fallback. Admin routes require the Admin tier. Setup endpoints
require ADMIN_SETUP_TOKEN. Bootstrap administrators are configured by
GLOBAL_ADMIN_EMAILS and GLOBAL_ADMIN_PASSWORD and are never shared accounts.
All privileged actions are written to audit_log.

4. DATA AND PROVENANCE
Candles and quotes carry a source column. Synthetic rows are never presented as
venue data. The UI reads only stored rows; missing data is shown as missing.
Daily bars are never loaded into the one-minute table.

5. SECURITY
Transport is HTTPS with HSTS. Responses carry CSP, nosniff, frame-deny and a
strict referrer policy. API requests are rate-limited per edge isolate. Secrets
live in environment variables, never in the repository. Report vulnerabilities
privately per SECURITY.md.

6. COMMCOIN / MFT
CommCoin, MFT wallets, mining, and the M-Pesa off-ramp are provided by the
separate Python chain service. This deployment proxies those routes only when
CHAIN_API_URL is configured. Token expiry, PQC key state, and epoch seeding are
consensus-critical; operators must persist chain state and never restore stale
wallet backups. MFT is experimental and may lose all value. Chain anchors are
not legal title or registry proof.

7. KYC / AML HOOKS
KYC review and off-ramp approval are operator workflows, not certifications.
Operators remain responsible for sanctions screening, record retention, and
applicable licensing in their jurisdiction.

8. MARKET ABUSE
Wash trading, spoofing, layering, and coordinated manipulation are prohibited.
Audit records, order timestamps, and account identifiers are retained to support
investigation and regulatory requests.

9. INCIDENT RESPONSE
On suspected compromise: disable the account, revoke its sessions, rotate the
MT5 bridge token and chain token, preserve audit_log, and notify affected
parties as required by law. Do not delete logs before capture.

10. BUSINESS CONTINUITY
Database backups are Neon point-in-time restores. Chain state is backed up by
the chain operator. MT5 execution depends on the bridge host and terminal.
Recovery objectives and failover runbooks are operator-owned.

11. VENDOR AND MODEL RISK
Market data (Yahoo), MT5 execution, Neon Postgres, and the Neon AI Gateway are
third-party dependencies. Model output is research assistance, not a trading
signal or guarantee. Vendor outages are surfaced in the UI, not hidden.

12. CHANGES
Policy changes require review, an audit entry, and a redeploy.`;
export function policies(): PolicyBundle {
  return {
    summary: {
      trading_mode: "hosted: live-only; local development: paper",
      live_venue: "MetaTrader 5 via operator bridge",
      data_source: "Yahoo Finance daily history",
      execution_gate: "tier + bridge + confirm + risk limits",
      auth: "HttpOnly session cookie + bearer token; Admin tier for ops",
      chain: "separate Python CommCoin service",
    },
    caveats: [
      "Delayed daily market data is not a real-time execution feed.",
      "MT5 acceptance is not a fill; reconcile by ticket.",
      "CommCoin/MFT is experimental and can lose all value.",
      "KYC and off-ramp workflows are plumbing, not legal compliance.",
    ],
    document: DOCUMENT,
    updated: UPDATED,
  };
}
