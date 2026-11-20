import type { Config } from "@netlify/functions";
import { Router } from "./lib/router";
import { json, error, readBody } from "./lib/http";
import { db } from "./lib/db";
import { SCHEMA } from "./lib/schema";
import { hashPassword, verifyPassword, newToken, newCsrf } from "./lib/authpw";
import { storage, BUCKET } from "./lib/storage";
import { chat, listModels, AI_BASE } from "./lib/ai";
import { authConfig, AUTH_BASE } from "./lib/auth";
import { listSymbols, getQuotes, getCandles, listExchanges, listMarkets } from "./lib/market";
import { listNews } from "./lib/news";

const router = new Router();

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

router.on("POST", "/api/admin/init", async () => {
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

router.on("POST", "/api/admin/seed", async () => {
  const accounts = [
    { email: "admin@local", password: "donttrytoplaythefoolwithmeniggesh", tier: "Admin" },
    { email: "demo@local", password: "demo1234", tier: "Pro" },
    { email: "prouser@local", password: "propass123!", tier: "Pro" },
    { email: "institutional@local", password: "institutepass123!", tier: "Institutional" },
    { email: "testuser1@local", password: "testpass123!", tier: "Free" },
    { email: "testuser2@local", password: "testpass123!", tier: "Free" },
  ];
  const results: any[] = [];
  for (const a of accounts) {
    try {
      const hash = hashPassword(a.password);
      await db()`INSERT INTO users (email, password_hash, tier) VALUES (${a.email}, ${hash}, ${a.tier}) ON CONFLICT (email) DO UPDATE SET password_hash = ${hash}, tier = ${a.tier}`;
      await db()`INSERT INTO accounts (user_id, cash, starting_cash) SELECT id, 100000, 100000 FROM users WHERE email = ${a.email} ON CONFLICT (user_id) DO NOTHING`;
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
    const hash = hashPassword(password);
    const rows: any = await db()`
      INSERT INTO users (email, password_hash) VALUES (${email}, ${hash})
      RETURNING id, email, tier
    `;
    return json({ user: rows[0] }, 201);
  } catch (e) {
    return error(400, (e as Error).message);
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
    return json({ token, csrf_token: csrf, user: { id: u.id, email: u.email, tier: u.tier } });
  } catch (e) {
    return error(400, (e as Error).message);
  }
});

router.on("POST", "/api/auth/logout", async ({ req }) => {
  const auth = req.headers.get("authorization") || "";
  const token = auth.replace(/^Bearer\s+/i, "").trim();
  if (token) await db()`DELETE FROM sessions WHERE token = ${token}`;
  return json({ ok: true });
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
  try { return json({ markets: await listMarkets(db()) }); }
  catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/exchanges", async () => {
  try { return json({ exchanges: await listExchanges(db()) }); }
  catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/candles", async ({ query }) => {
  try {
    const symbol = query.get("symbol") || "";
    const limit = parseInt(query.get("limit") || "200", 10);
    if (!symbol) return error(400, "symbol required");
    const candles = await getCandles(db(), symbol, limit);
    return json({ symbol, candles });
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
    const auth = req.headers.get("authorization") || "";
    const token = auth.replace(/^Bearer\s+/i, "").trim();
    const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
    if (sess.length === 0) return error(401, "unauthorized");
    const uid = sess[0].user_id;
    const acct: any = await db()`SELECT cash, starting_cash FROM accounts WHERE user_id = ${uid}`;
    const pos: any = await db()`SELECT * FROM positions WHERE user_id = ${uid}`;
    const ords: any = await db()`SELECT * FROM orders WHERE user_id = ${uid} ORDER BY created_at DESC LIMIT 20`;
    return json({ portfolio: { cash: acct[0]?.cash ?? 0, starting_cash: acct[0]?.starting_cash ?? 0, positions: pos, orders: ords } });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/orders", async ({ req, query }) => {
  try {
    const auth = req.headers.get("authorization") || "";
    const token = auth.replace(/^Bearer\s+/i, "").trim();
    const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
    if (sess.length === 0) return error(401, "unauthorized");
    const uid = sess[0].user_id;
    const limit = parseInt(query.get("limit") || "20", 10);
    const ords: any = await db()`SELECT * FROM orders WHERE user_id = ${uid} ORDER BY created_at DESC LIMIT ${limit}`;
    return json({ orders: ords });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("POST", "/api/orders", async ({ req }) => {
  try {
    const auth = req.headers.get("authorization") || "";
    const token = auth.replace(/^Bearer\s+/i, "").trim();
    const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
    if (sess.length === 0) return error(401, "unauthorized");
    const uid = sess[0].user_id;
    const body = await readBody(req);
    const rows: any = await db()`
      INSERT INTO orders (user_id, symbol, side, type, qty, price, mode, status)
      VALUES (${uid}, ${body.symbol || ''}, ${body.side || 'buy'}, ${body.type || 'market'}, ${body.qty || 0}, ${body.price ?? null}, ${body.mode || 'paper'}, 'open')
      RETURNING *
    `;
    return json({ order: rows[0] }, 201);
  } catch (e) { return error(400, (e as Error).message); }
});

router.on("GET", "/api/screener", async () => {
  try {
    const rows: any = await db()`SELECT * FROM symbols WHERE enabled = 1`;
    return json({ screener: rows });
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
    const auth = req.headers.get("authorization") || "";
    const token = auth.replace(/^Bearer\s+/i, "").trim();
    const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
    if (sess.length === 0) return error(401, "unauthorized");
    const uid = sess[0].user_id;
    const rows: any = await db()`SELECT * FROM journal WHERE user_id = ${uid} ORDER BY ts DESC LIMIT 50`;
    return json({ journal: rows });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("POST", "/api/journal", async ({ req }) => {
  try {
    const auth = req.headers.get("authorization") || "";
    const token = auth.replace(/^Bearer\s+/i, "").trim();
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
    const auth = req.headers.get("authorization") || "";
    const token = auth.replace(/^Bearer\s+/i, "").trim();
    const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
    if (sess.length === 0) return error(401, "unauthorized");
    const uid = sess[0].user_id;
    const rows: any = await db()`SELECT * FROM risk_limits WHERE user_id = ${uid}`;
    return json({ risk: rows[0] || {} });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/watchlists", async ({ req }) => {
  try {
    const auth = req.headers.get("authorization") || "";
    const token = auth.replace(/^Bearer\s+/i, "").trim();
    const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
    if (sess.length === 0) return error(401, "unauthorized");
    const uid = sess[0].user_id;
    const rows: any = await db()`SELECT * FROM watchlists WHERE user_id = ${uid}`;
    return json({ watchlists: rows });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/backtests", async ({ req }) => {
  try {
    const auth = req.headers.get("authorization") || "";
    const token = auth.replace(/^Bearer\s+/i, "").trim();
    const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
    if (sess.length === 0) return error(401, "unauthorized");
    const uid = sess[0].user_id;
    const rows: any = await db()`SELECT * FROM backtests WHERE user_id = ${uid} ORDER BY created_at DESC LIMIT 20`;
    return json({ backtests: rows });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("POST", "/api/backtests", async ({ req }) => {
  try {
    const auth = req.headers.get("authorization") || "";
    const token = auth.replace(/^Bearer\s+/i, "").trim();
    const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
    if (sess.length === 0) return error(401, "unauthorized");
    const uid = sess[0].user_id;
    const body = await readBody(req);
    const rows: any = await db()`
      INSERT INTO backtests (user_id, symbol, params_json, status)
      VALUES (${uid}, ${body.symbol || ''}, ${JSON.stringify(body.params || {})}, 'done')
      RETURNING *
    `;
    return json({ backtest: rows[0] }, 201);
  } catch (e) { return error(400, (e as Error).message); }
});

router.on("GET", "/api/analysis", async ({ query }) => {
  try {
    const symbol = query.get("symbol") || "";
    return json({ symbol, analysis: {} });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/anomalies", async () => {
  try {
    return json({ anomalies: [] });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/chart_context", async ({ query }) => {
  try {
    const symbol = query.get("symbol") || "";
    return json({ symbol, context: {} });
  } catch (e) { return error(500, (e as Error).message); }
});

router.on("GET", "/api/chart_position", async ({ query }) => {
  try {
    const symbol = query.get("symbol") || "";
    return json({ symbol, position: null });
  } catch (e) { return error(500, (e as Error).message); }
});

// --- remaining endpoints (stubs returning safe defaults) ---
const stub = (data: any = {}) => async () => json(data);

router.on("GET", "/api/i18n", async () => json({ locales: ["en"] }));
router.on("GET", "/api/i18n/messages", async ({ query }) => json({ locale: query.get("locale") || "en", messages: {} }));
router.on("GET", "/api/me/entitlements", async ({ req }) => {
  const auth = req.headers.get("authorization") || "";
  const token = auth.replace(/^Bearer\\s+/i, "").trim();
  const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
  if (sess.length === 0) return error(401, "unauthorized");
  const u: any = await db()`SELECT tier FROM users WHERE id = ${sess[0].user_id}`;
  return json({ tier: u[0]?.tier || "Free", entitlements: {} });
});
router.on("GET", "/api/policies", async () => json({ policies: [] }));
router.on("GET", "/api/kyc/me", async ({ req }) => {
  const auth = req.headers.get("authorization") || "";
  const token = auth.replace(/^Bearer\\s+/i, "").trim();
  const sess: any = await db()`SELECT user_id FROM sessions WHERE token = ${token} AND expires_at > ${Math.floor(Date.now()/1000)}`;
  if (sess.length === 0) return error(401, "unauthorized");
  return json({ kyc: { status: "none" } });
});
router.on("GET", "/api/allocation", async () => json({ allocation: {} }));
router.on("GET", "/api/arbitrage", async () => json({ arbitrage: [] }));
router.on("GET", "/api/arbitrage/venues", async () => json({ venues: [] }));
router.on("GET", "/api/cot", async () => json({ cot: [] }));
router.on("GET", "/api/cot/contract", async () => json({ contract: null }));
router.on("GET", "/api/cot/history", async () => json({ history: [] }));
router.on("POST", "/api/cot/sync", async () => json({ ok: true }));
router.on("GET", "/api/macro", async () => json({ macro: [] }));
router.on("GET", "/api/macro/series", async () => json({ series: [] }));
router.on("POST", "/api/macro/fetch", async () => json({ ok: true }));
router.on("GET", "/api/agents", async () => json({ agents: [] }));
router.on("GET", "/api/agents/tick", async () => json({ ok: true }));
router.on("GET", "/api/auth/2fa", async () => json({ enabled: false }));
router.on("POST", "/api/auth/2fa/setup", async () => json({ secret: "", qr: "" }));
router.on("POST", "/api/auth/2fa/enable", async () => json({ ok: true }));
router.on("POST", "/api/auth/2fa/disable", async () => json({ ok: true }));
router.on("POST", "/api/signals/preview", async () => json({ preview: {} }));
router.on("POST", "/api/backtests/validate", async () => json({ valid: true }));
router.on("GET", "/api/backtests/", async ({ req }) => {
  // detail view by id in path handled below
  return json({ backtest: null });
});
router.on("GET", "/api/admin/overview", async () => json({ overview: {} }));
router.on("GET", "/api/admin/readiness", async () => json({ ready: true }));
router.on("GET", "/api/admin/audit", async () => json({ audit: [] }));
router.on("GET", "/api/admin/users", async () => json({ users: [] }));
router.on("GET", "/api/admin/symbols/", async () => json({ symbols: [] }));
router.on("GET", "/api/admin/external_refs", async () => json({ refs: [] }));
router.on("POST", "/api/admin/external_refs/check", async () => json({ ok: true }));
router.on("GET", "/api/admin/kyc", async () => json({ kyc: [] }));
router.on("POST", "/api/admin/kyc/", async () => json({ ok: true }));

export default async (req: Request): Promise<Response> => {
  const { pathname } = new URL(req.url);
  const res = await router.handle(req, pathname);
  return res ?? error(404, "not found");
};

export const config: Config = {
  path: "/api/*",
};