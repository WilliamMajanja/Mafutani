export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", ...headers },
  });
}

export function error(status: number, message: string): Response {
  return json({ error: message }, status);
}

export async function readBody(req: Request): Promise<any> {
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
