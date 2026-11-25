import type { Config } from "@netlify/functions";
import { timingSafeEqual } from "node:crypto";
import { Router } from "./lib/router";
import { json, error, readBody, getSessionToken, sessionCookie } from "./lib/http";
import { db } from "./lib/db";
import { SCHEMA } from "./lib/schema";
import { hashPassword, verifyPassword, newToken, newCsrf } from "./lib/authpw";
import { storage, BUCKET } from "./lib/storage";
import { chat, listModels, AI_BASE } from "./lib/ai";
import { authConfig, AUTH_BASE } from "./lib/auth";
import { listSymbols, getQuotes, getCandles, listExchanges, listMarkets, latestPrice, dailySeries, quoteMap } from "./lib/market";
import { listNews, refreshNews } from "./lib/news";
import { analyse, detectAnomalies, scoreSeries, annualisedVolPct, signalFor, type Candle } from "./lib/analytics";
import { currentUser, accountFor } from "./lib/session";
import { executeOrder, paperEnabled } from "./lib/trading";
import { runBacktest } from "./lib/backtest";
import { liveConfigured, liveDisabledReason } from "./lib/mt5";
import { policies } from "./lib/policies";
import { wikiArticles } from "./lib/wiki";
import { logJournal } from "./lib/journal";
import { fillOrderManually } from "./lib/manual";
import { alpacaConfigured } from "./lib/alpaca";
import { venueRegistry, routeLive } from "./lib/venues";
import { getSetting, getBool, getNumber, allSettings, setSettings } from "./lib/settings";
import { createCashMovement, listCashMovements, listAdminCashMovements, confirmCashMovement, rejectCashMovement, cashReconciliation, MPESA_C2C_NUMBER, MPESA_C2C_NAME } from "./lib/cash";

const router = new Router();

/**
 * Setup endpoints are one-time operator actions, not public routes. They are
 * callable only with the out-of-band ADMIN_SETUP_TOKEN; when the token is not
 * configured they are indistinguishable from a missing route.
 */
function setupAuthorized(req: Request): boolean {
  const expected = process.env.ADMIN_SETUP_TOKEN || "";
  if (!expected) return false;
  const supplied = req.headers.get("x-admin-token") || "";
  const a = Buffer.from(expected);
  const b = Buffer.from(supplied);
  return a.length === b.length && timingSafeEqual(a, b);
}

/** Bootstrap administrators, from GLOBAL_ADMIN_EMAILS (comma-separated). */
function globalAdminEmails(): string[] {
  const raw = process.env.GLOBAL_ADMIN_EMAILS || process.env.GLOBAL_ADMIN_EMAIL || "";
  return raw.split(",").map((e) => e.trim().toLowerCase()).filter(Boolean);
}


router.on("GET", "/api/status", async () => {
  const checks: Record<string, string> = {};
  try {
    await db()`SELECT 1`;
    checks.db = "up";
  } catch (e) {
    checks.db = "down: " + (e as Error).message;
  }
  checks.ai = AI_BASE ? "configured" : "missing";
  checks.auth = AUTH_BASE ? "configured" : "missing";
  checks.storage = process.env.AWS_ENDPOINT_URL_S3 ? "configured" : "missing";
  checks.realtime = process.env.NEON_REALTIME_URL ? "configured" : "missing";
  return json({ ok: checks.db === "up", checks });
});

router.on("POST", "/api/admin/init", async ({ req }) => {
  if (!setupAuthorized(req)) return error(404, "not found");
  try {
    const sql = db();
    const statements = SCHEMA.split(";").map((s: string) => s.trim()).filter((s: string) => s.length > 0);
    for (const stmt of statements) {
      await sql(stmt as any);
    }
    return json({ ok: true });
  } catch (e) {
    return error(500, (e as Error).message);
  }
});

router.on("POST", "/api/admin/seed", async ({ req }) => {
  if (!setupAuthorized(req)) return error(404, "not found");
  // Only operator-supplied bootstrap accounts. No shared demo passwords:
  // a known credential in source is a production account takeover.
  const accounts: { email: string; password: string; tier: string }[] = [];
  const globalAdminPassword = process.env.GLOBAL_ADMIN_PASSWORD || "";
  if (globalAdminPassword) {
    for (const email of globalAdminEmails()) {
      accounts.push({ email, password: globalAdminPassword, tier: "Admin" });
    }
  }
  const results: any[] = [];
  for (const a of accounts) {
    try {
      const hash = hashPassword(a.password);
      await db()`INSERT INTO users (email, password_hash, tier) VALUES (${a.email}, ${hash}, ${a.tier}) ON CONFLICT (email) DO UPDATE SET password_hash = ${hash}, tier = ${a.tier}`;
      const start = a.tier === "Admin" ? 100_000_000 : 100_000;
      await db()`INSERT INTO accounts (user_id, cash, starting_cash) SELECT id, ${start}, ${start} FROM users WHERE email = ${a.email} ON CONFLICT (user_id) DO NOTHING`;
      try { await db()`INSERT INTO risk_limits (user_id) SELECT id FROM users WHERE email = ${a.email} ON CONFLICT (user_id) DO NOTHING`; } catch (e) { /* table may not exist */ }
      results.push({ email: a.email, status: "ok" });
    } catch (e) {
      results.push({ email: a.email, status: "error", error: (e as Error).message });
    }
  }
  return json({ seeded: results });
});

router.on("POST", "/api/auth/signup", async ({ req }) => {
  try {
    const body = await readBody(req);
    const email = String(body.email || "").trim().toLowerCase();
    const password = String(body.password || "");
    if (!email || !password) return error(400, "email and password required");

    const isGlobalAdmin = globalAdminEmails().includes(email);
    // The bootstrap administrators use an operator-supplied credential; all
    // other accounts keep the 8-character floor.
    if (password.length < 8 && !isGlobalAdmin) return error(400, "password must be at least 8 characters");
    const tier = isGlobalAdmin ? "Admin" : "Free";

    const hash = hashPassword(password);
    const rows: any = await db()`
      INSERT INTO users (email, password_hash, tier) VALUES (${email}, ${hash}, ${tier})
      RETURNING id, email, tier
    `;
    const u = rows[0];
    const start = isGlobalAdmin ? 100_000_000 : 10_000;
    await db()`INSERT INTO accounts (user_id, cash, starting_cash) VALUES (${u.id}, ${start}, ${start}) ON CONFLICT (user_id) DO NOTHING`;
    try { await db()`INSERT INTO risk_limits (user_id) VALUES (${u.id}) ON CONFLICT (user_id) DO NOTHING`; } catch { /* optional */ }

    const token = newToken();
    const csrf = newCsrf();
    const expires = Math.floor(Date.now() / 1000) + 7 * 24 * 3600;
    await db()`INSERT INTO sessions (token, user_id, expires_at, csrf_token) VALUES (${token}, ${u.id}, ${expires}, ${csrf})`;
    return json(
      { token, session_token: token, csrf, csrf_token: csrf, user: { id: u.id, email: u.email, display_name: u.display_name, tier: u.tier } },
      201,
      { "Set-Cookie": sessionCookie(token, req) },
    );
  } catch (e) {
    const msg = (e as Error).message.includes("duplicate key") ? "email already registered" : (e as Error).message;
    return error(400, msg);
  }
});

router.on("POST", "/api/auth/login", async ({ req }) => {
  try {
    const body = await readBody(req);
    const email = String(body.email || "").trim().toLowerCase();
    const password = String(body.password || "");
    const rows: any = await db()`SELECT id, email, display_name, password_hash, tier, disabled FROM users WHERE email = ${email}`;
    if (rows.length === 0) return error(401, "invalid credentials");
    const u = rows[0];
    if (u.disabled) return error(403, "account disabled");
    if (!verifyPassword(password, u.password_hash)) return error(401, "invalid credentials");
    const token = newToken();
    const csrf = newCsrf();
    const expires = Math.floor(Date.now() / 1000) + 7 * 24 * 3600;
    await db()`INSERT INTO sessions (token, user_id, expires_at, csrf_token) VALUES (${token}, ${u.id}, ${expires}, ${csrf})`;
    await audit(u.id, "auth.login", u.tier, { email: u.email });
    return json(
      { token, session_token: token, csrf, csrf_token: csrf, user: { id: u.id, email: u.email, display_name: u.display_name, tier: u.tier } },
      200,
      { "Set-Cookie": sessionCookie(token, req) },
    );
  } catch (e) { return error(400, (e as Error).message); }
});

router.on("POST", "/api/auth/logout", async ({ req }) => {
  const token = getSessionToken(req);
  if (token) await db()`DELETE FROM sessions WHERE token = ${token}`;
  return json({ ok: true }, 200, { "Set-Cookie": sessionCookie("", req, 0) });
});

router.on("GET", "/api/storage/list", async () => {
  try {
    const { ListObjectsV2Command } = await import("@aws-sdk/client-s3");
    const res = await storage().send(new ListObjectsV2Command({ Bucket: BUCKET }));
    return json({ bucket: BUCKET, objects: res.Contents ?? [] });
  } catch (e) {
    return error(500, (e as Error).message);
  }
});

router.on("GET", "/api/ai/models", async () => {
  try {
    return json(await listModels());
  } catch (e) {
    return error(500, (e as Error).message);
  }
});

router.on("POST", "/api/ai/chat", async ({ req }) => {
  try {
    const body = await readBody(req);
    return json(await chat(body.messages ?? [], body.model));
  } catch (e) {
    return error(400, (e as Error).message);
  }
});

router.on("POST", "/api/ai/analysis", async ({ req }) => {
  const user = await currentUser(req);
  if (!user) return error(401, "unauthorized");
  try {
    const body = await readBody(req);
    const symbol = String(body.symbol || "").trim().toUpperCase();
    if (!symbol) return error(400, "symbol required");
    const sym: any = await db()`SELECT * FROM symbols WHERE symbol = ${symbol}`;
    if (!sym.length) return error(404, "unknown symbol");
    const candles = (await getCandles(db(), symbol, 250, "1d")) as Candle[];
    if (!candles.length) return error(409, "no stored candles for this symbol");
    const a = analyse(candles);
    const q = await latestPrice(db(), symbol);
    const pos: any = await db()`
      SELECT qty, avg_price, realized_pnl FROM positions WHERE user_id = ${user.id} AND symbol = ${symbol}
    `;
    const prompt = [
      "You are a risk-focused market analyst inside a paper-trading terminal.",
      "Use only the supplied data. Do not invent prices, news, or fundamentals.",
      "Return 4 short sections: Bias, Key evidence, Risks, Invalidation level.",
      `Symbol: ${symbol} (${sym[0].name}, ${sym[0].asset_type}, ${sym[0].exchange})`,
      `Last: ${q?.last ?? a.last}; bid: ${q?.bid}; ask: ${q?.ask}`,
      `Indicators: ${JSON.stringify(a)}`,
      `Recent closes: ${candles.slice(-20).map((c) => c.close).join(", ")}`,
      `Open position: ${pos.length ? JSON.stringify(pos[0]) : "flat"}`,
      `Account tier: ${user.tier}. Paper account cash: ${(await accountFor(user.id)).cash}.`,
      "Be concise. No disclaimers. If the data is insufficient, say so explicitly.",
    ].join("\n");
    const out: any = await chat([{ role: "user", content: prompt }], String(body.model || "claude-haiku-4-5"));
    const text = out?.choices?.[0]?.message?.content || out?.output_text || "";
    await logJournal(user.id, {
      kind: "ai_review", symbol, strategy: "analysis",
      note: text.slice(0, 2000) || "AI analysis returned no text",
      details: { model: body.model || "claude-haiku-4-5", analysis: a },
    });
    return json({ symbol, model: body.model || "claude-haiku-4-5", text, analysis: a });
  } catch (e) {
    return error(502, "AI gateway: " + (e as Error).message);
  }
});

router.on("POST", "/api/ai/brief", async ({ req }) => {
  const user = await currentUser(req);
  if (!user) return error(401, "unauthorized");
  try {
    const acct = await accountFor(user.id);
    const pos: any = await db()`
      SELECT p.symbol, p.qty, p.avg_price, COALESCE(q.last, q.bid, q.ask) AS last
      FROM positions p
      LEFT JOIN LATERAL (
        SELECT bid, ask, last FROM quotes_l1 WHERE symbol = p.symbol ORDER BY ts DESC LIMIT 1
      ) q ON TRUE
      WHERE p.user_id = ${user.id} AND p.qty <> 0
    `;
    const series = await dailySeries(db(), 10);
    const quotes = await quoteMap(db());
    const movers = [];
    for (const [symbol, candles] of series) {
      if (candles.length < 2) continue;
      const prev = candles[candles.length - 2].close;
      const last = candles[candles.length - 1].close;
      const q = quotes.get(symbol);
      movers.push({ symbol, pct: prev ? ((last - prev) / prev) * 100 : 0, last: q?.last ?? last });
    }
    movers.sort((a, b) => Math.abs(b.pct) - Math.abs(a.pct));
    const stocks = movers.slice(0, 8);
    const prompt = [
      "You are the trading desk's risk briefer. Use only the supplied data.",
      "Return 3 short sections: Book, Market, Actions. No disclaimers.",
      `Account: ${user.email} (${user.tier}); cash ${acct.cash.toFixed(2)}; positions ${JSON.stringify(pos)}.`,
      `Top movers (session %): ${JSON.stringify(stocks)}.`,
      "Flag concentration, missing data, and anything a risk officer should act on before the next session.",
    ].join("\n");
    const out: any = await chat([{ role: "user", content: prompt }], "claude-haiku-4-5");
    const text = out?.choices?.[0]?.message?.content || out?.output_text || "";
    await logJournal(user.id, {
      kind: "ai_review", strategy: "desk_brief",
      note: text.slice(0, 2000) || "AI desk brief returned no text",
      details: { model: "claude-haiku-4-5", movers: stocks, cash: acct.cash },
    });
    return json({ email: user.email, model: "claude-haiku-4-5", text, movers: stocks });
  } catch (e) {
    return error(502, "AI gateway: " + (e as Error).message);
  }
});

