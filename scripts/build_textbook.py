#!/usr/bin/env python3
"""Build the Mafutani operator and strategy textbook PDF.

    python3 scripts/build_textbook.py

Output: docs/Mafutani_Textbook.pdf
"""
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether,
)

OUT = "docs/Mafutani_Textbook.pdf"
TITLE = "Mafutani Trading System"
SUBTITLE = "Operator Textbook, Strategy Guide, and Integration Blueprints"

styles = getSampleStyleSheet()
styles.add(ParagraphStyle(name="CoverTitle", parent=styles["Title"], fontSize=30,
                          leading=36, textColor=colors.HexColor("#0b3d2e"),
                          spaceAfter=10, alignment=TA_CENTER))
styles.add(ParagraphStyle(name="CoverSub", parent=styles["Normal"], fontSize=13,
                          leading=18, textColor=colors.HexColor("#35544a"),
                          alignment=TA_CENTER, spaceAfter=6))
styles.add(ParagraphStyle(name="H1x", parent=styles["Heading1"], fontSize=18,
                          leading=22, textColor=colors.HexColor("#0b3d2e"),
                          spaceBefore=14, spaceAfter=8))
styles.add(ParagraphStyle(name="H2x", parent=styles["Heading2"], fontSize=13,
                          leading=17, textColor=colors.HexColor("#1d5c48"),
                          spaceBefore=12, spaceAfter=6))
styles.add(ParagraphStyle(name="Bodyx", parent=styles["BodyText"], fontSize=9.6,
                          leading=14, spaceAfter=6, alignment=TA_LEFT))
styles.add(ParagraphStyle(name="Smallx", parent=styles["BodyText"], fontSize=8.2,
                          leading=11.5, textColor=colors.HexColor("#4a5a55")))
styles.add(ParagraphStyle(name="Codex", parent=styles["Code"], fontSize=7.6,
                          leading=10.5, backColor=colors.HexColor("#f2f5f4"),
                          borderPadding=5, leftIndent=4, rightIndent=4))
styles.add(ParagraphStyle(name="Warnx", parent=styles["BodyText"], fontSize=9,
                          leading=13, textColor=colors.HexColor("#7a2e00"),
                          backColor=colors.HexColor("#fff4e6"), borderPadding=6,
                          spaceBefore=6, spaceAfter=8))


def P(t, s="Bodyx"):
    return Paragraph(t, styles[s])


def H1(t):
    return Paragraph(t, styles["H1x"])


def H2(t):
    return Paragraph(t, styles["H2x"])


def bullets(items):
    return [P("• " + i) for i in items]


def table(rows, widths=None, header=True):
    data = [[P(str(c), "Smallx") for c in r] for r in rows]
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c8d4cf")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e4ede9"))]
    t.setStyle(TableStyle(style))
    return t


def on_page(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.HexColor("#6b7d77"))
    canvas.drawString(18 * mm, 12 * mm, "Mafutani Trading System — Operator Textbook")
    canvas.drawRightString(A4[0] - 18 * mm, 12 * mm, "Page %d" % doc.page)
    canvas.restoreState()


