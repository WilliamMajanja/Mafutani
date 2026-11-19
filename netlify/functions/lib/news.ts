export async function listNews(db: any, symbol?: string, limit = 20): Promise<any[]> {
  // News is stored in a simple table if it exists; otherwise return empty.
  try {
    if (symbol) {
      return await db`SELECT * FROM news WHERE symbol = ${symbol} ORDER BY ts DESC LIMIT ${limit}`;
    }
    return await db`SELECT * FROM news ORDER BY ts DESC LIMIT ${limit}`;
  } catch {
    return [];
  }
}
EOF
echo done