router.on("POST", "/api/ai/strategy", async ({ req }) => {
  const user = await currentUser(req);
  if (!user) return error(401, "unauthorized");
  try {
    const body = await readBody(req);
    const symbol = String(body.symbol || "").trim().toUpperCase();
    const strategy = String(body.strategy || "ema_trend").trim();
    if (!symbol) return error(400, "symbol required");
    const candles = (await getCandles(db(), symbol, 500, "1d")) as Candle[];
    if (!candles.length) return error(409, "no stored candles for this symbol");
    const sig = signalFor(strategy, candles, body.params || {});
    const bt = runBacktest(symbol, strategy, candles, body.params || {});
    const prompt = [
      "You are a quantitative reviewer. Critique this single-symbol strategy run.",
      "Use only the supplied figures. Do not invent results.",
      "Return 3 short sections: Verdict, Weaknesses, One concrete test to run next.",
      `Strategy: ${strategy} on ${symbol}`,
      `Signal now: ${JSON.stringify(sig)}`,
      `Backtest metrics: ${JSON.stringify(bt.metrics)}`,
      "State plainly whether the sample is too small or the edge is unproven.",
    ].join("\n");
    const out: any = await chat([{ role: "user", content: prompt }], String(body.model || "claude-haiku-4-5"));
    const text = out?.choices?.[0]?.message?.content || out?.output_text || "";
    await logJournal(user.id, {
      kind: "ai_review", symbol, strategy,
      note: text.slice(0, 2000) || "AI strategy review returned no text",
      details: { model: body.model || "claude-haiku-4-5", signal: sig, backtest: bt.metrics },
    });
    return json({ symbol, strategy, model: body.model || "claude-haiku-4-5", text, signal: sig, backtest: bt.metrics });
  } catch (e) {
    return error(502, "AI gateway: " + (e as Error).message);
  }
});

router.on("GET", "/api/auth/config", async () => json(authConfig()));

router.on("GET", "/api/venues", async ({ req, query }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  const symbol = (query.get("symbol") || "").trim().toUpperCase();
  return json({
    venues: venueRegistry().map((v) => ({ ...v, supports: undefined })),
    route: symbol ? (() => {
      const r = routeLive(symbol);
      return r ? { id: r.id, label: r.label } : null;
    })() : null,
    note: "Zero-dependency adapters. Adding a venue does not change the ticket, risk engine or journal.",
  });
});

router.on("GET", "/api/live/config", async ({ req }) => {
  const user = await currentUser(req);
  let tierEnabled = false;
  if (user) {
    const rows: any = await db()`
      SELECT t.live_enabled FROM users u JOIN tiers t ON t.name = u.tier WHERE u.id = ${user.id}
    `;
    tierEnabled = !!(rows[0] && Number(rows[0].live_enabled));
  }
  return json({
    venue: alpacaConfigured() ? "ALPACA" : "MT5",
    venues: { alpaca: alpacaConfigured(), mt5: liveConfigured() },
    configured: alpacaConfigured() || liveConfigured(),
    available: (alpacaConfigured() || liveConfigured()) && tierEnabled,
    tier_enabled: tierEnabled,
    reason: (alpacaConfigured() || liveConfigured())
      ? (tierEnabled ? "" : "live trading is not enabled for this tier")
      : "ALPACA_API_KEY/SECRET or MT5_BRIDGE_URL is not configured",
  });
});

router.on("GET", "/api/realtime/config", async () =>
  json({ url: process.env.NEON_REALTIME_URL || "", hasSecret: !!process.env.NEON_REALTIME_SECRET }),
);