def build():
    doc = BaseDocTemplate(OUT, pagesize=A4,
                          leftMargin=18 * mm, rightMargin=18 * mm,
                          topMargin=18 * mm, bottomMargin=18 * mm,
                          title=TITLE, author="Mafutani")
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=on_page)])
    s = []

    # Cover
    s += [Spacer(1, 48 * mm), P(TITLE, "CoverTitle"),
          P("Paper on the Pi. Live on the hosted deployment.", "CoverSub"),
          P(SUBTITLE, "CoverSub"),
          Spacer(1, 10 * mm),
          P("This book explains what the system actually does, how to operate it, "
            "where its limits are, and how to extend it into treasury and brokerage "
            "workflows without pretending that regulated integrations exist.",
            "Bodyx"),
          Spacer(1, 8 * mm),
          table([
              ["Document", "Operator textbook v1.0"],
              ["Deployment", "Netlify + Neon Postgres (live-only) / Raspberry Pi local node (paper + CommCoin)"],
              ["Market data", "Yahoo Finance delayed daily history"],
              ["Execution", "MetaTrader 5 via operator bridge; no bridge = no live orders"],
              ["Chain", "CommCoin/MFT Python service; PQC WOTS+/Merkle/SHAKE256"],
              ["Status", "Experimental software. Not financial advice."],
          ], widths=[38 * mm, 118 * mm]),
          PageBreak()]

    # Contents
    s += [H1("Contents")]
    toc = [
        "1. What Mafutani is, and what it is not",
        "2. Architecture and deployment topology",
        "3. Accounts, tiers, and access control",
        "4. The trading day: dashboard to order ticket",
        "5. Research: screener, analysis, anomalies, backtests",
        "6. Scalp Lab and Scalp Live",
        "7. Risk, limits, and rejection semantics",
        "8. CommCoin / MFT: wallets, mining, epochs, off-ramp",
        "9. Operating the Raspberry Pi node",
        "10. Operating the hosted live deployment",
        "11. Security model and hardening checklist",
        "12. Policies and audit",
        "13. Investor and board reporting",
        "14. Treasury blueprint: CBK DhowCSD, bonds, and T-bills",
        "15. Zero-dependency broker and trader blueprint",
        "16. Strategic use cases and revenue engines",
        "17. Roadmap, gaps, and go-live gates",
        "18. Runbooks and verification commands",
    ]
    s += bullets(toc)
    s += [PageBreak()]

    # 1
    s += [H1("1. What Mafutani is, and what it is not")]
    s += [P("Mafutani is a trading terminal, market-research workspace, paper-trading "
            "laboratory, and CommCoin node operator console. It is built from a "
            "standard-library Python server for the local node and a TypeScript "
            "Netlify port for the hosted deployment, both backed by real stored data.")]
    s += [H2("It is")]
    s += bullets([
        "A real market-data viewer over stored daily candles and quotes with provenance.",
        "A real paper-trading engine on the local node: orders, fills, positions, cash, P&L.",
        "A live-order gateway on the hosted deployment through an operator-run MT5 bridge.",
        "A CommCoin node: real PQC wallets, SHAKE256 proof-of-work, epoch seeding, transfers.",
        "An audited operator console: users, tiers, symbols, sessions, readiness, policies.",
        "An AI-assisted research surface using the Neon AI Gateway over real account data.",
    ])
    s += [H2("It is not")]
    s += bullets([
        "A bank, broker-dealer, fund manager, or investment adviser.",
        "A CBK DhowCSD participant, bond dealer, or treasury-bill distribution channel.",
        "A guarantee of profit, liquidity, settlement, or token value.",
        "A replacement for licensing, KYC/AML, sanctions screening, or legal advice.",
        "A real-time execution feed: hosted market data is delayed daily history.",
    ])
    s += [P("These boundaries are deliberate. The software refuses to fabricate a fill, "
            "a chain balance, a market price, or a regulatory permission.", "Warnx")]

    # 2
    s += [H1("2. Architecture and deployment topology")]
    s += [H2("Hosted deployment (live-only)")]
    s += [P("Netlify serves the static SPA from web/ and runs the TypeScript API as a "
            "function. Neon Postgres stores users, sessions, orders, fills, positions, "
            "symbols, daily candles, quotes, journal, backtests, audit, and settings. "
            "The same deployment proxies CommCoin routes to the Python chain service "
            "when CHAIN_API_URL is configured.")]
    s += [table([
        ["Layer", "Component", "Responsibility"],
        ["UI", "web/app.js + style.css", "hash router, pages, tickets, charts, admin"],
        ["API", "netlify/functions/api.ts", "auth, portfolio, orders, screener, admin, AI"],
        ["Edge", "rate-limit.ts", "per-IP API throttling"],
        ["Data", "Neon Postgres", "durable state and audit trail"],
        ["AI", "Neon AI Gateway", "analysis and strategy review"],
        ["Live venue", "MT5 bridge", "real order acceptance and fills"],
        ["Chain", "Python CommCoin service", "wallets, PoW, epochs, off-ramp"],
    ], widths=[26 * mm, 52 * mm, 78 * mm])]
    s += [H2("Local node (paper + chain)")]
    s += [P("app.py serves the same SPA plus the Python API on port 8000. It uses SQLite "
            "(data/trading.db) and the chain database (chain/mft.db). Local trading is "
            "paper by design; the chain is real within that node. A systemd user service "
            "keeps it alive on the Pi.")]
    s += [P("TRADING_MODE=paper is set in .env.local. Hosted deployments default to "
            "live-only and reject paper orders with HTTP 403.", "Warnx")]

    # 3
    s += [H1("3. Accounts, tiers, and access control")]
    s += [P("Tiers are rows in Postgres, not hardcoded in the UI. Admin and Institutional "
            "share the 100-symbol ceiling; Admin has the highest API rate and backtest "
            "window. Live trading also requires tiers.live_enabled and a configured venue.")]
    s += [table([
        ["Tier", "Symbols", "Live", "API rate", "Backtest days"],
        ["Free", "10", "No", "60", "30"],
        ["Pro", "50", "Yes", "300", "365"],
        ["Institutional", "100", "Yes", "1200", "1825"],
        ["Admin", "100", "Yes", "3600", "3650"],
    ], widths=[32 * mm, 24 * mm, 20 * mm, 28 * mm, 32 * mm])]
    s += [P("Bootstrap administrators are configured only through GLOBAL_ADMIN_EMAILS "
            "and GLOBAL_ADMIN_PASSWORD. The seed endpoint requires ADMIN_SETUP_TOKEN. "
            "Shared demo passwords were removed from the hosted seed path.")]
    s += [P("Sessions are HttpOnly cookies (SameSite=Lax, Secure on HTTPS) with a bearer "
            "token fallback. Admin routes require the Admin tier. Every privilege change "
            "is written to audit_log.")]

    # 4
    s += [H1("4. The trading day: dashboard to order ticket")]
    s += [P("The dashboard refreshes every 15 seconds without reloading the page. It shows "
            "cash, equity, P&amp;L, open positions, all enabled instruments, recent orders, "
            "and the current risk limits. The quote tape polls the API every 10 seconds; "
            "a failed poll is labelled, never filled with invented prices.")]
    s += [H2("Order lifecycle (paper, local)")]
    s += bullets([
        "Validate symbol, side, type, quantity, and limit price.",
        "Read the latest stored quote; reject if no quote exists.",
        "Enforce the risk limits: kill switch, max notional, max position.",
        "For a marketable order, compute fill price (ask for buys, bid for sells).",
        "Write order, fill, position, and cash in a consistent sequence.",
        "Return the order and fill; the UI re-reads the portfolio.",
    ])
    s += [H2("Order lifecycle (live, hosted MT5)")]
    s += bullets([
        "Require live-enabled tier, configured bridge, and confirm:true.",
        "POST the order to the bridge; MT5 retcode 10009 means accepted, not filled.",
        "Record status open/partially_filled; never claim a fill without a reported fill.",
        "Never debit or credit the paper account for a live order.",
        "A lost result is surfaced as 'may or may not have executed — reconcile by ticket'.",
    ])

    # 5
    s += [H1("5. Research: screener, analysis, anomalies, backtests")]
    s += [P("The screener computes session change, RSI, trend, score, volume, spread, and "
            "anomaly score from stored daily candles in one pass. The analysis page adds "
            "an AI brief generated only from those figures. Anomalies flag price z-scores, "
            "volume z-scores, and gaps; the clean tape is hidden unless requested.")]
    s += [P("Backtests are long/flat signal simulations on stored candles with explicit "
            "cost per trade, drawdown, Sharpe, win rate, profit factor, CAGR, coverage, "
            "and a significance verdict. A short sample is labelled unproven. The engine "
            "does not imply an edge because a curve rose.")]
    s += [table([
        ["Metric", "Meaning"],
        ["total_return_pct", "Compounded return over the stored window"],
        ["max_drawdown_pct", "Peak-to-trough decline"],
        ["sharpe", "Trade-return Sharpe scaled by years"],
        ["win_rate_pct", "Winning round trips / trades"],
        ["pf", "Gross wins / gross losses"],
        ["coverage", "Bars and days actually available"],
    ], widths=[40 * mm, 112 * mm])]

    # 6
    s += [H1("6. Scalp Lab and Scalp Live")]
    s += [P("Scalp Lab previews each strategy on stored candles and can run a backtest or "
            "ask the AI reviewer for a critique. Scalp Live is the order ticket: quote, "
            "chart, position, P&amp;L, buy/sell, flatten. Hosted deployments default to "
            "live mode; local deployments default to paper.")]
    s += [P("Available signal logic includes EMA trend, dual EMA, EMA + COT (with an "
            "explicit COT-unavailable note), RSI fade, mean reversion, VWAP pullback, "
            "VWAP bands, opening-range breakout, top-of-book imbalance, and news sentiment "
            "when headlines are stored.")]

    # 7
    s += [H1("7. Risk, limits, and rejection semantics")]
    s += bullets([
        "max_notional: reject an order whose quantity × reference price exceeds the limit.",
        "max_position_qty: reject an order that would push the absolute position above the limit.",
        "kill_switch: reject every new order.",
        "Insufficient cash: reject buys that cannot be funded.",
        "Unknown or disabled symbol: reject before any write.",
        "Live without bridge or confirm: reject with an explicit reason.",
    ])
    s += [P("Rejections are answers, not failures. The UI prints the reason. There is no "
            "silent fallback from live to paper.", "Warnx")]

    # 8
    s += [H1("8. CommCoin / MFT: wallets, mining, epochs, off-ramp")]
    s += [P("CommCoin is a separate Python service. Wallets are PQC WOTS+/Merkle over "
            "SHAKE256 and are stateful: never restore an old wallet backup or reuse a "
            "signature index. Mining iterates nonces until SHAKE256 meets the target. "
            "Each 182-day epoch seeds 100,000,000,000 MFT once, to the address that mines "
            "the seed block. Blocks after the seed carry no block reward; distribution is "
            "by transfer from the seeded wallet.")]
    s += [P("On the Pi node, the operator wallet holds the epoch seed. The operator can "
            "allocate MFT to a user wallet with a signed transfer. This is what was done "
            "for the local node operator account: 1,000,000 MFT was transferred from the "
            "seed wallet, and blocks were mined at height 2 and 3.")]
    s += [P("The hosted app proxies /api/chain/* to CHAIN_API_URL. Without that variable, "
            "the CommCoin page shows a setup panel instead of fake balances. The M-Pesa "
            "off-ramp is the same: operator-configured, acceptance is not settlement.", "Warnx")]

    # 9
    s += [H1("9. Operating the Raspberry Pi node")]
    s += [P("The reference device is a Raspberry Pi 5 (16 GB), aarch64, Debian, Python 3.13. "
            "The node is a systemd user service. Verify with the commands below.")]
    s += [P("systemctl --user status mafutani\n"
            "curl -s http://127.0.0.1:8000/api/status\n"
            "curl -s http://127.0.0.1:8000/api/chain/info", "Codex")]
    s += [P("Mining is intentionally SBC-friendly. A 500,000-nonce round is small; the "
            "API accepts up to 2,000,000 per call. Keep the wallet file on disk, restrict "
            "permissions, and back up the current state only.")]
    s += [P("The app was logging a 500 on wallet creation because ROOT was undefined in "
            "api/routes.py. That is fixed; wallet creation and mining were verified on the "
            "Pi.")]

    # 10
    s += [H1("10. Operating the hosted live deployment")]
    s += [P("Hosted is live-only. Paper order entry returns 403. Live needs a bridge, a "
            "funded MT5 account, and a tier with live_enabled.")]
    s += [P("# on the Windows host beside MetaTrader 5\n"
            "python services/mt5/bridge_server.py --port 8031 --token <TOKEN>\n"
            "# expose over HTTPS, then set in Netlify\n"
            "MT5_BRIDGE_URL=https://bridge.example.com\n"
            "MT5_BRIDGE_TOKEN=<TOKEN>\n"
            "TRADING_MODE=live", "Codex")]
    s += [P("Admin → System readiness → Test MT5 bridge performs a live /health check. "
            "A green bridge and a funded account are the only path to live orders.")]

    # 11
    s += [H1("11. Security model and hardening checklist")]
    s += bullets([
        "HTTPS only with HSTS; bridge tokens are refused over plaintext non-loopback URLs.",
        "CSP, frame-deny, nosniff, strict referrer policy, permissions policy.",
        "HttpOnly SameSite session cookie plus bearer token fallback.",
        "Admin-tier checks on every /api/admin/* route; self-disable and self-demote blocked.",
        "Setup endpoints require ADMIN_SETUP_TOKEN; shared demo accounts removed.",
        "Per-IP API rate limit at the edge; per-session CSRF token on mutating calls.",
        "Audit every login, order, admin action, and bridge test.",
        "Secrets only in Netlify/Neon environment variables and local .env files.",
        "Wallet files mode 600; chain state backed up as current state, never stale copies.",
    ])
    s += [P("Hardening for regulated industries additionally requires independent "
            "penetration testing, key custody review, SOC 2-style controls, sanctions "
            "screening, record retention, and legal review. This build is not certified.", "Warnx")]

    # 12
    s += [H1("12. Policies and audit")]
    s += [P("The Policies page is served by /api/policies from the same document the "
            "operator enforces: risk disclosure, order handling, access control, data "
            "provenance, security, CommCoin, KYC/AML hooks, market abuse, incident "
            "response, business continuity, vendor/model risk, and change control. "
            "Every admin change is visible in the audit table.")]

    # 13
    s += [H1("13. Investor and board reporting")]
    s += [P("The Investor page is a presentable, real-data summary: equity, P&amp;L, "
            "market value, positions, universe coverage, trading mode, system readiness, "
            "strategy catalog, and policy set. It can export a JSON snapshot for due "
            "diligence. Nothing on the page is mocked; missing data is shown as missing.")]

    # 14 CBK
    s += [H1("14. Treasury blueprint: CBK DhowCSD, bonds, and T-bills")]
    s += [P("This section is an integration blueprint, not a live service. The Central Bank "
            "of Kenya's DhowCSD is a central securities depository and auction platform. "
            "Participation requires onboarding, a CSD account, licensed intermediaries, "
            "settlement arrangements, and CBK approval. No public API is assumed here.", "Warnx")]
    s += [H2("Target architecture")]
    s += [table([
        ["Layer", "Component", "Control"],
        ["Funding", "Client money account", "Segregated, reconciled daily, no commingling"],
        ["Allocation", "Treasury policy engine", "Board-approved limits per tenor and issuer"],
        ["Execution", "Licensed dealer / DhowCSD participant", "Orders placed by authorised staff or API"],
        ["Custody", "CSD / custodian account", "Dual approval, statement reconciliation"],
        ["Accounting", "Position and accrual ledger", "Amortised cost, accrued interest, duration"],
        ["Reporting", "Investor statements", "Holdings, coupons, maturities, fees, tax"],
    ], widths=[24 * mm, 58 * mm, 70 * mm])]
    s += [H2("Mechanism design")]
    s += bullets([
        "Collect funds into a segregated client account; never into an operating wallet.",
        "Record an allocation instruction with investor, amount, tenor, and mandate.",
        "A second authorised officer approves the instruction (maker-checker).",
        "The licensed participant subscribes at auction or buys in the secondary market.",
        "Settlement lands in the CSD account; the ledger books the position and accrual.",
        "Coupons and maturities are reconciled and distributed per the mandate.",
        "Every step writes an immutable audit record and a client statement.",
    ])
    s += [H2("Instruments")]
    s += [P("Treasury bills (91/182/364 day), fixed-coupon bonds, infrastructure bonds, "
            "and agency paper. Each has different tax treatment, tenor, liquidity, and "
            "eligibility. The system must store the prospectus terms, auction results, "
            "coupon schedule, and settlement date, not just a price.")]
    s += [H2("What cannot be shipped today")]
    s += bullets([
        "A CBK API integration without CBK onboarding and credentials.",
        "A bond marketplace without a licensed dealer or CSD participation.",
        "Automatic investment of client funds without custody and mandate agreements.",
        "Any representation that DhowCSD access exists in this build.",
    ])
    s += [P("The correct next step is a legal and treasury workstream: choose the licensed "
            "participant, obtain CSD/auction access, define the client-money model, and "
            "only then write the adapter. The software should record instructions and "
            "reconcile statements in the meantime; it must not pretend to settle.", "Warnx")]

    # 15 Broker
    s += [H1("15. Zero-dependency broker and trader blueprint")]
    s += [P("Creating a broker is a licensed activity, not a software feature. What can be "
            "built honestly is an internal OMS/EMS that routes orders to venues through "
            "adapters. MT5 becomes one adapter, not the architecture.", "Warnx")]
    s += [H2("Components")]
    s += [table([
        ["Component", "Function"],
        ["Order router", "Validates, risk-checks, and selects a venue adapter"],
        ["Adapter interface", "submit, cancel, status, positions, account, health"],
        ["MT5 adapter", "Existing bridge protocol (reference implementation)"],
        ["FIX adapter", "Equities/futures broker sessions (FIX 4.4/5.0)"],
        ["REST adapter", "Crypto exchanges and broker APIs"],
        ["Internal matching engine", "Only for a disclosed internal simulation, never labelled live"],
        ["Reconciler", "Compares local orders/fills with venue statements"],
        ["Risk engine", "Pre-trade limits, kill switch, exposure, throttle"],
    ], widths=[38 * mm, 114 * mm])]
    s += [H2("Why an internal matcher is not a live broker")]
    s += [P("Matching two clients against each other is operating a market. It requires "
            "licensing, capital, market surveillance, and settlement. An internal matcher "
            "is appropriate only for testing and must be labelled as such. Live execution "
            "requires a real venue, real custody, and real reconciliation.")]
    s += [H2("Adapter contract (TypeScript)")]
    s += [P("interface VenueAdapter {\n"
            "  submit(order: OrderRequest): Promise<VenueOrder>;\n"
            "  cancel(id: string): Promise<void>;\n"
            "  status(id: string): Promise<VenueOrder>;\n"
            "  positions(): Promise<VenuePosition[]>;\n"
            "  account(): Promise<VenueAccount>;\n"
            "  health(): Promise<{ ok: boolean; detail?: string }>;\n"
            "}", "Codex")]
    s += [P("The MT5 client in netlify/functions/lib/mt5.ts is the first adapter. A FIX "
            "or REST adapter can be added without changing the order ticket or the risk "
            "engine. That is the real path away from MT5 lock-in.")]

    # 16
    s += [H1("16. Strategic use cases and revenue engines")]
    s += [table([
        ["Use case", "Who pays", "Operational requirement"],
        ["Prop desk terminal", "Internal desk", "Live venue, risk limits, reconciliation"],
        ["Managed accounts", "Investors", "Licensing, custody, reporting, fees"],
        ["Research subscription", "Traders", "Data licensing, AI cost control"],
        ["Treasury ladder", "Corporates", "Licensed dealer, CSD, settlement"],
        ["CommCoin node hosting", "Operators", "Chain state, uptime, key custody"],
        ["Market-making", "Venue rebates", "Latency, inventory risk, surveillance"],
        ["Carry and basis", "Fund", "Financing, collateral, margin operations"],
        ["Education and simulation", "Retail", "Clear separation from live claims"],
    ], widths=[40 * mm, 34 * mm, 78 * mm])]
    s += [P("Every engine above is a risk engine before it is a revenue engine. Size limits, "
            "kill switches, cost modelling, and reconciliation are the product. A return "
            "curve without those is a brochure, not a business.")]

    # 17
    s += [H1("17. Roadmap, gaps, and go-live gates")]
    s += [H2("Go-live gates")]
    s += bullets([
        "Live venue: MT5 bridge reachable over HTTPS, funded account, test order reconciled.",
        "Chain: public chain service with stable URL, persisted state, and token auth.",
        "Data: licensed real-time feed if intraday execution is offered.",
        "Legal: entity, client agreements, risk disclosures, KYC/AML, sanctions screening.",
        "Security: external penetration test, secret rotation, incident response drill.",
        "Operations: on-call, backups tested, reconciliation sign-off, audit retention.",
    ])
    s += [H2("Known gaps")]
    s += bullets([
        "Hosted CommCoin is unconfigured (CHAIN_API_URL absent).",
        "Hosted live trading is unconfigured (MT5_BRIDGE_URL absent): orders are disabled.",
        "Market data is delayed daily history, not a real-time execution feed.",
        "CBK/DhowCSD and bond distribution are blueprints, not integrations.",
        "No regulated broker, custodian, or investment adviser status.",
    ])

    # 18
    s += [H1("18. Runbooks and verification commands")]
    s += [P("Health and auth", "H2x")]
    s += [P("curl -s https://mafutani.org/api/status\n"
            "curl -s -X POST https://mafutani.org/api/auth/login -H 'Content-Type: application/json' \\\n"
            "  -d '{\"email\":\"<admin>\",\"password\":\"<password>\"}'", "Codex")]
    s += [P("Paper order must be refused on hosted", "H2x")]
    s += [P("curl -i -X POST https://mafutani.org/api/orders \\\n"
            "  -H 'Content-Type: application/json' -H \"X-Session: $TOKEN\" \\\n"
            "  -d '{\"symbol\":\"AAPL\",\"side\":\"buy\",\"qty\":1,\"mode\":\"paper\"}'\n"
            "# expect HTTP 403, live only", "Codex")]
    s += [P("Pi node", "H2x")]
    s += [P("systemctl --user restart mafutani\n"
            "systemctl --user status mafutani\n"
            "curl -s http://127.0.0.1:8000/api/chain/wallet -H \"X-Session: $TOKEN\"", "Codex")]
    s += [P("News ingestion", "H2x")]
    s += [P("curl -s 'https://mafutani.org/api/news?symbol=AAPL&limit=5'", "Codex")]

    # Appendices
    s += [PageBreak(), H1("Appendix A. Page-by-page operator guide")]
    s += [table([
        ["Page", "What it does", "What to check"],
        ["Dashboard", "Cash, equity, P&amp;L, positions, all assets, orders, risk, AI brief", "Updated timestamp and feed status"],
        ["Screener", "Session change, RSI, trend, score, volume, spread, anomalies", "Sort and filter; click through to chart"],
        ["Chart", "Candles, EMA/VWAP/Bollinger, RSI, regime strip, position context", "TF label matches the served resolution"],
        ["Scalp Lab", "Per-strategy signal preview, backtest, AI review", "Stub-free methods; sample size"],
        ["Scalp Live", "Quote, chart, ticket, position, P&amp;L, flatten", "Mode is LIVE; bridge status"],
        ["Analysis", "Indicator intelligence and AI brief", "Data used is shown on the page"],
        ["Anomalies", "Price/volume/gap scanners", "Clean tape hidden unless requested"],
        ["Backtests", "Run and review stored backtests", "Coverage and significance verdict"],
        ["Portfolio", "Positions, fills, order ticket", "Mode, cash, risk limits"],
        ["Journal", "Trade notes by symbol and strategy", "Entries persist"],
        ["News", "Google News RSS, cached in Postgres", "Stale flag if feed fails"],
        ["Heatmap", "Volume treemap with session change", "Tile count equals universe"],
        ["Markets", "Venues and session windows", "Region sessions computed in UTC"],
        ["CommCoin", "Chain status, wallet, mining, transfers, off-ramp", "CHAIN_API_URL configured"],
        ["Security", "Session and 2FA status", "2FA unavailable is stated"],
        ["Policies", "Operating policy document", "Updated date"],
        ["Admin", "Users, tiers, sessions, symbols, readiness, themes, audit", "Bridge test and audit"],
        ["Investor", "Presentable account and system summary", "Snapshot export"],
    ], widths=[24 * mm, 70 * mm, 58 * mm])]

    s += [PageBreak(), H1("Appendix B. API surface used by the UI")]
    s += [table([
        ["Method", "Path", "Purpose"],
        ["POST", "/api/auth/login", "Session, CSRF, cookie"],
        ["POST", "/api/auth/signup", "Admin bootstrap or Free account"],
        ["GET", "/api/me/entitlements", "Tier and session probe"],
        ["GET", "/api/portfolio", "Cash, positions, fills, P&amp;L, CommCoin value"],
        ["GET/POST", "/api/orders", "List and place orders"],
        ["POST", "/api/positions/flatten", "Close all positions"],
        ["GET", "/api/screener", "Universe analytics"],
        ["GET", "/api/analysis", "Per-symbol intelligence"],
        ["GET", "/api/anomalies", "Scanner results"],
        ["GET", "/api/candles", "Bars by resolution"],
        ["GET", "/api/quote(s)", "Latest top-of-book"],
        ["GET", "/api/news", "RSS ingestion and cache"],
        ["POST", "/api/signals/preview", "Strategy signal"],
        ["POST", "/api/backtests", "Run and store"],
        ["POST", "/api/ai/analysis|strategy|brief", "AI Gateway calls"],
        ["GET/POST", "/api/admin/*", "Admin tier only"],
        ["GET", "/api/policies", "Policy document"],
        ["GET", "/stream/quotes", "Quote snapshot (legacy SSE)"],
        ["GET/POST", "/api/chain/*", "Proxied to the Python chain"],
    ], widths=[18 * mm, 62 * mm, 72 * mm])]

    s += [PageBreak(), H1("Appendix C. MT5 bridge protocol")]
    s += [P("The EA has no server socket. The bridge listens; the EA long-polls. "
            "The app posts a command and waits for the EA result.")]
    s += [table([
        ["Endpoint", "Caller", "Meaning"],
        ["GET /next", "MQL5 EA", "Long-poll for the next command"],
        ["POST /result", "MQL5 EA", "Return the execution result"],
        ["POST /exec", "App", "Submit a command and wait"],
        ["GET /health", "App/admin", "Bridge liveness"],
    ], widths=[30 * mm, 28 * mm, 94 * mm])]
    s += [P("Rules: one command at a time (the terminal trade context is single-threaded); "
            "commands older than 120s are dropped; a 504 means the order may or may not "
            "have executed; never retry blindly. The token is sent in X-Mafutani-Token; "
            "the EA uses a query parameter because MQL5 WebRequest cannot set both a body "
            "and headers.")]

    s += [PageBreak(), H1("Appendix D. Chain operations cookbook")]
    s += [P("Wallet creation", "H2x")]
    s += [P("POST /api/chain/wallet {\"confirm\":true}\n"
            "# returns address, pub_fingerprint, signatures_max", "Codex")]
    s += [P("Mining", "H2x")]
    s += [P("POST /api/chain/mine {\"rounds\":500000}\n"
            "# {ok,height,nonce,hash} or {ok:false,reason,next_nonce}", "Codex")]
    s += [P("Transfer", "H2x")]
    s += [P("POST /api/chain/send {\"to\":\"mft1...\",\"amount_mft\":\"1000\","
            "\"confirm\":true}", "Codex")]
    s += [P("Operational rules", "H2x")]
    s += bullets([
        "One authoritative wallet file per user; never restore a stale copy.",
        "Never reuse signature indexes; WOTS+ is stateful.",
        "Epoch seed mints 100B once per 182 days to the seed miner.",
        "Blocks after the seed have no block reward; distribute by transfer.",
        "Persist chain/mft.db on durable storage; back up the current state.",
        "Off-ramp acceptance is not settlement; reconcile Daraja callbacks.",
    ])

    s += [PageBreak(), H1("Appendix E. Treasury operations checklist (CBK blueprint)")]
    s += [table([
        ["Step", "Owner", "Evidence"],
        ["Client agreement and mandate", "Legal", "Signed agreement, risk disclosure"],
        ["Segregated client account", "Finance", "Bank confirmation, reconciliation"],
        ["CSD/DhowCSD onboarding", "Treasury", "Participant approval, account IDs"],
        ["Auction calendar and limits", "Treasury", "Board-approved mandate"],
        ["Allocation instruction", "Adviser", "Signed instruction, suitability"],
        ["Maker-checker approval", "Operations", "Two distinct approvers"],
        ["Subscription/settlement", "Dealer", "Contract note, CSD statement"],
        ["Booking and accrual", "Finance", "Ledger entry, coupon schedule"],
        ["Client statement", "Operations", "Holdings, fees, tax, maturity"],
        ["Periodic reconciliation", "Compliance", "Signed reconciliation pack"],
    ], widths=[46 * mm, 28 * mm, 78 * mm])]
    s += [P("No step in this checklist is automated by the current build. It is the "
            "control design an implementation must satisfy before client funds move.", "Warnx")]

    s += [PageBreak(), H1("Appendix F. Broker adapter implementation notes")]
    s += [P("Order router responsibilities", "H2x")]
    s += bullets([
        "Normalise symbols and sides across venues.",
        "Apply pre-trade risk once, before adapter dispatch.",
        "Attach a deterministic client_order_id for idempotency.",
        "Persist the venue order id and raw response.",
        "Reconcile status transitions; never infer a fill from acceptance.",
        "Expose cancel and replace with the same idempotency guarantees.",
    ])
    s += [P("Adapter checklist", "H2x")]
    s += bullets([
        "Timeouts shorter than the caller's deadline, with a reconcilable outcome.",
        "Auth failure, rejection, throttling, and venue outage distinguished.",
        "Positions and account equity fetched from the venue, not cached indefinitely.",
        "Health endpoint used by readiness and the admin test button.",
        "Secrets injected from environment; never logged.",
    ])
    s += [P("FIX notes", "H2x")]
    s += [P("Use a session per venue/environment, sequence-number persistence, heartbeat "
            "monitoring, and a drop-copy for reconciliation. Treat ExecReport as the "
            "source of truth for fills. Never run FIX over the public internet without TLS.")]

    s += [PageBreak(), H1("Appendix G. Control mapping for industry hardening")]
    s += [table([
        ["Control area", "Implemented", "Still required"],
        ["Access control", "Tier checks, sessions, admin gate", "SSO/MFA, periodic access review"],
        ["Change management", "Git history, audit on admin actions", "Formal CAB, release approvals"],
        ["Logging", "audit_log for auth/orders/admin", "Central SIEM, retention policy"],
        ["Data protection", "HTTPS, secrets in env", "Encryption at rest policy, DLP, residency"],
        ["Vendor risk", "Readiness checks", "Contracts, due diligence, exit plans"],
        ["Incident response", "Policy text, revoke sessions", "On-call rota, drills, notification SLAs"],
        ["Business continuity", "Neon PITR, Pi service", "Tested RTO/RPO, failover runbook"],
        ["Financial controls", "Risk limits, audit", "Reconciliation, segregation, external audit"],
        ["Model risk", "AI output labelled research", "Validation, monitoring, bias review"],
    ], widths=[34 * mm, 56 * mm, 62 * mm])]

    s += [PageBreak(), H1("Appendix H. Glossary")]
    s += [table([
        ["Term", "Meaning"],
        ["CommCoin / MFT", "Experimental chain token with epoch expiry/replacement"],
        ["DhowCSD", "Central Bank of Kenya central securities depository platform"],
        ["WOTS+", "Winternitz one-time signature scheme used by the chain"],
        ["Epoch seed", "100B MFT minted once per 182-day epoch"],
        ["MT5", "MetaTrader 5 terminal and trade server"],
        ["Bridge", "Operator-run HTTP service the EA polls"],
        ["PoW", "Proof of work over SHAKE256"],
        ["OMS/EMS", "Order/execution management system"],
        ["CSD", "Central securities depository"],
        ["PITR", "Point-in-time recovery"],
        ["Retcode 10009", "MT5 acceptance, not a fill"],
    ], widths=[38 * mm, 114 * mm])]

    s += [PageBreak(), H1("Appendix I. Verification matrix")]
    s += [table([
        ["Claim", "How to verify", "Expected"],
        ["Login works", "POST /api/auth/login", "200 + session_token + Set-Cookie"],
        ["Hosted rejects paper", "POST /api/orders mode=paper", "403 live-only"],
        ["Live is gated", "GET /api/portfolio", "live_trading_enabled false without bridge"],
        ["News is real", "GET /api/news?symbol=AAPL", "items with links and timestamps"],
        ["Screener is real", "GET /api/screener", "session change and volume from candles"],
        ["Backtests compute", "POST /api/backtests", "metrics include trades, maxDD, sharpe"],
        ["AI is wired", "POST /api/ai/analysis", "text from the gateway model"],
        ["Admin is gated", "GET /api/admin/overview as non-admin", "403"],
        ["Pi mines", "POST /api/chain/mine", "ok true with height/nonce/hash"],
        ["Pi funded", "GET /api/chain/wallet", "balance_mft > 0 after transfer"],
    ], widths=[38 * mm, 68 * mm, 46 * mm])]

    s += [PageBreak(), H1("Appendix J. Onboarding checklist")]
    s += bullets([
        "Create the operator entity and open segregated client money accounts.",
        "Obtain the required licences for the services actually offered.",
        "Provision Neon, Netlify, and the Pi node; set every environment variable.",
        "Deploy the MT5 bridge and verify /api/admin/live/test.",
        "Deploy the chain service and verify the CommCoin page.",
        "Load real market data and license any intraday feed used for execution.",
        "Run the verification matrix and attach the output to the launch record.",
        "Complete external security testing and a reconciliation dry run.",
        "Publish client agreements, risk disclosures, and the policy set.",
        "Train operators on order rejection, kill switch, incident response, and audit.",
    ])

    s += [Spacer(1, 8 * mm),
          P("End of textbook. This document is descriptive, not a solicitation or "
            "financial advice. Verify every operational claim against the running system "
            "before relying on it.", "Smallx")]

    doc.build(s)
    print("wrote", OUT)


if __name__ == "__main__":
    build()
