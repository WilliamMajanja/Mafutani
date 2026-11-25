import { db } from "./db";

export const SETTING_DEFAULTS: Record<string, string> = {
  order_approval_required: "true",
  order_consensus_threshold: "100000",
  cash_approval_required: "true",
  live_trading_enabled: "true",
  manual_venue_enabled: "true",
  manual_execution_only: "true",
  usd_kes_rate: "130",
  mpesa_c2c_number: process.env.MPESA_C2C_NUMBER || "+254713270444",
  mpesa_c2c_name: process.env.MPESA_C2C_NAME || "Mafutani",
};

const BOOLEAN_KEYS = new Set(["order_approval_required", "cash_approval_required", "live_trading_enabled", "manual_venue_enabled", "manual_execution_only"]);
const NUMBER_KEYS = new Set(["order_consensus_threshold", "usd_kes_rate"]);

export async function getSetting(key: string): Promise<string> {
  try {
    const rows: any = await db()`SELECT value FROM app_settings WHERE key = ${key}`;
    if (rows.length) return String(rows[0].value);
  } catch { /* table may not exist yet */ }
  return SETTING_DEFAULTS[key] ?? "";
}

export async function getBool(key: string): Promise<boolean> {
  return String(await getSetting(key)).toLowerCase() === "true";
}

export async function getNumber(key: string): Promise<number> {
  const n = Number(await getSetting(key));
  return isFinite(n) ? n : Number(SETTING_DEFAULTS[key] || 0);
}

export async function allSettings(): Promise<Record<string, string>> {
  const out: Record<string, string> = { ...SETTING_DEFAULTS };
  try {
    const rows: any = await db()`SELECT key, value FROM app_settings`;
    for (const r of rows) out[r.key] = String(r.value);
  } catch { /* defaults are enough */ }
  return out;
}

export async function setSetting(key: string, value: string, adminId: number): Promise<void> {
  if (!(key in SETTING_DEFAULTS)) throw new Error(`unknown setting: ${key}`);
  let v = String(value).trim();
  if (BOOLEAN_KEYS.has(key)) v = v.toLowerCase() === "true" ? "true" : "false";
  if (NUMBER_KEYS.has(key)) {
    const n = Number(v);
    if (!isFinite(n) || n < 0) throw new Error(`${key} must be a non-negative number`);
    v = String(n);
  }
  await db()`
    INSERT INTO app_settings (key, value, updated_at, updated_by)
    VALUES (${key}, ${v}, EXTRACT(EPOCH FROM NOW())::BIGINT, ${adminId})
    ON CONFLICT (key) DO UPDATE SET
      value = EXCLUDED.value, updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by
  `;
}

export async function setSettings(values: Record<string, unknown>, adminId: number): Promise<Record<string, string>> {
  for (const [k, v] of Object.entries(values || {})) {
    if (!(k in SETTING_DEFAULTS)) continue;
    await setSetting(k, String(v), adminId);
  }
  return await allSettings();
}
