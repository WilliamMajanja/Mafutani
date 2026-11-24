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
import { listNews } from "./lib/news";
import { analyse, detectAnomalies, scoreSeries, annualisedVolPct, type Candle } from "./lib/analytics";
import { currentUser, accountFor } from "./lib/session";
import { executeOrder } from "./lib/trading";
import { runBacktest } from "./lib/backtest";

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
  const accounts = [
    { email: "admin@local", password: "donttrytoplaythefoolwithmeniggesh", tier: "Admin" },
    { email: "demo@local", password: "demo1234", tier: "Pro" },
    { email: "prouser@local", password: "propass123!", tier: "Pro" },
    { email: "institutional@local", password: "institutepass123!", tier: "Institutional" },
    { email: "testuser1@local", password: "testpass123!", tier: "Free" },
    { email: "testuser2@local", password: "testpass123!", tier: "Free" },
  ];
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

router.on("GET", "/api/auth/config", async () => json(authConfig()));

router.on("GET", "/api/realtime/config", async () =>
  json({ url: process.env.NEON_REALTIME_URL || "", hasSecret: !!process.env.NEON_REALTIME_SECRET }),
);

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
    const regions = Array.from(byRegion.values()).map((r) => ({
      ...r,
      asset_classes: Array.from(r.asset_classes),
    }));
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
    const symbol = query.get("symbol") || "";
    const rows: any = await db()`SELECT * FROM quotes_l1 WHERE symbol = ${symbol} ORDER BY ts DESC LIMIT 1`;
    return json({ quote: rows[0] || null });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/news", async ({ query }) => {
  try {
    const symbol = query.get("symbol") || undefined;
    const limit = parseInt(query.get("limit") || "20", 10);
    return json({ news: await listNews(db(), symbol, limit) });
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
    return json({
      cash: acct.cash,
      starting_cash: acct.starting_cash,
      equity: acct.cash + marketValue,
      tier: user.tier,
      live_trading_enabled: false,
      positions,
      fills,
      summary: {
        realized_pnl: Number(realized.toFixed(2)),
        unrealized_pnl: Number(unrealized.toFixed(2)),
        total_pnl: Number((realized + unrealized).toFixed(2)),
        market_value: Number(marketValue.toFixed(2)),
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
    return json({ id: rows[0].id, backtest: rows[0], ...result.metrics }, 201);
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
    return json({
      scanned: syms.length,
      flagged: results.filter((r) => r.anomaly_score > 0).length,
      clean: results.filter((r) => r.anomaly_score <= 0).length,
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
router.on("GET", "/api/policies", async () => json({ policies: [] }));
router.on("GET", "/api/kyc/me", async ({ req }) => {
  const token = getSessionToken(req);
  const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
  if (sess.length === 0) return error(401, "unauthorized");
  return json({ kyc: { status: "none" } });
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
    if (!symbol) return error(400, "symbol required");
    const candles = (await getCandles(db(), symbol, 250, "1d")) as Candle[];
    if (!candles.length) return error(409, "no candles for this symbol");
    return json({ preview: analyse(candles) });
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

router.on("GET", "/api/admin/external_refs", async () => error(501, "external reference checking is not configured"));
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
  const res = await router.handle(req, pathname);
  return res ?? error(404, "not found");
};

export const config: Config = {
  path: ["/api/*", "/stream/*"],
};