// Ambient landing page Worker: serves ./public, plus POST /api/waitlist -> D1.
const EMAIL_RE = /^[^\s@<>()[\]\\,;:"]+@[^\s@<>()[\]\\,;:"]+\.[a-z]{2,}$/i;

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });

async function waitlist(request, env) {
  if (request.method !== "POST") return json({ ok: false, error: "method_not_allowed" }, 405);
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ ok: false, error: "bad_request" }, 400);
  }
  // Honeypot: bots fill every field. Pretend success, store nothing.
  if (body && typeof body.company === "string" && body.company.trim() !== "") return json({ ok: true });

  const email = String(body?.email ?? "").trim().toLowerCase();
  if (email.length > 254 || !EMAIL_RE.test(email)) return json({ ok: false, error: "invalid_email" }, 400);

  const source = String(body?.source ?? "").slice(0, 40) || null;
  const referrer = String(body?.referrer ?? "").slice(0, 500) || null;
  const team_size = ["1-10", "11-30", "31-100"].includes(body?.team_size) ? body.team_size : null;

  // Idempotent: a repeat signup is a success, the first row is kept.
  await env.DB.prepare(
    "INSERT INTO waitlist (email, created_at, source, referrer, team_size) VALUES (?1, ?2, ?3, ?4, ?5) ON CONFLICT(email) DO NOTHING"
  ).bind(email, new Date().toISOString(), source, referrer, team_size).run();
  return json({ ok: true });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/api/waitlist") {
      try {
        return await waitlist(request, env);
      } catch (e) {
        return json({ ok: false, error: "server_error" }, 500);
      }
    }
    return env.ASSETS.fetch(request);
  },
};
