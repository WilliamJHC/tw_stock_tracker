/**
 * TradingView alert webhook -> Telegram forwarder (Cloudflare Worker).
 *
 * Runs on Cloudflare's free tier (100k requests/day) so alerts keep arriving
 * when the PC running monitor.py is off -- that is the whole reason this is a
 * Worker and not just an app.py endpoint.
 *
 * TradingView's alert UI can only POST a body to a fixed URL: it cannot set
 * custom headers, so the shared secret has to travel in the body or the query
 * string. Both are accepted (see authorize()); pick whichever fits the alert:
 *   - JSON body:  {"secret": "<TV_WEBHOOK_SECRET>", "message": "..."}
 *   - plain text: set the URL to https://<worker>/?token=<TV_WEBHOOK_SECRET>
 *                 and put any text in the alert body
 *
 * Secrets (set with `wrangler secret put <NAME>`, never in wrangler.toml):
 *   TV_WEBHOOK_SECRET   shared secret TradingView must present
 *   TELEGRAM_BOT_TOKEN  same bot as monitor.py's alerts
 *   TELEGRAM_CHAT_ID    same chat as monitor.py's alerts
 */

const MAX_BODY_BYTES = 8192;   // TradingView alert messages are short; cap to bound abuse
const TELEGRAM_API = "https://api.telegram.org";

/** Length-independent comparison, so a wrong secret can't be recovered by timing the response. */
function timingSafeEqual(a, b) {
  const ea = new TextEncoder().encode(a);
  const eb = new TextEncoder().encode(b);
  // Compare lengths without early-returning on the byte loop below.
  let diff = ea.length ^ eb.length;
  const n = Math.max(ea.length, eb.length);
  for (let i = 0; i < n; i++) {
    diff |= (ea[i] ?? 0) ^ (eb[i] ?? 0);
  }
  return diff === 0;
}

/**
 * Extracts the presented secret from either the JSON body or ?token=, and
 * checks it against TV_WEBHOOK_SECRET. Returns the parsed payload on success
 * or null on failure -- callers must not echo back why it failed.
 */
function authorize(url, bodyText, expectedSecret) {
  let parsed = null;
  try {
    parsed = JSON.parse(bodyText);
  } catch {
    parsed = null;  // plain-text alert body; secret must then come from ?token=
  }

  const presented =
    (parsed && typeof parsed.secret === "string" ? parsed.secret : null) ??
    url.searchParams.get("token");

  if (typeof presented !== "string" || !timingSafeEqual(presented, expectedSecret)) {
    return null;
  }
  return { parsed, text: bodyText };
}

/** Builds the Telegram message. [TV] prefix distinguishes these from monitor.py's own alerts. */
function formatMessage({ parsed, text }) {
  if (parsed && typeof parsed === "object") {
    // Drop the secret so it never reaches Telegram's servers or the chat log.
    const { secret, ...rest } = parsed;
    const body = typeof rest.message === "string"
      ? rest.message
      : JSON.stringify(rest);
    return `[TV] ${body}`;
  }
  return `[TV] ${text}`;
}

async function sendTelegram(env, message) {
  const resp = await fetch(`${TELEGRAM_API}/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text: message }),
  });
  return resp.ok;
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }
    if (!env.TV_WEBHOOK_SECRET || !env.TELEGRAM_BOT_TOKEN || !env.TELEGRAM_CHAT_ID) {
      // Misconfigured deployment: fail loudly here rather than silently
      // accepting unauthenticated alerts because the secret is undefined.
      return new Response("worker not configured", { status: 500 });
    }

    const bodyText = (await request.text()).slice(0, MAX_BODY_BYTES);
    const authorized = authorize(new URL(request.url), bodyText, env.TV_WEBHOOK_SECRET);
    if (authorized === null) {
      return new Response("unauthorized", { status: 401 });
    }

    const ok = await sendTelegram(env, formatMessage(authorized));
    return ok
      ? new Response("ok", { status: 200 })
      : new Response("telegram send failed", { status: 502 });
  },
};
