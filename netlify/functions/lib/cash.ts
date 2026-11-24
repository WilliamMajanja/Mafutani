import { db } from "./db";

export const MPESA_C2C_NUMBER = process.env.MPESA_C2C_NUMBER || "+254713270444";
export const MPESA_C2C_NAME = process.env.MPESA_C2C_NAME || "Mafutani";

export type CashMovementInput = {
  movement_type?: string;
  rail?: string;
  amount: number;
  currency?: string;
  external_reference?: string;
  idempotency_key?: string;
  details?: Record<string, unknown>;
};

function keyFor(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export async function createCashMovement(userId: number, input: CashMovementInput) {
  const amount = Number(input.amount);
  if (!isFinite(amount) || amount <= 0) throw new Error("amount must be greater than 0");
  const type = String(input.movement_type || "deposit").toLowerCase();
  if (!["deposit", "withdrawal", "fee", "settlement", "adjustment"].includes(type)) {
    throw new Error("invalid movement_type");
  }
  const key = input.idempotency_key || keyFor(type);
  const rows: any = await db()`
    INSERT INTO cash_movements
      (user_id, movement_type, rail, amount, currency, external_reference, status, idempotency_key, details_json)
    VALUES (${userId}, ${type}, ${String(input.rail || "mpesa")}, ${amount},
            ${String(input.currency || "KES")}, ${String(input.external_reference || "")},
            'pending', ${key}, ${JSON.stringify(input.details || {})})
    ON CONFLICT (idempotency_key) DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key
    RETURNING *
  `;
  return rows[0];
}

export async function listCashMovements(userId: number, limit = 50) {
  return await db()`
    SELECT * FROM cash_movements WHERE user_id = ${userId}
    ORDER BY id DESC LIMIT ${Math.min(200, Math.max(1, limit))}
  `;
}

export async function listAdminCashMovements(status?: string, limit = 200) {
  if (status) {
    return await db()`
      SELECT c.*, u.email, u.display_name
      FROM cash_movements c JOIN users u ON u.id = c.user_id
      WHERE c.status = ${status}
      ORDER BY c.id DESC LIMIT ${Math.min(500, Math.max(1, limit))}
    `;
  }
  return await db()`
    SELECT c.*, u.email, u.display_name
    FROM cash_movements c JOIN users u ON u.id = c.user_id
    ORDER BY c.id DESC LIMIT ${Math.min(500, Math.max(1, limit))}
  `;
}

/**
 * Confirm a pending movement and credit the account exactly once.
 *
 * The status transition and the cash credit happen in one statement. A
 * duplicate confirm returns no row, so a replayed callback cannot double-credit.
 */
export async function confirmCashMovement(id: number, adminId: number) {
  const rows: any = await db()`
    WITH m AS (
      UPDATE cash_movements
      SET status = 'confirmed', confirmed_at = EXTRACT(EPOCH FROM NOW())::BIGINT,
          confirmed_by = ${adminId}
      WHERE id = ${id} AND status = 'pending'
      RETURNING id, user_id, amount, currency, movement_type
    ), up AS (
      INSERT INTO accounts (user_id, cash, starting_cash)
      SELECT user_id, amount, 0 FROM m
      ON CONFLICT (user_id) DO UPDATE SET cash = accounts.cash + EXCLUDED.cash
      RETURNING user_id, cash
    )
    SELECT m.*, up.cash
    FROM m JOIN up ON up.user_id = m.user_id
  `;
  return rows[0] || null;
}

export async function rejectCashMovement(id: number, adminId: number, note: string) {
  const rows: any = await db()`
    UPDATE cash_movements
    SET status = 'rejected',
        details_json = jsonb_set(details_json::jsonb, '{rejection_note}', to_jsonb(${note}::text))::text
    WHERE id = ${id} AND status = 'pending'
    RETURNING *
  `;
  return rows[0] || null;
}

export async function cashReconciliation() {
  const rows: any = await db()`
    SELECT
      COALESCE(SUM(CASE WHEN status = 'confirmed' AND movement_type = 'deposit' THEN amount ELSE 0 END), 0) AS confirmed_deposits,
      COALESCE(SUM(CASE WHEN status = 'confirmed' AND movement_type = 'withdrawal' THEN amount ELSE 0 END), 0) AS confirmed_withdrawals,
      COALESCE(SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END), 0) AS pending_count,
      COALESCE(SUM(CASE WHEN status = 'pending' THEN amount ELSE 0 END), 0) AS pending_amount
    FROM cash_movements
  `;
  const acct: any = await db()`SELECT COALESCE(SUM(cash), 0) AS cash FROM accounts`;
  const r = rows[0];
  return {
    confirmed_deposits: Number(r.confirmed_deposits),
    confirmed_withdrawals: Number(r.confirmed_withdrawals),
    net_confirmed: Number((Number(r.confirmed_deposits) - Number(r.confirmed_withdrawals)).toFixed(2)),
    account_cash: Number(acct[0].cash),
    pending_count: Number(r.pending_count),
    pending_amount: Number(r.pending_amount),
    note: "M-Pesa C2C movements are confirmed manually. account_cash also includes starting cash and trading P&L.",
  };
}
