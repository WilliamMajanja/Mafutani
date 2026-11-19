import type { Config } from "@netlify/functions";
import { Router } from "./lib/router";
import { json, error } from "./lib/http";
import { db } from "./lib/db";
import { SCHEMA } from "./lib/schema";
import { hashPassword, verifyPassword, newToken, newCsrf } from "./lib/authpw";
import { storage, BUCKET } from "./lib/storage";
import { chat, listModels, AI_BASE } from "./lib/ai";
import { authConfig, AUTH_BASE } from "./lib/auth";
import { listSymbols, getQuotes, getCandles, listExchanges, listMarkets } from "./lib/market";

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
    await db()`${SCHEMA}`;
    return json({ ok: true });
  } catch (e) {
    return error(500, (e as Error).message);
  }
});

router.on("POST", "/api/auth/signup", async ({ req }) => {
  try {
    const body = await req.json();
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
    const body = await req.json();
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
    const body = await req.json();
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

export default async (req: Request): Promise<Response> => {
  const { pathname } = new URL(req.url);
  const res = await router.handle(req, pathname);
  return res ?? error(404, "not found");
};

export const config: Config = {
  path: "/api/*",
};