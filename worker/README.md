# rollick-broadcaster-redirect

A tiny Cloudflare Worker that gives the Rollick BROADCASTER a **stable public URL**.

## What it does

Reads the current `*.trycloudflare.com` URL from a Cloudflare KV namespace and
**307-redirects** every request to it. Path, query string, and HTTP method are
preserved.

```
Email link  →  https://rollick-broadcaster.<your-subdomain>.workers.dev/b/abc123
                │
                ▼  (Worker reads KV key "current" → "https://xyz.trycloudflare.com")
              307
                ▼
Browser     →  https://xyz.trycloudflare.com/b/abc123
                │
                ▼
            viewer page loads
```

The `*.workers.dev` URL is **stable for the lifetime of the Worker** — it does
not change on Docker / PC / `cloudflared` restarts. Only the trycloudflare URL
behind it changes; the Worker always 307s to the live one.

## One-time setup

Prerequisites: a Cloudflare account and Node.js (verified with Node 22).

```bash
# 1. Install wrangler (if you don't have it)
npm install -g wrangler

# 2. Log in to your Cloudflare account
wrangler login

# 3. Create the KV namespace (one-time)
wrangler kv:namespace create BACKEND_URLS
# → prints something like:
#   { binding = "BACKEND_URLS", id = "abcd1234..." }
# Copy the id into wrangler.toml where it currently says
# `id = "REPLACE_AFTER_wrangler_kv_namespace_create"`.

# 4. Deploy the Worker
wrangler deploy
# → prints something like:
#   Published rollick-broadcaster-redirect (1.23 sec)
#   https://rollick-broadcaster-redirect.<your-account>.workers.dev
# Note the URL — that becomes your stable BASE_PUBLIC_URL.

# 5. Create a Cloudflare API token (host writes to KV from start-tunnel.sh)
#    Cloudflare dashboard → My Profile → API Tokens → Create Token
#    → "Edit Cloudflare Workers" template
#    → Account Resources: Workers KV Storage → Edit (scope to BACKEND_URLS)
#    Copy the token; you'll add it to .env in the repo as CF_API_TOKEN.

# 6. Get your Cloudflare Account ID
#    Cloudflare dashboard → right sidebar → "Account ID"
#    Copy it; you'll add it to .env in the repo as CF_ACCOUNT_ID.
```

## Wire it into the BROADCASTER

In the BROADCASTER repo, add to `.env` (or `.env.example` for the template):

```bash
BASE_PUBLIC_URL=https://rollick-broadcaster-redirect.<your-account>.workers.dev
CF_API_TOKEN=<the token from step 5>
CF_ACCOUNT_ID=<the id from step 6>
CF_KV_NAMESPACE_ID=<the id from step 3>
```

Then open `/admin/settings` in the BROADCASTER admin UI, paste the
`BASE_PUBLIC_URL` into "Public base URL", and click Save. The runtime-override
path applies it immediately — no container restart needed.

## How the host side stays in sync

`scripts/start-tunnel.sh` runs on the BROADCASTER host and updates the
`current` KV key on every tunnel (re)start:

```
docker compose up -d cloudflared             # start the tunnel
... wait for trycloudflare URL in logs ...
python -m scripts.tunnel_kv put <new URL>    # write to KV
python -m scripts.tunnel_kv get              # verify round-trip
python -m scripts.tunnel_kv probe <workers.dev URL>  # confirm 200 end-to-end
```

The KV update is the **only** thing that needs to happen for the Worker to
start 307-redirecting to the new backend. The BROADCASTER app is never
restarted.

## Local development

```bash
# Run the worker locally (needs a `current` KV entry — populated via dev mode
# or a manual `wrangler kv:key put --binding BACKEND_URLS current "http://localhost:8123"`)
npm run dev
# → starts on http://localhost:8787
```

## What it costs

Stays well within the Cloudflare Workers **free tier**:

- 100,000 requests/day (each broadcast link click is 1 request).
- 1,000 KV writes/day (one write per tunnel restart).
- KV reads are also free up to 100,000/day.

For Rollick's volume (a few broadcasts a day, a few hundred link clicks), the
bill will be **$0/month**.

## Files

- `src/index.js` — the Worker code (one function, ~25 lines).
- `wrangler.toml` — Worker config + KV binding.
- `package.json` — `wrangler` dev dependency.
- `.gitignore` — excludes `node_modules/`, `.wrangler/`, `.dev.vars`.