router.on("GET", "/api/bases", async ({ query }) => {
  try {
    const region = (query.get("region") || "").toUpperCase();
    const type = (query.get("type") || "").toLowerCase();
    const symbol = (query.get("symbol") || "").toUpperCase();
    const rows: any = region && type
      ? await db()`SELECT * FROM consolidation_bases WHERE region = ${region} AND type = ${type} ORDER BY region, type, name`
      : region
        ? await db()`SELECT * FROM consolidation_bases WHERE region = ${region} ORDER BY type, name`
        : type
          ? await db()`SELECT * FROM consolidation_bases WHERE type = ${type} ORDER BY region, name`
          : await db()`SELECT * FROM consolidation_bases ORDER BY region, type, name`;
    const bases = rows.map((r: any) => ({
      ...r,
      related_symbols: JSON.parse(r.related_symbols || "[]"),
    })).filter((r: any) => !symbol || r.related_symbols.includes(symbol));
    const byRegion: Record<string, any[]> = {};
    const types: Record<string, number> = {};
    for (const b of bases) {
      (byRegion[b.region] = byRegion[b.region] || []).push(b);
      types[b.type] = (types[b.type] || 0) + 1;
    }
    return json({
      bases,
      by_region: byRegion,
      types,
      note: "Informational context only. Not legal title, registry, operating status, or an endorsement.",
    });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/symbols", async () => {
  try { return json({ symbols: await listSymbols(db()) }); }
  catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/quotes", async () => {
  try { return json({ quotes: await getQuotes(db()) }); }
  catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/markets", async () => {
  try {
    const rows: any = await listMarkets(db());
    const venues = rows.map((r: any) => ({
      code: r.code,
      name: r.name,
      type: r.type,
      asset_classes: String(r.asset_classes || "").split(",").filter(Boolean),
      region: r.region || "Global",
      currency: r.currency || "USD",
      tz: r.tz || "UTC",
    }));
    const byRegion = new Map<string, any>();
    for (const v of venues) {
      const key = v.region;
      if (!byRegion.has(key)) {
        byRegion.set(key, {
          code: key.toUpperCase().replace(/[^A-Z0-9]/g, "_"),
          name: key,
          tz: v.tz,
          open_utc: 0,
          close_utc: 24,
          open: new Date().getUTCDay() >= 1 && new Date().getUTCDay() <= 5,
          venues_tracked: 0,
          asset_classes: new Set<string>(),
        });
      }
      const r = byRegion.get(key)!;
      r.venues_tracked += 1;
      v.asset_classes.forEach((a: string) => r.asset_classes.add(a));
    }
    const SESSIONS: Record<string, { name: string; open: number; close: number }> = {
      AMER: { name: "Americas", open: 13.5, close: 20 },
      EUROPE: { name: "Europe", open: 7, close: 16.5 },
      MEA: { name: "Middle East & Africa", open: 5, close: 14 },
      APAC: { name: "Asia-Pacific", open: 0, close: 6.5 },
      GLOBAL: { name: "Global", open: 0, close: 24 },
    };
    const now = new Date();
    const hour = now.getUTCHours() + now.getUTCMinutes() / 60;
    const weekday = now.getUTCDay() >= 1 && now.getUTCDay() <= 5;
    const regions = Array.from(byRegion.values()).map((r) => {
      const s = SESSIONS[r.code] || { name: r.name, open: 0, close: 24 };
      return {
        ...r,
        name: s.name,
        open_utc: s.open,
        close_utc: s.close,
        open: s.close - s.open >= 24 ? true : (weekday && hour >= s.open && hour < s.close),
        asset_classes: Array.from(r.asset_classes),
      };
    });
    return json({ config_path: "exchanges table", regions, venues });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/exchanges", async () => {
  try { return json({ exchanges: await listExchanges(db()) }); }
  catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/candles", async ({ query }) => {
  try {
    const symbol = (query.get("symbol") || "").trim().toUpperCase();
    const tf = query.get("tf") === "1d" ? "1d" : "1m";
    const limit = Math.min(2000, Math.max(1, parseInt(query.get("limit") || "200", 10) || 200));
    if (!symbol) return error(400, "symbol required");
    let servedTf = tf;
    let candles = await getCandles(db(), symbol, limit, tf);
    // The chart defaults to 1m. A deployment that only carries daily history
    // should still render, and must say which resolution it actually served.
    if (!candles.length && tf === "1m") {
      candles = await getCandles(db(), symbol, limit, "1d");
      if (candles.length) servedTf = "1d";
    }
    return json({ symbol, tf: servedTf, requested_tf: tf, candles });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/quote", async ({ query }) => {
  try {
    const symbol = (query.get("symbol") || "").trim().toUpperCase();
    if (!symbol) return error(400, "symbol required");
    const rows: any = await db()`SELECT * FROM quotes_l1 WHERE symbol = ${symbol} ORDER BY ts DESC LIMIT 1`;
    const quote = rows[0] || null;
    if (!quote) return json({ quote: null });
    const spread_bps = quote.last ? Number((((quote.ask - quote.bid) / quote.last) * 10_000).toFixed(2)) : null;
    return json({ ...quote, quote, spread_bps });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/news", async ({ query }) => {
  try {
    const symbol = query.get("symbol") || undefined;
    const limit = Math.min(100, Math.max(1, parseInt(query.get("limit") || "20", 10) || 20));
    const result = await refreshNews(db(), symbol, limit);
    return json({ items: result.items, news: result.items, stale: result.stale, error: result.error || "" });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/cash/config", async () => json({
  rail: "mpesa_c2c",
  number: await getSetting("mpesa_c2c_number"),
  name: await getSetting("mpesa_c2c_name"),
  currency: "KES",
  approval_required: await getBool("cash_approval_required"),
  usd_kes_rate: await getNumber("usd_kes_rate"),
  note: "Send M-Pesa C2C to this number, then submit the transaction reference. An admin confirms receipt before cash is credited.",
}));

router.on("POST", "/api/cash/deposit", async ({ req }) => {
  const user = await currentUser(req);
  if (!user) return error(401, "unauthorized");
  try {
    const body = await readBody(req);
    const amount = Number(body.amount);
    if (!isFinite(amount) || amount <= 0) return error(400, "amount must be greater than 0");
    const ref = String(body.external_reference || body.reference || "").trim();
    if (!ref) return error(400, "M-Pesa transaction reference required");
    const m = await createCashMovement(user.id, {
      movement_type: "deposit",
      rail: "mpesa",
      amount,
      currency: "KES",
      external_reference: ref,
      idempotency_key: "deposit:" + user.id + ":" + ref,
      details: { number: MPESA_C2C_NUMBER, submitted_by: user.email },
    });
    const cashApproval = await getBool("cash_approval_required");
    let movement = m;
    if (!cashApproval) {
      const confirmed = await confirmCashMovement(m.id, 0);
      if (confirmed) movement = { ...m, status: "confirmed", cash_after: confirmed.cash };
    }
    await logJournal(user.id, {
      kind: "cash", symbol: "", strategy: cashApproval ? "deposit_request" : "deposit_auto_confirmed",
      note: `M-Pesa deposit ${amount} KES ref ${ref}`,
      ref_id: `cash:${m.id}`,
      details: { movement_id: m.id, amount, currency: "KES", reference: ref, status: movement.status },
    });
    return json({
      movement: movement,
      usd_amount: movement.usd_amount,
      instructions: `Send KES ${amount.toFixed(2)} via M-Pesa C2C to ${MPESA_C2C_NUMBER} (${MPESA_C2C_NAME}) and keep the transaction code ${ref}. An admin must confirm receipt before cash is credited.`,
    }, 201);
  } catch (e) { return error(400, (e as Error).message); }
});

router.on("GET", "/api/cash/movements", async ({ req, query }) => {
  const user = await currentUser(req);
  if (!user) return error(401, "unauthorized");
  const limit = parseInt(query.get("limit") || "50", 10) || 50;
  return json({ movements: await listCashMovements(user.id, limit), config: { number: MPESA_C2C_NUMBER, name: MPESA_C2C_NAME, currency: "KES" } });
});

router.on("GET", "/api/admin/cash", async ({ req, query }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  const status = (query.get("status") || "").trim();
  const limit = parseInt(query.get("limit") || "200", 10) || 200;
  return json({ movements: await listAdminCashMovements(status || undefined, limit) });
});

router.on("POST", "/api/admin/cash/:id/confirm", async ({ req, params }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  try {
    const id = parseInt(params.id, 10);
    if (!Number.isInteger(id)) return error(400, "invalid movement id");
    const m = await confirmCashMovement(id, gate.user!.id);
    if (!m) return error(409, "movement is not pending");
    await audit(gate.user!.id, "cash.confirm", gate.user!.tier, { movement_id: id, amount: m.amount, currency: m.currency });
    await logJournal(m.user_id, {
      kind: "cash", strategy: "deposit_confirmed",
      note: `M-Pesa deposit confirmed: ${m.amount} ${m.currency}`,
      ref_id: `cash:${id}`,
      details: { movement_id: id, amount: m.amount, currency: m.currency, confirmed_by: gate.user!.id, cash_after: m.cash },
    });
    return json({ movement: m });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("POST", "/api/admin/cash/:id/reject", async ({ req, params }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  try {
    const id = parseInt(params.id, 10);
    if (!Number.isInteger(id)) return error(400, "invalid movement id");
    const body = await readBody(req);
    const note = String(body.note || "").slice(0, 500);
    const m = await rejectCashMovement(id, gate.user!.id, note);
    if (!m) return error(409, "movement is not pending");
    await audit(gate.user!.id, "cash.reject", gate.user!.tier, { movement_id: id, note });
    await logJournal(m.user_id, {
      kind: "cash", strategy: "deposit_rejected",
      note: `M-Pesa deposit rejected${note ? ": " + note : ""}`,
      ref_id: `cash:${id}`,
      details: { movement_id: id, note, rejected_by: gate.user!.id },
    });
    return json({ movement: m });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/admin/cash/reconcile", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  return json(await cashReconciliation());
});

router.on("GET", "/api/portfolio", async ({ req }) => {
  try {
    const user = await currentUser(req);
    if (!user) return error(401, "unauthorized");
    const acct = await accountFor(user.id);
    const pos: any = await db()`
      SELECT p.symbol, p.qty, p.avg_price, p.realized_pnl, p.updated_at,
             q.bid, q.ask, COALESCE(q.last, q.bid, q.ask) AS last
      FROM positions p
      LEFT JOIN LATERAL (
        SELECT bid, ask, last FROM quotes_l1 WHERE symbol = p.symbol ORDER BY ts DESC LIMIT 1
      ) q ON TRUE
      WHERE p.user_id = ${user.id}
      ORDER BY p.symbol
    `;
    const fills: any = await db()`
      SELECT id, symbol, side, qty, price, fee, ts
      FROM fills WHERE user_id = ${user.id} ORDER BY ts DESC LIMIT 25
    `;
    let realized = 0;
    let unrealized = 0;
    let marketValue = 0;
    const positions = pos.map((p: any) => {
      const last = p.last == null ? null : Number(p.last);
      const qty = Number(p.qty);
      const avg = Number(p.avg_price);
      const upl = last == null ? 0 : (last - avg) * qty;
      realized += Number(p.realized_pnl) || 0;
      unrealized += upl;
      marketValue += last == null ? 0 : last * qty;
      return {
        symbol: p.symbol, qty, avg_price: avg, realized_pnl: Number(p.realized_pnl) || 0,
        last, market_value: last == null ? null : last * qty,
        unrealized_pnl: last == null ? null : upl,
        updated_at: p.updated_at,
      };
    });
    const tierLive: any = await db()`
      SELECT t.live_enabled FROM users u JOIN tiers t ON t.name = u.tier WHERE u.id = ${user.id}
    `;
    const manualVenue = await getBool("manual_venue_enabled");
    const anyLive = alpacaConfigured() || liveConfigured() || manualVenue;
    const liveAvailable = anyLive && !!(tierLive[0] && Number(tierLive[0].live_enabled));
    const depRows: any = await db()`
      SELECT COALESCE(SUM(CASE
        WHEN movement_type = 'deposit' THEN COALESCE(usd_amount, amount)
        WHEN movement_type = 'withdrawal' THEN -COALESCE(usd_amount, amount)
        ELSE 0 END), 0) AS net_deposits
      FROM cash_movements WHERE user_id = ${user.id} AND status = 'confirmed'
    `;
    const netDeposits = Number(depRows[0].net_deposits) || 0;

    // CommCoin value comes from the chain service, not from a hardcoded rate.
    // The chain exposes a reference value (metadata), so it is reported as a
    // reference valuation, never as a market price.
    let mft: any = { available: false, reason: "CommCoin chain service is not configured", balance_mft: null, reference_value_usd: null, value_usd: null };
    const chainBase = (process.env.CHAIN_API_URL || "").replace(/\/+$/, "");
    if (chainBase) {
      try {
        const headers: Record<string, string> = {};
        const token = getSessionToken(req);
        if (token) headers["X-Session"] = token;
        if (process.env.CHAIN_API_TOKEN) headers["X-Mafutani-Token"] = process.env.CHAIN_API_TOKEN;
        headers["X-Mafutani-User"] = user.email;
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 4000);
        const [infoRes, walletRes] = await Promise.all([
          fetch(chainBase + "/api/chain/info", { headers, signal: ctrl.signal }),
          fetch(chainBase + "/api/chain/wallet", { headers, signal: ctrl.signal }),
        ]);
        clearTimeout(timer);
        const info: any = infoRes.ok ? await infoRes.json() : {};
        const wallet: any = walletRes.ok ? await walletRes.json() : {};
        const balance = wallet && wallet.exists ? Number(wallet.balance_mft) : 0;
        const ref = info && info.reference_value_usd != null ? Number(info.reference_value_usd) : null;
        const mftPrice = await getNumber("mft_usd_price");
        mft = {
          available: true,
          balance_mft: isFinite(balance) ? balance : 0,
          reference_value_usd: isFinite(ref as number) ? ref : null,
          market_price_usd: mftPrice > 0 ? mftPrice : null,
          value_usd: mftPrice > 0 && isFinite(balance) ? Number((balance * mftPrice).toFixed(2)) : null,
          note: mftPrice > 0
            ? "operator-set MFT/USD price; not a market guarantee"
            : "no MFT/USD market price configured; reference metadata is not included in equity",
          coin_symbol: info.coin_symbol || "MFT",
        };
      } catch (e) {
        mft.reason = "CommCoin service unreachable: " + (e as Error).message;
      }
    }
    const commcoinValue = mft.value_usd == null ? 0 : Number(mft.value_usd);
    return json({
      cash: acct.cash,
      starting_cash: acct.starting_cash,
      equity: acct.cash + marketValue + commcoinValue,
      commcoin: mft,
      market_value_securities: Number(marketValue.toFixed(2)),
      market_value: Number((marketValue + commcoinValue).toFixed(2)),
      tier: user.tier,
      email: user.email,
      display_name: user.display_name || "",
      live_trading_enabled: liveAvailable,
      live_venue: liveAvailable ? (alpacaConfigured() ? "ALPACA" : (liveConfigured() ? "MT5" : "MANUAL")) : null,
      live_reason: liveAvailable
        ? (alpacaConfigured() || liveConfigured() ? "automated venue available" : "manual execution: admin approves and records the fill")
        : (anyLive ? "live trading is not enabled for this tier" : "ALPACA_API_KEY/SECRET or MT5_BRIDGE_URL is not configured"),
      paper_enabled: paperEnabled(),
      positions,
      fills,
      summary: (() => {
        const securitiesEquity = acct.cash + marketValue;
        const basis = Number(acct.starting_cash) || 0;
        const capital = basis + netDeposits;
        const totalPnl = securitiesEquity + commcoinValue - capital;
        const realizedPnl = securitiesEquity - capital - unrealized;
        return {
          starting_cash: basis,
          net_deposits: Number(netDeposits.toFixed(2)),
          capital_base: Number(capital.toFixed(2)),
          securities_equity: Number(securitiesEquity.toFixed(2)),
          realized_pnl: Number(realizedPnl.toFixed(2)),
          unrealized_pnl: Number(unrealized.toFixed(2)),
          total_pnl: Number(totalPnl.toFixed(2)),
          return_pct: capital ? Number(((totalPnl / capital) * 100).toFixed(4)) : null,
          market_value: Number((marketValue + commcoinValue).toFixed(2)),
          market_value_securities: Number(marketValue.toFixed(2)),
          commcoin_value: commcoinValue,
        };
      })(),
    });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/orders", async ({ req, query }) => {
  try {
    const user = await currentUser(req);
    if (!user) return error(401, "unauthorized");
    const limit = Math.min(200, Math.max(1, parseInt(query.get("limit") || "20", 10) || 20));
    const orders: any = await db()`
      SELECT * FROM orders WHERE user_id = ${user.id} ORDER BY created_at DESC LIMIT ${limit}
    `;
    return json({ orders, rows: orders });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("POST", "/api/orders", async ({ req }) => {
  const user = await currentUser(req);
  if (!user) return error(401, "unauthorized");
  try {
    const body = await readBody(req);
    const mode = String(body.mode || (paperEnabled() ? "paper" : "live")).toLowerCase();
    if (mode === "live" && !(await getBool("live_trading_enabled"))) {
      return error(403, "live trading is disabled by the administrator");
    }
    const approvalRequired = await getBool("order_approval_required");
    // Local paper bypasses the approval queue. Hosted live orders, and any
    // explicitly live order, require admin approval.
    if (mode !== "live" && paperEnabled()) {
      const result = await executeOrder(user.id, body);
      await audit(user.id, "order.place", user.tier, {
        symbol: body.symbol, side: body.side, qty: body.qty, mode: "paper",
      });
      const o: any = result.order || {};
      await logJournal(user.id, {
        kind: "trade",
        symbol: o.symbol || String(body.symbol || "").toUpperCase(),
        strategy: String(body.strategy || "manual"),
        note: `${o.side || body.side} ${o.qty || body.qty} ${o.symbol || body.symbol} ${o.status || ""}`.trim(),
        ref_id: o.id ? `order:${o.id}` : "",
        details: { order_id: o.id, side: o.side, qty: o.qty, price: (result as any).fill?.price ?? o.price, status: o.status, mode: "paper" },
      });
      return json(result, 201);
    }

    if (mode === "live" && !approvalRequired) {
      if (body.confirm !== true) return error(428, "live orders require confirm:true");
      const result: any = await executeOrder(user.id, { ...body, mode: "live", confirm: true });
      await audit(user.id, "order.place", user.tier, { symbol: body.symbol, side: body.side, qty: body.qty, mode: "live", auto: true });
      const auto: any = result.order || {};
      await logJournal(user.id, {
        kind: "trade", symbol: auto.symbol || String(body.symbol || "").toUpperCase(),
        strategy: String(body.strategy || "manual"),
        note: `live ${auto.side || body.side} ${auto.qty || body.qty} ${auto.symbol || body.symbol} ${auto.status || ""} (auto-approved)`.trim(),
        ref_id: auto.id ? `order:${auto.id}` : "",
        details: { order_id: auto.id, side: auto.side, qty: auto.qty, price: (result.fill && result.fill.price) || auto.price, status: auto.status, mode: "live", auto_approved: true },
      });
      return json(result, 201);
    }

    const symbol = String(body.symbol || "").trim().toUpperCase();
    const side = String(body.side || "").trim().toLowerCase();
    const type = String(body.type || "market").trim().toLowerCase();
    const qty = Number(body.qty);
    if (!symbol) return error(400, "symbol required");
    if (side !== "buy" && side !== "sell") return error(400, "side must be buy or sell");
    if (!isFinite(qty) || qty <= 0) return error(400, "qty must be greater than 0");
    if (type === "limit" && !(Number(body.price) > 0)) return error(400, "limit orders need a positive price");

    const quote = await latestPrice(db(), symbol);
    const refPrice = type === "limit" ? Number(body.price) : (quote ? (side === "buy" ? quote.ask : quote.bid) : Number(body.price || 0));
    const notional = qty * refPrice;
    const riskRows: any = await db()`
      SELECT max_position_qty, max_notional, kill_switch FROM risk_limits WHERE user_id = ${user.id}
    `;
    const risk = riskRows[0] || { max_position_qty: 100, max_notional: 100000, kill_switch: 0 };
    if (Number(risk.kill_switch)) return error(403, "kill switch is on");
    if (notional > Number(risk.max_notional)) {
      return error(403, `order notional ${notional.toFixed(2)} exceeds max_notional ${risk.max_notional}`);
    }
    const posRows: any = await db()`
      SELECT qty FROM positions WHERE user_id = ${user.id} AND symbol = ${symbol}
    `;
    const currentQty = Number(posRows[0]?.qty || 0);
    const resultingQty = currentQty + (side === "buy" ? qty : -qty);
    if (Math.abs(resultingQty) > Number(risk.max_position_qty)) {
      return error(403, `resulting position ${resultingQty} exceeds max_position_qty ${risk.max_position_qty}`);
    }
    const adminRows: any = await db()`SELECT COUNT(*)::int AS n FROM users WHERE tier = 'Admin' AND disabled = 0`;
    const adminCount = Number(adminRows[0].n) || 0;
    if (adminCount === 0) return error(503, "no active admin available to approve orders");
    const threshold = await getNumber("order_consensus_threshold");
    const large = notional >= threshold;
    const required = large ? adminCount : 1;

    const requestKey = `req-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    const rows: any = await db()`
      INSERT INTO orders (user_id, symbol, side, type, qty, price, mode, status,
                          client_order_id, required_approvals, approvals_count)
      VALUES (${user.id}, ${symbol}, ${side}, ${type}, ${qty}, ${refPrice || null}, 'live',
              'pending_approval', ${requestKey}, ${required}, 0)
      RETURNING *
    `;
    const request = rows[0];
    await audit(user.id, "order.request", user.tier, {
      order_id: request.id, symbol, side, qty, notional, required, large,
    });
    await logJournal(user.id, {
      kind: "trade_request", symbol, strategy: String(body.strategy || "manual"),
      note: `live ${side} ${qty} ${symbol} requested · ${required}/${adminCount} approval(s)`,
      ref_id: `order:${request.id}`,
      details: { order_id: request.id, side, qty, price: refPrice, notional, required, admin_count: adminCount, large, status: "pending_approval" },
    });
    return json({
      order: request,
      pending: true,
      required_approvals: required,
      admin_count: adminCount,
      large,
      message: large
        ? `Large move: ${required} of ${adminCount} admins must approve before execution.`
        : "Queued for admin approval.",
    }, 202);
  } catch (e) {
    const err = e as Error & { status?: number };
    return error(err.status || 400, err.message);
  }
});

router.on("GET", "/api/admin/orders/pending", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  const rows: any = await db()`
    SELECT o.*, u.email, u.display_name,
           COALESCE((
             SELECT json_agg(json_build_object(
               'admin_id', a.admin_user_id, 'email', au.email,
               'decision', a.decision, 'note', a.note, 'ts', a.created_at
             ) ORDER BY a.created_at)
             FROM order_approvals a JOIN users au ON au.id = a.admin_user_id
             WHERE a.order_id = o.id
           ), '[]'::json) AS approvals
    FROM orders o JOIN users u ON u.id = o.user_id
    WHERE o.status IN ('pending_approval', 'execution_failed')
    ORDER BY o.id DESC
    LIMIT 200
  `;
  return json({ orders: rows });
});

async function tryExecuteApproved(order: any, adminId: number, note: string) {
  try {
    const manualOnly = await getBool("manual_execution_only");
    const venue = routeLive(order.symbol);
    if (manualOnly || !venue) {
      await db()`UPDATE orders SET status = 'awaiting_manual_fill', approval_note = ${note || "manual execution required"} WHERE id = ${order.id}`;
      await logJournal(order.user_id, {
        kind: "trade_approval", symbol: order.symbol, strategy: "manual_fill_required",
        note: `approved request #${order.id}; no automated venue for ${order.symbol} — awaiting manual fill`,
        ref_id: `order:${order.id}`,
        details: { request_id: order.id, approved_by: adminId, status: "awaiting_manual_fill" },
      });
      return { ok: true, manual: true, order };
    }
    const result: any = await executeOrder(order.user_id, {
      symbol: order.symbol, side: order.side, type: order.type, qty: Number(order.qty),
      price: order.price == null ? null : Number(order.price),
      mode: "live", confirm: true, client_order_id: `approval:${order.id}`,
    });
    const executed: any = result.order || {};
    await db()`
      UPDATE orders SET status = 'approved', approval_note = ${note || ""},
        venue_order_id = ${executed.venue_order_id || ("exec:" + executed.id)}
      WHERE id = ${order.id}
    `;
    await logJournal(order.user_id, {
      kind: "trade", symbol: order.symbol, strategy: "admin_approved",
      note: `approved ${order.side} ${order.qty} ${order.symbol} → order #${executed.id} ${executed.status}`,
      ref_id: `order:${executed.id}`,
      details: { request_id: order.id, executed_order_id: executed.id, venue_order_id: executed.venue_order_id, status: executed.status, approved_by: adminId },
    });
    return { ok: true, executed };
  } catch (e) {
    const msg = (e as Error).message;
    await db()`UPDATE orders SET status = 'execution_failed', approval_note = ${msg} WHERE id = ${order.id}`;
    await logJournal(order.user_id, {
      kind: "trade_approval", symbol: order.symbol, strategy: "execution_failed",
      note: `approved request #${order.id} failed at venue: ${msg}`,
      ref_id: `order:${order.id}`,
      details: { request_id: order.id, error: msg, approved_by: adminId },
    });
    return { ok: false, error: msg };
  }
}

router.on("POST", "/api/admin/orders/:id/approve", async ({ req, params }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  try {
    const id = parseInt(params.id, 10);
    if (!Number.isInteger(id)) return error(400, "invalid order id");
    const body = await readBody(req);
    const note = String(body.note || "").slice(0, 500);
    const rows: any = await db()`SELECT * FROM orders WHERE id = ${id}`;
    if (!rows.length) return error(404, "order not found");
    const order = rows[0];
    if (!["pending_approval", "execution_failed"].includes(String(order.status))) {
      return error(409, "order is not awaiting approval");
    }
    const inserted: any = await db()`
      INSERT INTO order_approvals (order_id, admin_user_id, decision, note)
      VALUES (${id}, ${gate.user!.id}, 'approve', ${note})
      ON CONFLICT (order_id, admin_user_id) DO UPDATE SET decision = 'approve', note = EXCLUDED.note
      RETURNING *
    `;
    const countRows: any = await db()`
      SELECT COUNT(*)::int AS n FROM order_approvals WHERE order_id = ${id} AND decision = 'approve'
    `;
    const approved = Number(countRows[0].n);
    const required = Number(order.required_approvals) || 1;
    await db()`UPDATE orders SET approvals_count = ${approved} WHERE id = ${id}`;
    await logJournal(order.user_id, {
      kind: "trade_approval", symbol: order.symbol, strategy: "admin_approve",
      note: `admin approved ${approved}/${required} for ${order.side} ${order.qty} ${order.symbol}${note ? ": " + note : ""}`,
      ref_id: `order:${id}`,
      details: { request_id: id, approved, required, admin_id: gate.user!.id, note },
    });
    await audit(gate.user!.id, "order.approve", gate.user!.tier, { order_id: id, approved, required });
    if (approved < required) {
      return json({ order_id: id, approved, required, pending: true });
    }
    const result = await tryExecuteApproved(order, gate.user!.id, note);
    if (!result.ok) return error(502, result.error || "venue execution failed");
    return json({ order_id: id, approved, required, executed: result.executed });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("POST", "/api/admin/orders/:id/reject", async ({ req, params }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  try {
    const id = parseInt(params.id, 10);
    if (!Number.isInteger(id)) return error(400, "invalid order id");
    const body = await readBody(req);
    const note = String(body.note || "").slice(0, 500);
    const rows: any = await db()`SELECT * FROM orders WHERE id = ${id}`;
    if (!rows.length) return error(404, "order not found");
    const order = rows[0];
    if (!["pending_approval", "execution_failed"].includes(String(order.status))) {
      return error(409, "order is not awaiting approval");
    }
    await db()`
      INSERT INTO order_approvals (order_id, admin_user_id, decision, note)
      VALUES (${id}, ${gate.user!.id}, 'reject', ${note})
      ON CONFLICT (order_id, admin_user_id) DO UPDATE SET decision = 'reject', note = EXCLUDED.note
    `;
    await db()`UPDATE orders SET status = 'rejected', approval_note = ${note} WHERE id = ${id}`;
    await logJournal(order.user_id, {
      kind: "trade_approval", symbol: order.symbol, strategy: "admin_reject",
      note: `admin rejected ${order.side} ${order.qty} ${order.symbol}${note ? ": " + note : ""}`,
      ref_id: `order:${id}`,
      details: { request_id: id, admin_id: gate.user!.id, note },
    });
    await audit(gate.user!.id, "order.reject", gate.user!.tier, { order_id: id, note });
    return json({ order_id: id, status: "rejected" });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("POST", "/api/admin/orders/:id/retry", async ({ req, params }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  try {
    const id = parseInt(params.id, 10);
    if (!Number.isInteger(id)) return error(400, "invalid order id");
    const rows: any = await db()`SELECT * FROM orders WHERE id = ${id}`;
    if (!rows.length) return error(404, "order not found");
    const order = rows[0];
    if (String(order.status) !== "execution_failed") return error(409, "order is not in execution_failed state");
    const countRows: any = await db()`
      SELECT COUNT(*)::int AS n FROM order_approvals WHERE order_id = ${id} AND decision = 'approve'
    `;
    if (Number(countRows[0].n) < Number(order.required_approvals || 1)) {
      return error(409, "approval threshold not met");
    }
    const result = await tryExecuteApproved(order, gate.user!.id, "retry");
    if (!result.ok) return error(502, result.error || "venue execution failed");
    return json({ order_id: id, executed: result.executed });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("POST", "/api/admin/orders/:id/fill", async ({ req, params }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  try {
    const id = parseInt(params.id, 10);
    if (!Number.isInteger(id)) return error(400, "invalid order id");
    const body = await readBody(req);
    const price = Number(body.price);
    if (!isFinite(price) || price <= 0) return error(400, "fill price must be greater than 0");
    const result = await fillOrderManually(id, price, gate.user!.id);
    await audit(gate.user!.id, "order.manual_fill", gate.user!.tier, { order_id: id, price, qty: result.order.qty, symbol: result.order.symbol });
    await logJournal(result.order.user_id, {
      kind: "trade", symbol: result.order.symbol, strategy: "manual_fill",
      note: `manual fill ${result.order.side} ${result.order.qty} ${result.order.symbol} @ ${price}`,
      ref_id: `order:${id}`,
      details: { request_id: id, fill_id: result.fill.id, price, qty: result.order.qty, side: result.order.side, cash_after: result.cash_after, filled_by: gate.user!.id },
    });
    return json(result);
  } catch (e) { return error(400, (e as Error).message); }
});

router.on("POST", "/api/positions/flatten", async ({ req }) => {
  const user = await currentUser(req);
  if (!user) return error(401, "unauthorized");
  try {
    const positions: any = await db()`
      SELECT symbol, qty FROM positions WHERE user_id = ${user.id} AND qty <> 0
    `;
    const closed: any[] = [];
    const rejected: any[] = [];
    for (const p of positions) {
      const qty = Number(p.qty);
      try {
        const result = await executeOrder(user.id, {
          symbol: p.symbol,
          side: qty > 0 ? "sell" : "buy",
          type: "market",
          qty: Math.abs(qty),
          mode: "paper",
        });
        await logJournal(user.id, {
          kind: "trade", symbol: p.symbol, strategy: "flatten",
          note: `flatten ${qty > 0 ? "sell" : "buy"} ${Math.abs(qty)} ${p.symbol} ${result.order?.status || ""}`.trim(),
          ref_id: result.order?.id ? `order:${result.order.id}` : "",
          details: { order_id: result.order?.id, reason: "flatten_all", mode: "paper" },
        });
        closed.push({ symbol: p.symbol, order_id: result.order?.id });
      } catch (e) {
        rejected.push({ symbol: p.symbol, qty, reason: (e as Error).message, stage: "order" });
      }
    }
    return json({ count: closed.length, closed, rejected });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/screener", async ({ query }) => {
  try {
    const limit = Math.min(500, Math.max(1, parseInt(query.get("limit") || "200", 10) || 200));
    const minVol = parseFloat(query.get("min_vol") || "0") || 0;
    const maxSpread = (query.get("max_spread_bps") || query.get("max_spread"))
      ? parseFloat((query.get("max_spread_bps") || query.get("max_spread"))!)
      : null;
    const asset = (query.get("asset_type") || query.get("asset") || "").toLowerCase();
    const exchange = (query.get("exchange") || "").toUpperCase();
    const region = (query.get("region") || "").toUpperCase();

    const syms: any = await listSymbols(db());
    const series = await dailySeries(db(), 120);
    const quotes = await quoteMap(db());
    const exchangeRows: any = await db()`SELECT code, region FROM exchanges`;
    const exchangeRegion = new Map<string, string>(exchangeRows.map((e: any) => [e.code, String(e.region || "").toUpperCase()]));

    const results: any[] = [];
    for (const s of syms) {
      if (asset && s.asset_type !== asset) continue;
      if (exchange && String(s.exchange || "").toUpperCase() !== exchange) continue;
      if (region && exchangeRegion.get(s.exchange) !== region) continue;
      const candles = series.get(s.symbol);
      if (!candles || !candles.length) continue;
      const scored = scoreSeries(candles as Candle[]);
      if (scored.volume < minVol) continue;
      const a = analyse(candles as Candle[]);
      const q = quotes.get(s.symbol);
      const last = q?.last == null ? scored.last : Number(q.last);
      const spreadBps = q && last ? ((Number(q.ask) - Number(q.bid)) / last) * 10_000 : null;
      if (maxSpread != null && spreadBps != null && spreadBps > maxSpread) continue;
      results.push({
        symbol: s.symbol,
        name: s.name,
        asset_type: s.asset_type,
        exchange: s.exchange,
        currency: s.currency,
        last,
        change_pct: scored.change_pct,
        volume: scored.volume,
        closes: scored.closes,
        rsi14: a.rsi14,
        spread_bps: spreadBps == null ? null : Number(spreadBps.toFixed(2)),
        score: a.score,
        bias: a.bias,
        trend: a.trend,
        regime: a.regime,
        anomaly_score: a.anomaly_score,
        anomalies: a.anomalies,
        summary: a.reason,
        source: candles[0].source,
      });
    }
    results.sort((x, y) => (y.volume || 0) - (x.volume || 0));
    return json({ results: results.slice(0, limit), rows: results.slice(0, limit), count: results.length });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/strategies", async () => {
  try {
    const rows: any = await db()`SELECT * FROM strategies ORDER BY name`;
    return json({ strategies: rows });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/journal", async ({ req, query }) => {
  try {
    const user = await currentUser(req);
    if (!user) return error(401, "unauthorized");
    const kind = (query.get("kind") || "").trim();
    const symbol = (query.get("symbol") || "").trim().toUpperCase();
    const q = (query.get("q") || "").trim();
    const limit = Math.min(500, Math.max(1, parseInt(query.get("limit") || "200", 10) || 200));
    const rows: any = await db()`
      SELECT * FROM journal
      WHERE user_id = ${user.id}
        AND (${kind} = '' OR kind = ${kind})
        AND (${symbol} = '' OR symbol = ${symbol})
        AND (${q} = '' OR note ILIKE ${"%" + q + "%"} OR strategy ILIKE ${"%" + q + "%"})
      ORDER BY ts DESC, id DESC
      LIMIT ${limit}
    `;
    const counts: any = await db()`
      SELECT kind, COUNT(*)::int AS n FROM journal WHERE user_id = ${user.id} GROUP BY kind ORDER BY n DESC
    `;
    return json({
      entries: rows.map((r: any) => ({ ...r, details: JSON.parse(r.details_json || "{}") })),
      journal: rows,
      counts,
    });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("POST", "/api/journal", async ({ req }) => {
  try {
    const token = getSessionToken(req);
    const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
    if (sess.length === 0) return error(401, "unauthorized");
    const uid = sess[0].user_id;
    const body = await readBody(req);
    const rows: any = await db()`
      INSERT INTO journal (user_id, symbol, strategy, note, pnl_ref, kind, details_json)
      VALUES (${uid}, ${body.symbol || ''}, ${body.strategy || ''}, ${body.note || ''},
              ${body.pnl_ref ?? null}, 'note', '{}')
      RETURNING *
    `;
    return json({ entry: rows[0] }, 201);
  } catch (e) { return error(400, (e as Error).message); }
});

router.on("GET", "/api/risk", async ({ req }) => {
  try {
    const user = await currentUser(req);
    if (!user) return error(401, "unauthorized");
    const rows: any = await db()`SELECT * FROM risk_limits WHERE user_id = ${user.id}`;
    return json(rows[0] || {
      user_id: user.id,
      max_position_qty: 100,
      max_notional: 100000,
      max_daily_loss: 1000,
      max_trades_per_hour: 20,
      kill_switch: 0,
    });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/watchlists", async ({ req }) => {
  try {
    const token = getSessionToken(req);
    const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
    if (sess.length === 0) return error(401, "unauthorized");
    const uid = sess[0].user_id;
    const rows: any = await db()`SELECT * FROM watchlists WHERE user_id = ${uid}`;
    return json({ watchlists: rows });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/backtests", async ({ req }) => {
  try {
    const user = await currentUser(req);
    if (!user) return error(401, "unauthorized");
    const rows: any = await db()`SELECT * FROM backtests WHERE user_id = ${user.id} ORDER BY created_at DESC LIMIT 20`;
    return json({ backtests: rows });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("POST", "/api/backtests", async ({ req }) => {
  const user = await currentUser(req);
  if (!user) return error(401, "unauthorized");
  try {
    const body = await readBody(req);
    const symbol = String(body.symbol || "").trim().toUpperCase();
    const strategy = String(body.strategy || "ema_trend").trim();
    if (!symbol) return error(400, "symbol required");
    const candles = (await getCandles(db(), symbol, 3650, "1d")) as Candle[];
    if (!candles.length) return error(409, "no daily candles for this symbol");
    const result = runBacktest(symbol, strategy, candles, body.params || {});
    const rows: any = await db()`
      INSERT INTO backtests (user_id, symbol, params_json, from_ts, to_ts, metrics_json, equity_json, status)
      VALUES (${user.id}, ${symbol}, ${JSON.stringify(result.params)},
              ${candles[0].ts}, ${candles[candles.length - 1].ts},
              ${JSON.stringify(result.metrics)}, ${JSON.stringify(result.equity)}, 'done')
      RETURNING *
    `;
    const m: any = result.metrics;
    await logJournal(user.id, {
      kind: "backtest",
      symbol,
      strategy,
      note: `backtest ${strategy} ${symbol}: return ${m.total_return_pct}% · Sharpe ${m.sharpe} · ${m.trades} trades · ${m.significance?.verdict || "unproven"}`,
      ref_id: `backtest:${rows[0].id}`,
      details: { metrics: result.metrics, params: result.params, coverage: m.coverage },
    });
    return json({
      id: rows[0].id,
      backtest: rows[0],
      metrics: result.metrics,
      equity: result.equity,
      params: result.params,
      ...result.metrics,
    }, 201);
  } catch (e) {
    const err = e as Error & { status?: number };
    return error(err.status || 400, err.message);
  }
});

router.on("GET", "/api/backtests/:id", async ({ req, params }) => {
  try {
    const user = await currentUser(req);
    if (!user) return error(401, "unauthorized");
    const id = parseInt(params.id, 10);
    const rows: any = await db()`
      SELECT * FROM backtests WHERE id = ${id} AND user_id = ${user.id}
    `;
    if (!rows.length) return error(404, "backtest not found");
    const r = rows[0];
    return json({
      ...r,
      metrics: JSON.parse(r.metrics_json || "{}"),
      equity: JSON.parse(r.equity_json || "[]"),
      params: JSON.parse(r.params_json || "{}"),
    });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/universe", async () => {
  try {
    const syms: any = await listSymbols(db());
    const series = await dailySeries(db(), 120);
    const quotes = await quoteMap(db());
    const exchangeRows: any = await db()`SELECT code, region FROM exchanges`;
    const exRegion = new Map<string, string>(exchangeRows.map((e: any) => [e.code, String(e.region || "GLOBAL")]));
    const rows: any[] = [];
    for (const s of syms) {
      const candles = series.get(s.symbol);
      if (!candles || candles.length < 20) continue;
      const a = analyse(candles as Candle[]);
      const scored = scoreSeries(candles as Candle[]);
      const q = quotes.get(s.symbol);
      const last = q?.last == null ? scored.last : Number(q.last);
      const prev = candles[candles.length - 2]?.close;
      const chg = prev && last ? ((last - prev) / prev) * 100 : null;
      const ema21 = (() => {
        const closes = candles.map((c) => c.close);
        const k = 2 / 22;
        let e = closes[0];
        for (let i = 1; i < closes.length; i++) e = closes[i] * k + e * (1 - k);
        return e;
      })();
      const lastTs = Number(candles[candles.length - 1].ts);
      rows.push({
        symbol: s.symbol, name: s.name, asset_type: s.asset_type,
        exchange: s.exchange, region: exRegion.get(s.exchange) || "GLOBAL",
        last, change_pct: chg, rsi14: a.rsi14, trend: a.trend, score: a.score,
        vol_ann_pct: a.vol_ann_pct, anomaly_score: a.anomaly_score,
        above_ema21: last != null && last > ema21,
        volume: scored.volume, bars: candles.length,
        last_date: new Date(lastTs * 1000).toISOString().slice(0, 10),
        last_ts: lastTs,
      });
    }
    const changes = rows.map((r) => r.change_pct).filter((x) => x != null) as number[];
    const rsis = rows.map((r) => r.rsi14).filter((x) => x != null) as number[];
    const vols = rows.map((r) => r.vol_ann_pct).filter((x) => x != null) as number[];
    const avg = (arr: number[]) => arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : null;
    const median = (arr: number[]) => {
      if (!arr.length) return null;
      const s = arr.slice().sort((a, b) => a - b);
      return s[Math.floor(s.length / 2)];
    };
    const sd = (arr: number[]) => {
      if (arr.length < 2) return null;
      const m = avg(arr)!;
      return Math.sqrt(arr.reduce((a, b) => a + (b - m) ** 2, 0) / arr.length);
    };
    const byClass: Record<string, any> = {};
    for (const r of rows) {
      const k = r.asset_type || "other";
      const b = byClass[k] = byClass[k] || { count: 0, up: 0, down: 0, changes: [] as number[], rsis: [] as number[] };
      b.count += 1;
      if (r.change_pct > 0) b.up += 1; else if (r.change_pct < 0) b.down += 1;
      if (r.change_pct != null) b.changes.push(r.change_pct);
      if (r.rsi14 != null) b.rsis.push(r.rsi14);
    }
    const classes = Object.keys(byClass).map((k) => ({
      asset_type: k, count: byClass[k].count, up: byClass[k].up, down: byClass[k].down,
      avg_change_pct: avg(byClass[k].changes), avg_rsi: avg(byClass[k].rsis),
    }));
    const sorted = rows.filter((r) => r.change_pct != null).sort((a, b) => b.change_pct - a.change_pct);
    const up = rows.filter((r) => (r.change_pct || 0) > 0).length;
    const down = rows.filter((r) => (r.change_pct || 0) < 0).length;
    const byRegion: Record<string, any> = {};
    for (const r of rows) {
      const k = r.region || "GLOBAL";
      const b = byRegion[k] = byRegion[k] || { count: 0, up: 0, down: 0, changes: [] as number[] };
      b.count += 1;
      if (r.change_pct > 0) b.up += 1; else if (r.change_pct < 0) b.down += 1;
      if (r.change_pct != null) b.changes.push(r.change_pct);
    }
    const regions = Object.keys(byRegion).sort().map((k) => ({
      region: k, count: byRegion[k].count, up: byRegion[k].up, down: byRegion[k].down,
      avg_change_pct: avg(byRegion[k].changes) == null ? null : Number(avg(byRegion[k].changes)!.toFixed(2)),
    }));
    const dates = rows.map((r) => r.last_ts).filter((x) => x > 0).sort((a, b) => a - b);
    const latest = dates.length ? new Date(dates[dates.length - 1] * 1000).toISOString().slice(0, 10) : null;
    const earliest = dates.length ? new Date(dates[0] * 1000).toISOString().slice(0, 10) : null;
    const today = new Date().toISOString().slice(0, 10);
    const coverage = {
      instruments: syms.length,
      with_data: rows.length,
      missing_data: Math.max(0, syms.length - rows.length),
      earliest_last_bar: earliest,
      latest_last_bar: latest,
      bars_window: 120,
      source: "Yahoo Finance daily history",
      delayed: true,
      partial_last_bar: latest === today,
      note: latest === today
        ? "The latest bar may be today's incomplete session. Changes are last stored bar vs prior bar, not a unified global close."
        : "Daily bars; not a real-time feed.",
    };
    return json({
      coverage,
      regions,
      as_of: new Date().toISOString(),
      scanned: rows.length,
      breadth: {
        up, down, flat: rows.length - up - down,
        advance_decline: down ? Number((up / down).toFixed(2)) : null,
        avg_change_pct: avg(changes) == null ? null : Number(avg(changes)!.toFixed(2)),
        median_change_pct: median(changes) == null ? null : Number(median(changes)!.toFixed(2)),
        dispersion_pct: sd(changes) == null ? null : Number(sd(changes)!.toFixed(2)),
        avg_rsi: avg(rsis) == null ? null : Number(avg(rsis)!.toFixed(1)),
        overbought: rows.filter((r) => (r.rsi14 || 0) >= 70).length,
        oversold: rows.filter((r) => (r.rsi14 || 0) <= 30).length,
        above_ema21: rows.filter((r) => r.above_ema21).length,
        avg_vol_pct: avg(vols) == null ? null : Number(avg(vols)!.toFixed(1)),
      },
      classes,
      gainers: sorted.slice(0, 8),
      losers: sorted.slice(-8).reverse(),
      instruments: rows.sort((a, b) => a.symbol.localeCompare(b.symbol)),
      regime: (avg(changes) || 0) > 0 && up > down ? "risk-on" : (avg(changes) || 0) < 0 && down > up ? "risk-off" : "mixed",
    });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/analysis", async ({ query }) => {
  try {
    const symbol = (query.get("symbol") || "").trim().toUpperCase();
    if (!symbol) return error(400, "symbol required");
    const sym: any = await db()`SELECT * FROM symbols WHERE symbol = ${symbol}`;
    if (!sym.length) return error(404, "unknown symbol");
    let candles = (await getCandles(db(), symbol, 250, "1d")) as Candle[];
    if (!candles.length) candles = (await getCandles(db(), symbol, 250, "1m")) as Candle[];
    const analysis = analyse(candles);
    const ex: any = await db()`SELECT * FROM exchanges WHERE code = ${sym[0].exchange}`;
    return json({
      symbol: sym[0],
      exchange: ex[0] || null,
      analysis,
      bars: candles.length,
    });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/anomalies", async ({ query }) => {
  try {
    const showAll = query.get("all") !== "0";
    const syms: any = await listSymbols(db());
    const series = await dailySeries(db(), 120);
    const results: any[] = [];
    const bySeverity: Record<string, number> = { high: 0, medium: 0, low: 0 };
    const kinds: Record<string, number> = {};
    for (const s of syms) {
      const candles = series.get(s.symbol);
      if (!candles || !candles.length) continue;
      const a = analyse(candles as Candle[]);
      for (const an of a.anomalies) {
        bySeverity[an.severity] = (bySeverity[an.severity] || 0) + 1;
        kinds[an.kind] = (kinds[an.kind] || 0) + 1;
      }
      if (!showAll && a.anomaly_score <= 0) continue;
      const lastTs = Number(candles[candles.length - 1].ts);
      results.push({
        symbol: s.symbol, name: s.name, asset_type: s.asset_type,
        last: a.last, score: a.score, bias: a.bias, regime: a.regime,
        anomaly_score: a.anomaly_score, anomalies: a.anomalies,
        bars: candles.length,
        last_date: new Date(lastTs * 1000).toISOString().slice(0, 10),
        window_bars: 30,
      });
    }
    results.sort((x, y) => y.anomaly_score - x.anomaly_score);
    const flagged = results.filter((r) => r.anomaly_score > 0).length;
    return json({
      scanned: syms.length,
      analysed: results.length,
      listed: results.length,
      flagged,
      clean: Math.max(0, syms.length - flagged),
      by_severity: bySeverity,
      kinds,
      window_bars: 30,
      as_of: new Date().toISOString(),
      results,
    });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/chart_context", async ({ query }) => {
  try {
    const symbol = (query.get("symbol") || "").trim().toUpperCase();
    const tf = query.get("tf") === "1d" ? "1d" : "1m";
    const limit = Math.min(1000, Math.max(20, parseInt(query.get("limit") || "200", 10) || 200));
    if (!symbol) return error(400, "symbol required");
    const candles = (await getCandles(db(), symbol, limit, tf)) as Candle[];
    if (!candles.length) return json({ symbol, tf, series: [], regime: {}, resolution: {} });

    const rets: number[] = [0];
    for (let i = 1; i < candles.length; i++) {
      const p = candles[i - 1].close;
      rets.push(p > 0 ? Math.log(candles[i].close / p) : 0);
    }
    const WINDOW = 10;
    const vol: number[] = [];
    for (let i = 0; i < rets.length; i++) {
      const w = rets.slice(Math.max(0, i - WINDOW + 1), i + 1);
      const mean = w.reduce((a, b) => a + b, 0) / w.length;
      const sd = Math.sqrt(w.reduce((a, b) => a + (b - mean) ** 2, 0) / w.length);
      vol.push(sd);
    }
    const order = vol.slice().sort((a, b) => a - b);
    const q = (p: number) => order[Math.min(order.length - 1, Math.max(0, Math.floor(p * order.length)))] || 0;
    const expansion = q(0.95);
    const compression = q(0.05);
    const meanVol = vol.reduce((a, b) => a + b, 0) / vol.length;
    const sdVol = Math.sqrt(vol.reduce((a, b) => a + (b - meanVol) ** 2, 0) / vol.length) || 1;

    const series = candles.map((c, i) => {
      const z = (vol[i] - meanVol) / sdVol;
      const anomaly = Math.abs(z) >= 3;
      return {
        ts: c.ts,
        state: vol[i] >= expansion ? "expansion" : vol[i] <= compression ? "compression" : null,
        anomaly,
        severity: anomaly ? (Math.abs(z) >= 4 ? "high" : "medium") : null,
      };
    });

    const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
    const sd = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / rets.length) || 1;
    const kurtosis = rets.reduce((a, b) => a + ((b - mean) / sd) ** 4, 0) / rets.length - 3;

    return json({
      symbol,
      tf,
      series,
      regime: { basis: "estimated", expansion, compression },
      resolution: { label: tf === "1d" ? "1d" : "1m", regime_window_bars: WINDOW },
      cut_z: 3,
      kurtosis,
    });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/chart_position", async ({ req, query }) => {
  try {
    const symbol = (query.get("symbol") || "").trim().toUpperCase();
    if (!symbol) return error(400, "symbol required");
    const user = await currentUser(req);
    let position = null;
    let unrealized = null;
    if (user) {
      const rows: any = await db()`
        SELECT symbol, qty, avg_price, realized_pnl FROM positions WHERE user_id = ${user.id} AND symbol = ${symbol}
      `;
      const q = await latestPrice(db(), symbol);
      if (rows.length) {
        position = rows[0];
        if (q) unrealized = (q.last - Number(rows[0].avg_price)) * Number(rows[0].qty);
      }
      const sym: any = await db()`SELECT currency FROM symbols WHERE symbol = ${symbol}`;
      return json({
        symbol,
        position,
        unrealized_pnl: unrealized == null ? null : Number(unrealized.toFixed(2)),
        currency: sym[0]?.currency || "",
        round_trip_cost_bps: q && q.last ? Number((((q.ask - q.bid) / q.last) * 10_000).toFixed(2)) : null,
      });
    }
    return json({ symbol, position: null, unrealized_pnl: null, currency: "", round_trip_cost_bps: null });
  } catch (e) { return error(500, (e as Error).message); }
});

// --- auxiliary endpoints ---

router.on("GET", "/api/i18n", async () => json({ locales: ["en"] }));
router.on("GET", "/api/i18n/messages", async ({ query }) => json({ locale: query.get("locale") || "en", messages: {} }));
router.on("GET", "/api/me/entitlements", async ({ req }) => {
  const token = getSessionToken(req);
  const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
  if (sess.length === 0) return error(401, "unauthorized");
  const u: any = await db()`SELECT tier, email, display_name FROM users WHERE id = ${sess[0].user_id}`;
  return json({ tier: u[0]?.tier || "Free", email: u[0]?.email || "", display_name: u[0]?.display_name || "", entitlements: {} });
});
router.on("GET", "/api/policies", async () => json({ ...policies(), wiki: wikiArticles() }));
router.on("GET", "/api/kyc/me", async ({ req }) => {
  const token = getSessionToken(req);
  const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
  if (sess.length === 0) return error(401, "unauthorized");
  return json({ kyc: { status: "none" } });
});
router.on("POST", "/api/ux/event", async ({ req }) => {
  try {
    const user = await currentUser(req);
    if (!user) return new Response(null, { status: 204 });
    const body = await readBody(req);
    const event = String(body.event || "event").slice(0, 80);
    const page = String(body.page || "").slice(0, 120);
    const symbol = String(body.symbol || "").slice(0, 32).toUpperCase();
    const detail = body.detail && typeof body.detail === "object" ? body.detail : {};
    const note = String(body.note || "").slice(0, 1000);
    await logJournal(user.id, {
      kind: "ux",
      symbol,
      strategy: event,
      note: note || `${event}${page ? " @ " + page : ""}`,
      details: { page, ...detail },
    });
    return new Response(null, { status: 204 });
  } catch {
    return new Response(null, { status: 204 });
  }
});

router.on("POST", "/api/terminal/command", async ({ req }) => {
  try {
    const body = await readBody(req);
    const raw = String(body.cmd || "").trim();
    if (!raw) return json({ ok: false, output: "empty command" });
    const parts = raw.split(/\s+/);
    const cmd = parts[0].toLowerCase();
    const args = parts.slice(1);
    const user = await currentUser(req);

    if (cmd === "help") {
      return json({
        ok: true,
        output: [
          "commands: help | status | screener | universe | markets | anomalies",
          "quote SYMBOL | analyze SYMBOL | news [SYMBOL]",
          "positions | orders | cash | buy SYMBOL QTY [limit PRICE] | sell SYMBOL QTY [limit PRICE]",
          "backtest STRATEGY SYMBOL | clear",
        ].join("\n"),
      });
    }
    if (cmd === "clear") return json({ ok: true, output: "", data: { clear: true } });
    if (cmd === "status") {
      const checks: any = await db()`SELECT 1 AS ok`;
      return json({ ok: true, output: `db=${checks.length ? "up" : "down"} tier=${user?.tier || "anonymous"} live=${liveConfigured() ? "configured" : "unavailable"}`, data: {} });
    }
    if (cmd === "screener") {
      const syms: any = await listSymbols(db());
      const series = await dailySeries(db(), 120);
      const leaders = syms.map((s: any) => {
        const c = series.get(s.symbol) || [];
        return { symbol: s.symbol, vol: c.reduce((a, x) => a + x.volume, 0) };
      }).sort((a: any, b: any) => b.vol - a.vol).slice(0, 12);
      return json({ ok: true, output: leaders.map((l: any) => `${l.symbol} vol ${Math.round(l.vol).toLocaleString()}`).join("\n"), data: { leaders } });
    }
    if (cmd === "news") {
      const symbol = (args[0] || "").toUpperCase() || undefined;
      const n = await refreshNews(db(), symbol, 8);
      return json({
        ok: true,
        output: n.items.length
          ? n.items.map((x) => `${x.title} (${x.source})`).join("\n")
          : (n.error ? `news feed error: ${n.error}` : "no headlines"),
        data: { items: n.items, stale: n.stale },
      });
    }
    if (cmd === "analyze" || cmd === "analysis") {
      const symbol = (args[0] || "").toUpperCase();
      if (!symbol) return json({ ok: false, output: "usage: analyze SYMBOL" });
      const candles = (await getCandles(db(), symbol, 250, "1d")) as Candle[];
      if (!candles.length) return json({ ok: false, output: `no candles for ${symbol}` });
      const a = analyse(candles);
      return json({
        ok: true,
        output: `${symbol} bias ${a.bias} score ${a.score} rsi ${a.rsi14 ?? "—"} trend ${a.trend} vol ${a.vol_ann_pct ?? "—"}%\n${a.reason}`,
        data: a,
      });
    }
    if (cmd === "anomalies") {
      const syms: any = await listSymbols(db());
      const series = await dailySeries(db(), 120);
      const hit: string[] = [];
      for (const s of syms) {
        const c = series.get(s.symbol);
        if (!c || !c.length) continue;
        const a = analyse(c as Candle[]);
        if (a.anomaly_score > 0) {
          hit.push(`${s.symbol} score ${a.anomaly_score}: ` + a.anomalies.map((x) => x.kind).join(", "));
        }
      }
      return json({ ok: true, output: hit.length ? hit.join("\n") : "no flagged instruments", data: { flagged: hit.length } });
    }
    if (cmd === "universe") {
      const syms: any = await listSymbols(db());
      const series = await dailySeries(db(), 120);
      let up = 0, down = 0;
      for (const s of syms) {
        const c = series.get(s.symbol);
        if (!c || c.length < 2) continue;
        const chg = c[c.length - 1].close - c[c.length - 2].close;
        if (chg > 0) up++; else if (chg < 0) down++;
      }
      return json({ ok: true, output: `${syms.length} instruments · ${up} up / ${down} down / ${syms.length - up - down} flat`, data: { scanned: syms.length, up, down } });
    }
    if (cmd === "markets") {
      const venues = await listMarkets(db());
      return json({ ok: true, output: venues.map((v: any) => `${v.code} ${v.region} ${v.currency}`).join("\n"), data: { venues } });
    }
    if (cmd === "quote") {
      const symbol = (args[0] || "").toUpperCase();
      if (!symbol) return json({ ok: false, output: "usage: quote SYMBOL" });
      const q = await latestPrice(db(), symbol);
      if (!q) return json({ ok: false, output: `no quote for ${symbol}` });
      return json({ ok: true, output: `${symbol} bid ${q.bid} ask ${q.ask} last ${q.last}`, data: q });
    }
    if (cmd === "cash" || cmd === "positions" || cmd === "orders") {
      if (!user) return json({ ok: false, output: "login required" });
      if (cmd === "cash") {
        const acct = await accountFor(user.id);
        return json({ ok: true, output: `cash ${acct.cash.toFixed(2)} (starting ${acct.starting_cash.toFixed(2)})`, data: acct });
      }
      if (cmd === "positions") {
        const rows: any = await db()`SELECT symbol, qty, avg_price, realized_pnl FROM positions WHERE user_id = ${user.id} AND qty <> 0`;
        return json({ ok: true, output: rows.length ? rows.map((p: any) => `${p.symbol} ${p.qty} @ ${p.avg_price}`).join("\n") : "flat", data: { positions: rows } });
      }
      const rows: any = await db()`SELECT id, symbol, side, qty, status, mode FROM orders WHERE user_id = ${user.id} ORDER BY id DESC LIMIT 15`;
      return json({ ok: true, output: rows.length ? rows.map((o: any) => `#${o.id} ${o.mode} ${o.side} ${o.qty} ${o.symbol} ${o.status}`).join("\n") : "no orders", data: { orders: rows } });
    }
    if (cmd === "buy" || cmd === "sell") {
      if (!user) return json({ ok: false, output: "login required" });
      const symbol = (args[0] || "").toUpperCase();
      const qty = parseFloat(args[1] || "0");
      if (!symbol || !isFinite(qty) || qty <= 0) return json({ ok: false, output: "usage: buy|sell SYMBOL QTY [limit PRICE]" });
      const isLimit = (args[2] || "").toLowerCase() === "limit";
      const price = isLimit ? parseFloat(args[3] || "0") : null;
      try {
        const result: any = await executeOrder(user.id, {
          symbol, side: cmd, qty, type: isLimit ? "limit" : "market",
          price, mode: "paper",
        });
        await logJournal(user.id, {
          kind: "trade", symbol, strategy: "terminal",
          note: `terminal ${cmd} ${qty} ${symbol} ${result.order.status}`,
          ref_id: result.order.id ? `order:${result.order.id}` : "",
          details: { order_id: result.order.id, source: "terminal", mode: "paper" },
        });
        return json({ ok: true, output: `order #${result.order.id} ${result.order.status} ${cmd} ${qty} ${symbol}${result.resting ? " (resting)" : ""}`, data: result });
      } catch (e) {
        return json({ ok: false, output: (e as Error).message });
      }
    }
    if (cmd === "backtest") {
      if (!user) return json({ ok: false, output: "login required" });
      const strategy = args[0] || "ema_trend";
      const symbol = (args[1] || "").toUpperCase();
      if (!symbol) return json({ ok: false, output: "usage: backtest STRATEGY SYMBOL" });
      const candles = (await getCandles(db(), symbol, 3650, "1d")) as Candle[];
      if (!candles.length) return json({ ok: false, output: `no daily candles for ${symbol}` });
      const r = runBacktest(symbol, strategy, candles, {});
      const m: any = r.metrics;
      return json({
        ok: true,
        output: `${strategy} ${symbol}: return ${m.total_return_pct}% dd ${m.max_drawdown_pct}% sharpe ${m.sharpe} trades ${m.trades} (${m.significance.verdict})`,
        data: r,
      });
    }
    return json({ ok: false, output: `unknown command: ${cmd}. type 'help'` });
  } catch (e) {
    return json({ ok: false, output: (e as Error).message });
  }
});

router.on("GET", "/api/allocation", async ({ req }) => {
  try {
    const user = await currentUser(req);
    if (!user) return error(401, "unauthorized");
    const acct = await accountFor(user.id);
    const pos: any = await db()`
      SELECT p.symbol, p.qty, COALESCE(q.last, q.bid, q.ask) AS last
      FROM positions p
      LEFT JOIN LATERAL (
        SELECT bid, ask, last FROM quotes_l1 WHERE symbol = p.symbol ORDER BY ts DESC LIMIT 1
      ) q ON TRUE
      WHERE p.user_id = ${user.id} AND p.qty <> 0
    `;
    const buckets = pos.map((p: any) => ({
      symbol: p.symbol, value: p.last == null ? 0 : Number(p.last) * Number(p.qty),
    }));
    const invested = buckets.reduce((a: number, b: any) => a + Math.max(0, b.value), 0);
    const total = invested + acct.cash;
    return json({
      cash: acct.cash,
      invested,
      total,
      allocation: [
        { symbol: "CASH", weight_pct: total ? Number(((acct.cash / total) * 100).toFixed(2)) : 0 },
        ...buckets.map((b: any) => ({
          symbol: b.symbol,
          weight_pct: total ? Number(((b.value / total) * 100).toFixed(2)) : 0,
        })),
      ],
    });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/arbitrage", async () => error(501, "arbitrage scanning is not configured on this deployment"));
router.on("GET", "/api/arbitrage/venues", async () => error(501, "arbitrage scanning is not configured on this deployment"));
router.on("GET", "/api/cot", async () => error(501, "COT ingestion is not configured on this deployment"));
router.on("GET", "/api/cot/contract", async () => error(501, "COT ingestion is not configured on this deployment"));
router.on("GET", "/api/cot/history", async () => error(501, "COT ingestion is not configured on this deployment"));
router.on("POST", "/api/cot/sync", async () => error(501, "COT ingestion is not configured on this deployment"));
router.on("GET", "/api/macro", async () => error(501, "macro ingestion is not configured on this deployment"));
router.on("GET", "/api/macro/series", async () => error(501, "macro ingestion is not configured on this deployment"));
router.on("POST", "/api/macro/fetch", async () => error(501, "macro ingestion is not configured on this deployment"));
router.on("GET", "/api/agents", async () => error(501, "agent execution is not configured on this deployment"));
router.on("GET", "/api/agents/tick", async () => error(501, "agent execution is not configured on this deployment"));

router.on("GET", "/api/auth/2fa", async ({ req }) => {
  const user = await currentUser(req);
  if (!user) return error(401, "unauthorized");
  const rows: any = await db()`SELECT totp_enabled FROM users WHERE id = ${user.id}`;
  return json({ enabled: !!rows[0]?.totp_enabled });
});
router.on("POST", "/api/auth/2fa/setup", async () => error(501, "TOTP enrolment is not available in this build"));
router.on("POST", "/api/auth/2fa/enable", async () => error(501, "TOTP enrolment is not available in this build"));
router.on("POST", "/api/auth/2fa/disable", async () => error(501, "TOTP enrolment is not available in this build"));

router.on("POST", "/api/signals/preview", async ({ req }) => {
  try {
    const body = await readBody(req);
    const symbol = String(body.symbol || "").trim().toUpperCase();
    const strategy = String(body.strategy || "ema_trend").trim();
    if (!symbol) return error(400, "symbol required");
    let candles = (await getCandles(db(), symbol, 500, "1d")) as Candle[];
    if (!candles.length) candles = (await getCandles(db(), symbol, 500, "1m")) as Candle[];
    if (!candles.length) return error(409, "no candles for this symbol");
    const q = await latestPrice(db(), symbol);
    if (strategy === "book_imbalance") {
      const rows: any = await db()`
        SELECT bid, ask, bid_size, ask_size, last FROM quotes_l1
        WHERE symbol = ${symbol} ORDER BY ts DESC LIMIT 1
      `;
      const book = rows[0];
      if (!book) return error(409, "no quote for this symbol");
      const b = Number(book.bid_size) || 0;
      const a = Number(book.ask_size) || 0;
      const total = b + a;
      const ratio = total > 0 ? (b - a) / total : 0;
      const threshold = Number(body.params?.threshold ?? 0.15);
      const signal = ratio > threshold ? "buy" : ratio < -threshold ? "sell" : "flat";
      return json({
        signal,
        reason: `top-of-book imbalance ${(ratio * 100).toFixed(1)}% (bid ${b} / ask ${a}); threshold ±${(threshold * 100).toFixed(0)}%`,
        last: book.last == null ? null : Number(book.last),
        bid: Number(book.bid), ask: Number(book.ask),
        spread_bps: q && q.last ? Number((((q.ask - q.bid) / q.last) * 10_000).toFixed(2)) : null,
        bars: candles.length, source: "engine", symbol, strategy,
      });
    }
    if (strategy === "news_momentum") {
      const news: any = symbol
        ? await db()`SELECT title, source, ts FROM news WHERE symbol = ${symbol} ORDER BY ts DESC LIMIT 20`
        : await db()`SELECT title, source, ts FROM news ORDER BY ts DESC LIMIT 20`;
      if (!news.length) {
        return json({
          signal: "flat",
          reason: "no stored headlines for this instrument; news ingestion is not running",
          last: q?.last ?? null, bars: candles.length, source: "engine", symbol, strategy,
        });
      }
      const positive = /\b(beat|beats|surge|record|upgrade|growth|profit|wins|rally|bullish)\b/i;
      const negative = /\b(miss|misses|plunge|downgrade|loss|lawsuit|probe|bearish|fraud|cut)\b/i;
      let score = 0;
      for (const n of news) {
        if (positive.test(n.title)) score += 1;
        if (negative.test(n.title)) score -= 1;
      }
      return json({
        signal: score > 0 ? "buy" : score < 0 ? "sell" : "flat",
        reason: `${news.length} headlines, sentiment score ${score}`,
        last: q?.last ?? null, bars: candles.length, source: "engine", symbol, strategy,
      });
    }
    const sig = signalFor(strategy, candles, body.params || {});
    return json({
      ...sig,
      symbol,
      strategy,
      bid: q?.bid ?? null,
      ask: q?.ask ?? null,
      spread_bps: q && q.last ? Number((((q.ask - q.bid) / q.last) * 10_000).toFixed(2)) : null,
    });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("POST", "/api/backtests/validate", async ({ req }) => {
  try {
    const body = await readBody(req);
    const symbol = String(body.symbol || "").trim().toUpperCase();
    const strategy = String(body.strategy || "");
    const errors: string[] = [];
    if (!symbol) errors.push("symbol required");
    if (!strategy) errors.push("strategy required");
    const rows: any = symbol ? await db()`SELECT 1 FROM symbols WHERE symbol = ${symbol} AND enabled = 1` : [];
    if (symbol && !rows.length) errors.push("unknown symbol");
    return json({ valid: errors.length === 0, errors });
  } catch (e) { return error(400, (e as Error).message); }
});

async function audit(actorUserId: number | null, action: string, tier = "", details: Record<string, unknown> = {}) {
  try {
    await db()`
      INSERT INTO audit_log (actor_user_id, action, tier, paper_live, details_json)
      VALUES (${actorUserId}, ${action}, ${tier}, 'paper', ${JSON.stringify(details)})
    `;
  } catch { /* audit must not break the request */ }
}

async function adminUser(req: Request) {
  const user = await currentUser(req);
  if (!user) return { error: error(401, "unauthorized") };
  if (user.tier !== "Admin") return { error: error(403, "admin tier required") };
  return { user };
}

router.on("GET", "/api/admin/settings", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  return json({ settings: await allSettings() });
});

router.on("POST", "/api/admin/settings", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  try {
    const body = await readBody(req);
    const values = body.settings && typeof body.settings === "object" ? body.settings : body;
    const saved = await setSettings(values, gate.user!.id);
    await audit(gate.user!.id, "admin.settings", gate.user!.tier, { keys: Object.keys(values) });
    return json({ settings: saved });
  } catch (e) { return error(400, (e as Error).message); }
});

router.on("GET", "/api/admin/overview", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  const [users, accounts, symbols, candles, orders, sessions, equity, deposits]: any[] = await Promise.all([
    db()`SELECT COUNT(*)::int AS n FROM users`,
    db()`SELECT COUNT(*)::int AS n, COALESCE(SUM(cash), 0) AS cash, COALESCE(SUM(starting_cash), 0) AS starting_cash FROM accounts`,
    db()`SELECT COUNT(*)::int AS n FROM symbols WHERE enabled = 1`,
    db()`SELECT COUNT(*)::int AS n FROM candles_1d`,
    db()`SELECT COUNT(*)::int AS n FROM orders`,
    db()`SELECT COUNT(*)::int AS n FROM sessions WHERE expires_at > ${Math.floor(Date.now() / 1000)}`,
    db()`
      SELECT COALESCE(SUM(COALESCE(a.cash, 0) + COALESCE(mv.value, 0)), 0) AS equity
      FROM accounts a
      LEFT JOIN (
        SELECT p.user_id, SUM(p.qty * COALESCE(q.last, q.bid, q.ask)) AS value
        FROM positions p
        LEFT JOIN LATERAL (
          SELECT bid, ask, last FROM quotes_l1 WHERE symbol = p.symbol ORDER BY ts DESC LIMIT 1
        ) q ON TRUE
        WHERE p.qty <> 0
        GROUP BY p.user_id
      ) mv ON mv.user_id = a.user_id
    `,
    db()`
      SELECT COALESCE(SUM(CASE
        WHEN movement_type = 'deposit' THEN COALESCE(usd_amount, amount)
        WHEN movement_type = 'withdrawal' THEN -COALESCE(usd_amount, amount)
        ELSE 0 END), 0) AS net_deposits
      FROM cash_movements WHERE status = 'confirmed'
    `,
  ]);
  return json({
    overview: {
      users: users[0].n,
      accounts: accounts[0].n,
      cash: Number(accounts[0].cash),
      starting_cash: Number(accounts[0].starting_cash),
      equity: Number(equity[0].equity),
      net_deposits: Number(deposits[0].net_deposits),
      total_pnl: Number((Number(equity[0].equity) - Number(accounts[0].starting_cash) - Number(deposits[0].net_deposits)).toFixed(2)),
      symbols: symbols[0].n,
      candles_daily: candles[0].n,
      orders: orders[0].n,
      active_sessions: sessions[0].n,
    },
  });
});

router.on("GET", "/api/admin/readiness", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  const checks: Record<string, unknown> = {};
  try { await db()`SELECT 1`; checks.db = "up"; } catch (e) { checks.db = "down: " + (e as Error).message; }
  checks.ai_gateway = process.env.NEON_AI_GATEWAY_TOKEN ? "configured" : "missing";
  checks.auth = process.env.NEON_AUTH_BASE_URL ? "configured" : "missing";
  checks.storage = process.env.AWS_ENDPOINT_URL_S3 ? "configured" : "missing";
  checks.realtime = process.env.NEON_REALTIME_URL ? "configured" : "missing";
  return json({ ready: checks.db === "up", checks });
});

router.on("GET", "/api/admin/audit", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  const rows: any = await db()`SELECT * FROM audit_log ORDER BY ts DESC LIMIT 100`;
  return json({ audit: rows });
});

router.on("GET", "/api/admin/gaps", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  const gaps = [
    { id: "live-bridge", area: "Execution", severity: "blocker", status: liveConfigured() ? "configured" : "open", owner: "Operations", detail: liveConfigured() ? "MT5 bridge configured" : "MT5_BRIDGE_URL / MT5_BRIDGE_TOKEN missing" },
    { id: "chain-service", area: "CommCoin", severity: "blocker", status: process.env.CHAIN_API_URL ? "configured" : "open", owner: "Operations", detail: process.env.CHAIN_API_URL ? "chain service configured" : "CHAIN_API_URL missing; hosted CommCoin unavailable" },
    { id: "market-data", area: "Data", severity: "high", status: "mitigated", owner: "Data", detail: "Delayed daily Yahoo history; not an execution feed" },
    { id: "cbk", area: "Treasury", severity: "blocker", status: "open", owner: "Legal/Treasury", detail: "CBK DhowCSD, bonds and T-bills require onboarding and licensing" },
    { id: "broker-licence", area: "Brokerage", severity: "blocker", status: "open", owner: "Legal", detail: "Internal matcher is not a licensed broker; venue adapters are the path" },
    { id: "pen-test", area: "Security", severity: "high", status: "open", owner: "Security", detail: "External penetration test not completed" },
    { id: "2fa", area: "Security", severity: "medium", status: "open", owner: "Engineering", detail: "TOTP enrolment endpoint returns 501 in the Netlify port" },
    { id: "rate-limit", area: "Security", severity: "medium", status: "mitigated", owner: "Engineering", detail: "Edge limiter is per-isolate; add a shared store for global limits" },
    { id: "kyc", area: "Compliance", severity: "high", status: "open", owner: "Compliance", detail: "KYC/AML hooks are workflow plumbing, not certification" },
    { id: "demo-accounts", area: "Access", severity: "high", status: "closed", owner: "Engineering", detail: "Shared demo accounts removed from hosted seed path and database" },
    { id: "paper-hosted", area: "Policy", severity: "high", status: "closed", owner: "Engineering", detail: "Hosted paper orders return 403; local remains paper" },
  ];
  return json({ gaps, blockers: gaps.filter((g) => g.status === "open" && g.severity === "blocker").length });
});

router.on("POST", "/api/admin/users", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  try {
    const body = await readBody(req);
    const email = String(body.email || "").trim().toLowerCase();
    const name = String(body.display_name || "").trim().slice(0, 80);
    const tier = String(body.tier || "Free");
    const password = String(body.password || "");
    if (!email || !email.includes("@")) return error(400, "valid email required");
    if (password.length < 8) return error(400, "password must be at least 8 characters");
    const allowed: any = await db()`SELECT name FROM tiers`;
    if (!allowed.some((r: any) => r.name === tier)) return error(400, "unknown tier");
    const hash = hashPassword(password);
    const rows: any = await db()`
      INSERT INTO users (email, display_name, password_hash, tier)
      VALUES (${email}, ${name}, ${hash}, ${tier})
      RETURNING id, email, display_name, tier
    `;
    const u = rows[0];
    const start = tier === "Admin" ? 100_000_000 : 100_000;
    await db()`INSERT INTO accounts (user_id, cash, starting_cash) VALUES (${u.id}, ${start}, ${start}) ON CONFLICT (user_id) DO NOTHING`;
    try { await db()`INSERT INTO risk_limits (user_id) VALUES (${u.id}) ON CONFLICT (user_id) DO NOTHING`; } catch { /* optional */ }
    await audit(gate.user!.id, "admin.user_create", gate.user!.tier, { target: u.id, email, tier });
    await logJournal(gate.user!.id, {
      kind: "admin", strategy: "user_create",
      note: `created ${u.email} (${tier})`,
      ref_id: `user:${u.id}`,
      details: { user_id: u.id, email: u.email, display_name: name, tier },
    });
    return json({ user: u }, 201);
  } catch (e) {
    const msg = (e as Error).message.includes("duplicate key") ? "email already registered" : (e as Error).message;
    return error(400, msg);
  }
});

router.on("GET", "/api/admin/users", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  const rows: any = await db()`
    SELECT u.id, u.email, u.display_name, u.tier, u.disabled, u.created_at, u.last_login_ts,
           COALESCE(a.cash, 0) AS cash,
           COALESCE(a.cash, 0) + COALESCE((
             SELECT SUM(p.qty * COALESCE(q.last, q.bid, q.ask))
             FROM positions p
             LEFT JOIN LATERAL (
               SELECT bid, ask, last FROM quotes_l1 WHERE symbol = p.symbol ORDER BY ts DESC LIMIT 1
             ) q ON TRUE
             WHERE p.user_id = u.id AND p.qty <> 0
           ), 0) AS equity
    FROM users u LEFT JOIN accounts a ON a.user_id = u.id
    ORDER BY u.id
  `;
  return json({ users: rows });
});

router.on("GET", "/api/admin/symbols", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  return json({ symbols: await listSymbols(db()) });
});

router.on("GET", "/api/admin/hardening", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  const checks: any = {};
  try { await db()`SELECT 1`; checks.db = true; } catch { checks.db = false; }
  const controls = [
    { id: "mode-live-only", area: "Execution", status: paperEnabled() ? "fail" : "pass", evidence: paperEnabled() ? "TRADING_MODE allows paper" : "hosted paper orders rejected", action: "Set TRADING_MODE=live" },
    { id: "setup-token", area: "Access", status: process.env.ADMIN_SETUP_TOKEN ? "pass" : "fail", evidence: process.env.ADMIN_SETUP_TOKEN ? "ADMIN_SETUP_TOKEN set" : "setup token missing", action: "Set ADMIN_SETUP_TOKEN" },
    { id: "bootstrap-admins", area: "Access", status: (process.env.GLOBAL_ADMIN_EMAILS && process.env.GLOBAL_ADMIN_PASSWORD) ? "pass" : "fail", evidence: "bootstrap env", action: "Set GLOBAL_ADMIN_EMAILS and GLOBAL_ADMIN_PASSWORD" },
    { id: "session-revoke", area: "Access", status: "pass", evidence: "Admin -> Revoke sessions; /api/admin/users/:id/revoke", action: "none" },
    { id: "admin-gate", area: "Access", status: "pass", evidence: "all /api/admin/* require Admin tier", action: "none" },
    { id: "audit-log", area: "Logging", status: checks.db ? "pass" : "fail", evidence: "audit_log + journal", action: "verify retention and export" },
    { id: "edge-rate-limit", area: "Availability", status: "pass", evidence: "edge function /api/* 300 req/min per isolate", action: "move to a shared store for global limits" },
    { id: "security-headers", area: "Transport", status: "pass", evidence: "CSP, HSTS, frame-deny, nosniff, referrer policy", action: "verify with curl -I" },
    { id: "tls", area: "Transport", status: "manual", evidence: "Netlify managed TLS; chain/bridge must be HTTPS", action: "verify certificate and bridge URL" },
    { id: "two-factor", area: "Access", status: "fail", evidence: "TOTP enrolment returns 501 in the Netlify port", action: "implement TOTP or SSO" },
    { id: "pen-test", area: "Assurance", status: "manual", evidence: "no external penetration test recorded", action: "commission test and remediate" },
    { id: "kyc-aml", area: "Compliance", status: "manual", evidence: "workflow hooks only; no certification", action: "legal/sanctions/record program" },
    { id: "backups", area: "Continuity", status: "manual", evidence: "Neon PITR; Pi chain state on disk", action: "test restore and document RTO/RPO" },
    { id: "live-venue", area: "Execution", status: (alpacaConfigured() || liveConfigured()) ? "pass" : "fail", evidence: alpacaConfigured() ? "Alpaca configured" : (liveConfigured() ? "MT5 configured" : "no live venue"), action: "set Alpaca or MT5 credentials" },
    { id: "chain-service", area: "CommCoin", status: process.env.CHAIN_API_URL ? "pass" : "fail", evidence: process.env.CHAIN_API_URL ? "CHAIN_API_URL set" : "chain service missing", action: "set CHAIN_API_URL/CHAIN_API_TOKEN" },
  ];
  const scored = controls.filter((c) => c.status !== "manual");
  const passed = scored.filter((c) => c.status === "pass").length;
  return json({
    generated_at: new Date().toISOString(),
    reference: "CPIP- defensive concepts; report-only, no auto-apply",
    score_pct: scored.length ? Math.round((passed / scored.length) * 100) : 0,
    passed,
    scored: scored.length,
    manual: controls.filter((c) => c.status === "manual").length,
    controls,
  });
});

router.on("GET", "/api/admin/journal", async ({ req, query }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  try {
    const userId = parseInt(query.get("user_id") || "0", 10);
    const kind = (query.get("kind") || "").trim();
    const symbol = (query.get("symbol") || "").trim().toUpperCase();
    const limit = Math.min(500, Math.max(1, parseInt(query.get("limit") || "200", 10) || 200));
    const rows: any = await db()`
      SELECT j.id, j.user_id, u.email, u.display_name, j.ts, j.symbol, j.strategy,
             j.note, j.pnl_ref, j.kind, j.ref_id, j.details_json,
             o.status AS order_status, o.required_approvals, o.approvals_count,
             o.price AS order_price, o.side AS order_side, o.qty AS order_qty,
             c.status AS cash_status, c.amount AS cash_amount, c.external_reference AS cash_reference
      FROM journal j
      JOIN users u ON u.id = j.user_id
      LEFT JOIN orders o ON j.ref_id = 'order:' || o.id
      LEFT JOIN cash_movements c ON j.ref_id = 'cash:' || c.id
      WHERE (${userId} = 0 OR j.user_id = ${userId})
        AND (${kind} = '' OR j.kind = ${kind})
        AND (${symbol} = '' OR j.symbol = ${symbol})
      ORDER BY j.ts DESC, j.id DESC
      LIMIT ${limit}
    `;
    const counts: any = await db()`
      SELECT kind, COUNT(*)::int AS n FROM journal GROUP BY kind ORDER BY n DESC
    `;
    const recon: any = await db()`
      SELECT COUNT(*)::int AS orders,
             COUNT(*) FILTER (WHERE j.id IS NULL)::int AS missing_journal
      FROM orders o
      LEFT JOIN journal j ON j.ref_id = 'order:' || o.id
    `;
    return json({
      entries: rows.map((r: any) => ({ ...r, details: JSON.parse(r.details_json || "{}") })),
      counts,
      reconciliation: recon[0] || { orders: 0, missing_journal: 0 },
    });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("POST", "/api/admin/users/:id/status", async ({ req, params }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  try {
    const id = parseInt(params.id, 10);
    if (!Number.isInteger(id)) return error(400, "invalid user id");
    const body = await readBody(req);
    const disabled = body.disabled ? 1 : 0;
    if (id === gate.user!.id && disabled) return error(400, "you cannot disable your own account");
    const rows: any = await db()`UPDATE users SET disabled = ${disabled} WHERE id = ${id} RETURNING id, email, disabled`;
    if (!rows.length) return error(404, "user not found");
    await audit(gate.user!.id, disabled ? "admin.user_disable" : "admin.user_enable", gate.user!.tier, { target: id });
    return json({ user: rows[0] });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("POST", "/api/admin/users/:id/tier", async ({ req, params }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  try {
    const id = parseInt(params.id, 10);
    if (!Number.isInteger(id)) return error(400, "invalid user id");
    const body = await readBody(req);
    const tier = String(body.tier || "");
    const allowed: any = await db()`SELECT name FROM tiers`;
    if (!allowed.some((r: any) => r.name === tier)) return error(400, "unknown tier");
    if (id === gate.user!.id && tier !== "Admin") return error(400, "you cannot demote your own admin account");
    const rows: any = await db()`UPDATE users SET tier = ${tier} WHERE id = ${id} RETURNING id, email, tier`;
    if (!rows.length) return error(404, "user not found");
    await audit(gate.user!.id, "admin.user_tier", gate.user!.tier, { target: id, tier });
    return json({ user: rows[0] });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("POST", "/api/admin/symbols/:symbol/status", async ({ req, params }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  try {
    const symbol = String(params.symbol || "").toUpperCase();
    const body = await readBody(req);
    const enabled = body.enabled ? 1 : 0;
    const rows: any = await db()`
      UPDATE symbols SET enabled = ${enabled} WHERE symbol = ${symbol} RETURNING symbol, enabled
    `;
    if (!rows.length) return error(404, "symbol not found");
    await audit(gate.user!.id, enabled ? "admin.symbol_enable" : "admin.symbol_disable", gate.user!.tier, { symbol });
    return json({ symbol: rows[0] });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/admin/external_refs", async () => error(501, "external reference checking is not configured"));

router.on("POST", "/api/admin/live/test", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  const raw = (process.env.MT5_BRIDGE_URL || "").replace(/\/+$/, "");
  if (!raw) return error(503, "MT5_BRIDGE_URL is not configured");
  let base: URL;
  try { base = new URL(raw); } catch { return error(503, "MT5_BRIDGE_URL is invalid"); }
  const loopback = ["127.0.0.1", "localhost", "::1"].includes(base.hostname);
  if (base.protocol !== "https:" && !loopback) return error(503, "MT5 bridge must use HTTPS");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 5000);
  try {
    const res = await fetch(base.origin + base.pathname.replace(/\/+$/, "") + "/health", {
      headers: { "X-Mafutani-Token": process.env.MT5_BRIDGE_TOKEN || "" },
      signal: controller.signal,
    });
    const body = await res.text();
    await audit(gate.user!.id, "admin.live_test", gate.user!.tier, { status: res.status });
    return json({ ok: res.ok, status: res.status, response: body.slice(0, 500) });
  } catch (e) {
    return error(502, "bridge unreachable: " + (e as Error).message);
  } finally {
    clearTimeout(timer);
  }
});

router.on("POST", "/api/admin/users/:id/revoke", async ({ req, params }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  try {
    const id = parseInt(params.id, 10);
    if (!Number.isInteger(id)) return error(400, "invalid user id");
    const rows: any = await db()`DELETE FROM sessions WHERE user_id = ${id} RETURNING token`;
    await audit(gate.user!.id, "admin.sessions_revoke", gate.user!.tier, { target: id, count: rows.length });
    return json({ revoked: rows.length });
  } catch (e) { return error(500, (e as Error).message); }
});
router.on("POST", "/api/admin/external_refs/check", async () => error(501, "external reference checking is not configured"));
router.on("GET", "/api/admin/kyc", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  return json({ kyc: [] });
});
router.on("POST", "/api/admin/kyc/*", async () => error(501, "KYC review is not configured"));

// Server-sent quote tape. Netlify closes the function after the response, so
// this emits one full snapshot and asks EventSource to reconnect; the browser
// then has a live tape without a long-lived connection the host cannot hold.
router.on("GET", "/stream/quotes", async () => {
  try {
    const quotes = await getQuotes(db());
    const body = "retry: 3000\n" +
      "data: " + JSON.stringify({ quotes, ts: Math.floor(Date.now() / 1000) }) + "\n\n";
    return new Response(body, {
      headers: {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-store",
        "Connection": "keep-alive",
      },
    });
  } catch (e) {
    return error(500, (e as Error).message);
  }
});

export default async (req: Request): Promise<Response> => {
  const { pathname } = new URL(req.url);

  // CommCoin/MFT lives in the Python service (chain/ + api/routes.py). The
  // Netlify port proxies it rather than inventing a second ledger.
  if (pathname.startsWith("/api/chain/") || pathname === "/api/offramp/mpesa" || pathname.startsWith("/api/offramp/mpesa/")) {
    const base = (process.env.CHAIN_API_URL || "").replace(/\/+$/, "");
    if (!base) {
      return error(503, "CommCoin chain service is not configured (CHAIN_API_URL). Deploy the Python service and point CHAIN_API_URL at it.");
    }
    const url = new URL(req.url);
    const target = base + pathname + (url.search || "");
    const headers: Record<string, string> = { "Content-Type": req.headers.get("content-type") || "application/json" };
    if (process.env.CHAIN_API_TOKEN) headers["X-Mafutani-Token"] = process.env.CHAIN_API_TOKEN;
    const chainUser = await currentUser(req);
    if (chainUser) headers["X-Mafutani-User"] = chainUser.email;
    if (req.headers.get("x-session")) headers["X-Session"] = req.headers.get("x-session")!;
    if (req.headers.get("cookie")) headers["Cookie"] = req.headers.get("cookie")!;
    try {
      const upstream = await fetch(target, {
        method: req.method,
        headers,
        body: req.method === "GET" || req.method === "HEAD" ? undefined : await req.text(),
      });
      const raw = await upstream.arrayBuffer();
      return new Response(raw, {
        status: upstream.status,
        headers: {
          "Content-Type": upstream.headers.get("content-type") || "application/json",
          "Cache-Control": "no-store",
        },
      });
    } catch (e) {
      return error(502, `CommCoin service unreachable: ${(e as Error).message}`);
    }
  }

  const res = await router.handle(req, pathname);
  return res ?? error(404, "not found");
};

export const config: Config = {
  path: ["/api/*", "/stream/*"],
};