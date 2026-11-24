import { db } from "./db";
import { getSessionToken } from "./http";

export type SessionUser = { id: number; email: string; tier: string; csrf_token: string };

/** Resolve the caller from a session token, or null when absent/expired. */
export async function currentUser(req: Request): Promise<SessionUser | null> {
  const token = getSessionToken(req);
  if (!token) return null;
  const rows: any = await db()`
    SELECT u.id, u.email, u.tier, s.csrf_token
    FROM sessions s
    JOIN users u ON u.id = s.user_id
    WHERE s.token = ${token}
      AND s.expires_at > ${Math.floor(Date.now() / 1000)}
      AND u.disabled = 0
    LIMIT 1
  `;
  return rows.length ? rows[0] : null;
}

/** Account row for a user, created on first use so a missing row can't zero a balance. */
export async function accountFor(userId: number): Promise<{ cash: number; starting_cash: number }> {
  const rows: any = await db()`SELECT cash, starting_cash FROM accounts WHERE user_id = ${userId}`;
  if (rows.length) return { cash: Number(rows[0].cash), starting_cash: Number(rows[0].starting_cash) };
  const tier: any = await db()`SELECT tier FROM users WHERE id = ${userId}`;
  const start = tier.length && tier[0].tier === "Admin" ? 100_000_000 : 10_000;
  await db()`INSERT INTO accounts (user_id, cash, starting_cash) VALUES (${userId}, ${start}, ${start}) ON CONFLICT (user_id) DO NOTHING`;
  return { cash: start, starting_cash: start };
}
