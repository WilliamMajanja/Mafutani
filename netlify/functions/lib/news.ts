/**
 * News ingestion from Google News RSS, stored in Postgres.
 *
 * The page renders what is stored. A fetch failure is reported as `stale`
 * rather than replaced with invented headlines.
 */

const CACHE_SECONDS = 10 * 60;

function decodeEntities(s: string): string {
  return s
    .replace(/<!\[CDATA\[(.*?)\]\]>/gs, "$1")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&#x27;/g, "'");
}

function tag(xml: string, name: string): string {
  const m = xml.match(new RegExp(`<${name}[^>]*>([\\s\\S]*?)</${name}>`, "i"));
  return m ? decodeEntities(m[1].trim()) : "";
}

export type NewsItem = {
  ts: number;
  symbol: string;
  title: string;
  url: string;
  link: string;
  source: string;
  summary: string;
};

export async function refreshNews(db: any, symbol?: string, limit = 25): Promise<{ items: NewsItem[]; stale: boolean; error?: string }> {
  const sym = (symbol || "").trim().toUpperCase();
  const newest: any = sym
    ? await db`SELECT MAX(ts) AS ts FROM news WHERE symbol = ${sym}`
    : await db`SELECT MAX(ts) AS ts FROM news`;
  const last = Number(newest[0]?.ts || 0);
  const now = Math.floor(Date.now() / 1000);
  const fresh = last > 0 && now - last < CACHE_SECONDS;

  let stale = false;
  let error: string | undefined;
  if (!fresh) {
    const query = sym
      ? "https://news.google.com/rss/search?q=" + encodeURIComponent(`${sym} when:7d`) + "&hl=en-US&gl=US&ceid=US:en"
      : "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en";
    try {
      const res = await fetch(query, {
        headers: { "User-Agent": "Mozilla/5.0 (compatible; MafutaniNews/1.0)" },
      });
      if (!res.ok) throw new Error(`news feed HTTP ${res.status}`);
      const xml = await res.text();
      const blocks = xml.match(/<item>[\s\S]*?<\/item>/gi) || [];
      for (const b of blocks.slice(0, 50)) {
        const rawTitle = tag(b, "title");
        const link = tag(b, "link");
        const pub = tag(b, "pubDate");
        const source = tag(b, "source") || "Google News";
        if (!rawTitle || !link) continue;
        const title = rawTitle.replace(/\s+-\s+[^-]+$/, "").trim();
        const parsed = Date.parse(pub);
        const ts = isFinite(parsed) ? Math.floor(parsed / 1000) : now;
        await db`
          INSERT INTO news (ts, symbol, title, url, source, summary)
          SELECT ${ts}, ${sym}, ${title}, ${link}, ${source}, ${""}
          WHERE NOT EXISTS (SELECT 1 FROM news WHERE url = ${link})
        `;
      }
    } catch (e) {
      stale = true;
      error = (e as Error).message;
    }
  }

  const rows: any = sym
    ? await db`SELECT ts, symbol, title, url, source, summary FROM news WHERE symbol = ${sym} ORDER BY ts DESC LIMIT ${limit}`
    : await db`SELECT ts, symbol, title, url, source, summary FROM news ORDER BY ts DESC LIMIT ${limit}`;
  const items: NewsItem[] = rows.map((r: any) => ({
    ts: Number(r.ts),
    symbol: r.symbol,
    title: r.title,
    url: r.url,
    link: r.url,
    source: r.source,
    summary: r.summary,
  }));
  return { items, stale, error };
}

export async function listNews(db: any, symbol?: string, limit = 20): Promise<any[]> {
  try {
    if (symbol) return await db`SELECT * FROM news WHERE symbol = ${symbol} ORDER BY ts DESC LIMIT ${limit}`;
    return await db`SELECT * FROM news ORDER BY ts DESC LIMIT ${limit}`;
  } catch {
    return [];
  }
}
