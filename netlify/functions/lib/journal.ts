import { db } from "./db";

export type JournalEntry = {
  kind: string;
  symbol?: string;
  strategy?: string;
  note?: string;
  pnl_ref?: number | null;
  details?: Record<string, unknown>;
  ref_id?: string;
};

/** Append an event to the account journal. Never throws into the caller. */
export async function logJournal(userId: number, entry: JournalEntry): Promise<void> {
  try {
    await db()`
      INSERT INTO journal (user_id, ts, symbol, strategy, note, pnl_ref, kind, details_json, ref_id)
      VALUES (${userId}, ${Math.floor(Date.now() / 1000)}, ${entry.symbol || ""},
              ${entry.strategy || ""}, ${entry.note || ""}, ${entry.pnl_ref ?? null},
              ${entry.kind || "note"}, ${JSON.stringify(entry.details || {})}, ${entry.ref_id || ""})
    `;
  } catch {
    /* journal is an audit aid; it must not break the action it records */
  }
}
