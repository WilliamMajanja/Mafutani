import { neon } from "@neondatabase/serverless";

let sql: ReturnType<typeof neon> | null = null;

export function db() {
  if (!sql) {
    const url = process.env.DATABASE_URL || process.env.NEON_DATABASE_URL;
    if (!url) throw new Error("DATABASE_URL is not set");
    sql = neon(url);
  }
  return sql;
}
