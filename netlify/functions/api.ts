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
      { token, session_token: token, csrf, csrf_token: csrf, user: { id: u.id, email: u.email, tier: u.tier } },
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
    const rows: any = await db()`SELECT id, email, password_hash, tier, disabled FROM users WHERE email = ${email}`;
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
      { token, session_token: token, csrf, csrf_token: csrf, user: { id: u.id, email: u.email, tier: u.tier } },
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
    return json({ symbol, strategy, model: body.model || "claude-haiku-4-5", text, signal: sig, backtest: bt.metrics });
  } catch (e) {
    return error(502, "AI gateway: " + (e as Error).message);
  }
});

router.on("GET", "/api/auth/config", async () => json(authConfig()));

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
    venue: "MT5",
    configured: liveConfigured(),
    available: liveConfigured() && tierEnabled,
    tier_enabled: tierEnabled,
    reason: liveConfigured() ? (tierEnabled ? "" : "live trading is not enabled for this tier") : liveDisabledReason(),
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
      EMEA: { name: "Europe / Middle East / Africa", open: 7, close: 16 },
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
    const liveAvailable = liveConfigured() && !!(tierLive[0] && Number(tierLive[0].live_enabled));

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
        mft = {
          available: true,
          balance_mft: isFinite(balance) ? balance : 0,
          reference_value_usd: isFinite(ref as number) ? ref : null,
          value_usd: isFinite(ref as number) && isFinite(balance) ? Number((balance * (ref as number)).toFixed(2)) : null,
          note: info.reference_value_note || "reference value only, not a market price",
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
      live_trading_enabled: liveAvailable,
      live_venue: liveAvailable ? "MT5" : null,
      live_reason: liveAvailable ? "" : (liveDisabledReason() || "live trading is not enabled for this tier"),
      paper_enabled: paperEnabled(),
      positions,
      fills,
      summary: {
        realized_pnl: Number(realized.toFixed(2)),
        unrealized_pnl: Number(unrealized.toFixed(2)),
        total_pnl: Number((realized + unrealized).toFixed(2)),
        market_value: Number((marketValue + commcoinValue).toFixed(2)),
        market_value_securities: Number(marketValue.toFixed(2)),
        commcoin_value: commcoinValue,
      },
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
    const result = await executeOrder(user.id, body);
    await audit(user.id, "order.place", user.tier, {
      symbol: body.symbol, side: body.side, qty: body.qty, mode: body.mode || "paper",
    });
    return json(result, 201);
  } catch (e) {
    const err = e as Error & { status?: number };
    return error(err.status || 400, err.message);
  }
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

router.on("GET", "/api/journal", async ({ req }) => {
  try {
    const token = getSessionToken(req);
    const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
    if (sess.length === 0) return error(401, "unauthorized");
    const uid = sess[0].user_id;
    const rows: any = await db()`SELECT * FROM journal WHERE user_id = ${uid} ORDER BY ts DESC LIMIT 50`;
    return json({ journal: rows });
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
      INSERT INTO journal (user_id, symbol, strategy, note, pnl_ref)
      VALUES (${uid}, ${body.symbol || ''}, ${body.strategy || ''}, ${body.note || ''}, ${body.pnl_ref ?? null})
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
    const showAll = query.get("all") === "1";
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
      results.push({
        symbol: s.symbol, name: s.name, asset_type: s.asset_type,
        last: a.last, score: a.score, bias: a.bias, regime: a.regime,
        anomaly_score: a.anomaly_score, anomalies: a.anomalies,
      });
    }
    results.sort((x, y) => y.anomaly_score - x.anomaly_score);
    const flagged = results.filter((r) => r.anomaly_score > 0).length;
    return json({
      scanned: syms.length,
      flagged,
      clean: Math.max(0, syms.length - flagged),
      by_severity: bySeverity,
      kinds,
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
  const u: any = await db()`SELECT tier FROM users WHERE id = ${sess[0].user_id}`;
  return json({ tier: u[0]?.tier || "Free", entitlements: {} });
});
router.on("GET", "/api/policies", async () => json(policies()));
router.on("GET", "/api/kyc/me", async ({ req }) => {
  const token = getSessionToken(req);
  const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
  if (sess.length === 0) return error(401, "unauthorized");
  return json({ kyc: { status: "none" } });
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
          "commands: help | status | screener | quote SYMBOL | positions | orders | cash",
          "buy SYMBOL QTY [limit PRICE] | sell SYMBOL QTY [limit PRICE]",
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

router.on("GET", "/api/admin/overview", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  const [users, accounts, symbols, candles, orders, sessions]: any[] = await Promise.all([
    db()`SELECT COUNT(*)::int AS n FROM users`,
    db()`SELECT COUNT(*)::int AS n, COALESCE(SUM(cash), 0) AS cash FROM accounts`,
    db()`SELECT COUNT(*)::int AS n FROM symbols WHERE enabled = 1`,
    db()`SELECT COUNT(*)::int AS n FROM candles_1d`,
    db()`SELECT COUNT(*)::int AS n FROM orders`,
    db()`SELECT COUNT(*)::int AS n FROM sessions WHERE expires_at > ${Math.floor(Date.now() / 1000)}`,
  ]);
  return json({
    overview: {
      users: users[0].n,
      accounts: accounts[0].n,
      cash: Number(accounts[0].cash),
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

router.on("GET", "/api/admin/users", async ({ req }) => {
  const gate = await adminUser(req);
  if (gate.error) return gate.error;
  const rows: any = await db()`
    SELECT u.id, u.email, u.tier, u.disabled, u.created_at, u.last_login_ts,
           COALESCE(a.cash, 0) AS cash
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