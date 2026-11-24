const WINDOW_MS = 60_000;
const MAX = 300;

type Entry = { count: number; reset: number };
const store = new Map<string, Entry>();

function clientIp(req: Request, context: { ip?: string }): string {
  const forwarded = req.headers.get("x-forwarded-for");
  return (forwarded ? forwarded.split(",")[0].trim() : "") || context.ip || "unknown";
}

export default async (req: Request, context: { ip?: string; next: () => Promise<Response> }) => {
  const ip = clientIp(req, context);
  const now = Date.now();
  let entry = store.get(ip);

  if (!entry || entry.reset <= now) {
    entry = { count: 0, reset: now + WINDOW_MS };
    store.set(ip, entry);
  }

  entry.count += 1;
  if (entry.count > MAX) {
    const retry = Math.max(1, Math.ceil((entry.reset - now) / 1000));
    return new Response(JSON.stringify({ error: "too many requests" }), {
      status: 429,
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "Retry-After": String(retry),
        "Cache-Control": "no-store",
      },
    });
  }

  // Keep the map bounded under churn.
  if (store.size > 10_000) {
    for (const [key, value] of store) {
      if (value.reset <= now) store.delete(key);
    }
  }

  return context.next();
};

export const config = { path: "/api/*" };
