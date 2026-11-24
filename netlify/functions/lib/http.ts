export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ...headers },
  });
}

export function error(status: number, message: string): Response {
  return json({ error: message }, status);
}

export function getSessionToken(req: Request): string {
  const auth = req.headers.get("authorization") || "";
  const bearer = auth.replace(/^Bearer\s+/i, "").trim();
  if (bearer) return bearer;
  const session = (req.headers.get("x-session") || "").trim();
  if (session) return session;
  // Browser fallback: the HttpOnly session cookie. This keeps a reload signed
  // in even when localStorage is unavailable (private mode, cleared storage).
  const cookie = req.headers.get("cookie") || "";
  for (const part of cookie.split(";")) {
    const [name, ...rest] = part.trim().split("=");
    if (name === "session") return decodeURIComponent(rest.join("="));
  }
  return "";
}

/** Set-Cookie value for the session token; Secure on HTTPS deployments. */
export function sessionCookie(token: string, req: Request, maxAgeSeconds = 7 * 24 * 3600): string {
  const secure = new URL(req.url).protocol === "https:" ? "; Secure" : "";
  return `session=${encodeURIComponent(token)}; Path=/; Max-Age=${maxAgeSeconds}; HttpOnly; SameSite=Lax${secure}`;
}

export async function readBody(req: Request): Promise<any> {
  const len = Number(req.headers.get("content-length") || 0);
  if (len > 1_000_000) throw new Error("request body too large (max 1 MB)");
  const ct = req.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    try { return await req.json(); } catch { return {}; }
  }
  if (ct.includes("application/x-www-form-urlencoded") || ct.includes("multipart/form-data")) {
    const text = await req.text();
    const params = new URLSearchParams(text);
    const obj: any = {};
    params.forEach((v, k) => { obj[k] = v; });
    return obj;
  }
  try { return await req.json(); } catch { return {}; }
}
