export const AI_BASE = process.env.NEON_AI_GATEWAY_BASE_URL || "";
export const AI_TOKEN = process.env.NEON_AI_GATEWAY_TOKEN || "";

export async function chat(messages: { role: string; content: string }[], model = "gpt-4o-mini") {
  const res = await fetch(`${AI_BASE}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${AI_TOKEN}` },
    body: JSON.stringify({ model, messages }),
  });
  if (!res.ok) throw new Error(`AI gateway ${res.status}: ${await res.text()}`);
  return res.json();
}

export async function listModels() {
  const res = await fetch(`${AI_BASE}/v1/models`, {
    headers: { Authorization: `Bearer ${AI_TOKEN}` },
  });
  if (!res.ok) throw new Error(`AI gateway ${res.status}: ${await res.text()}`);
  return res.json();
}