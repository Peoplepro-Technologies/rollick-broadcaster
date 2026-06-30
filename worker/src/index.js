/**
 * rollick-broadcaster-redirect — Cloudflare Worker
 *
 * Reads the current *.trycloudflare.com backend URL from the
 * BACKEND_URLS KV namespace and 307-redirects every request to it.
 * Path, query, and HTTP method are preserved.
 *
 * Why 307 and not 302:
 *   The viewer page has a "post a comment" form that posts to /api/*
 *   on the public URL. 302 causes some clients (and RFC 7231 §6.4.3
 *   itself) to downgrade the method to GET, silently losing the
 *   comment body. 307 preserves method and body across the redirect.
 *
 * Failure modes:
 *   - KV key "current" missing: 503 "Tunnel not configured. Run
 *     scripts/start-tunnel.sh on the host." — operator runs the
 *     script to populate KV.
 *   - Backend down: the 307 still fires, the user sees a Cloudflare
 *     502/1033 from the backend. Fix by re-running start-tunnel.sh
 *     after restoring the backend.
 */
export default {
  async fetch(request, env, ctx) {
    const backend = await env.BACKEND_URLS.get("current");
    if (!backend) {
      return new Response(
        "Tunnel not configured. Run scripts/start-tunnel.sh on the host.",
        { status: 503, headers: { "content-type": "text/plain; charset=utf-8" } }
      );
    }
    const target = new URL(request.url);
    target.protocol = backend.startsWith("https://") ? "https:" : "http:";
    // Strip protocol AND any path from the backend value — the
    // captured trycloudflare URL is always just scheme + host.
    target.host = backend.replace(/^https?:\/\//, "").replace(/\/.*$/, "");
    // Path + query are already preserved by the URL constructor.
    return Response.redirect(target.toString(), 307);
  },
};
