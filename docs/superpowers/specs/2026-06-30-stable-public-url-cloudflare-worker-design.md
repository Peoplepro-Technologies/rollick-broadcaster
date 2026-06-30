# Stable Public URL for Broadcast Links via Cloudflare Worker + KV

**Date:** 2026-06-30
**Status:** Draft (pending user review)
**Supersedes:** The `BASE_PUBLIC_URL` capture-and-recreate-app step in `scripts/start-tunnel.sh` (the new script writes the backend URL to Cloudflare KV and the app is no longer restarted to pick up a new public URL).

## Problem

`scripts/start-tunnel.sh` currently brings up a Cloudflare **quick-tunnel** sidecar (`tunnel --no-autoupdate --url http://app:8123`). Cloudflare assigns a **random `*.trycloudflare.com` hostname** to the tunnel every time `cloudflared` starts. The script captures the URL from the logs, writes it into `.env` as `BASE_PUBLIC_URL`, and recreates the `app` container so outgoing emails carry that URL.

The result: any of the following breaks every previously-sent broadcast email.

- The `cloudflared` Docker container restarts (manual or crash): new random URL → old URL is dead.
- The PC reboots: Docker may auto-start `cloudflared` → new random URL.
- `docker compose down` + `up` is run: same outcome.

Because `BASE_PUBLIC_URL` is the only thing baked into the email body, and that value changes on every tunnel restart, all links sent in past emails point to a dead `trycloudflare` subdomain. The user reported this on 2026-06-30 after a Docker restart.

## Goals & non-goals

**Goals**
- The public URL embedded in a broadcast email must be **stable across every kind of restart** (Docker, PC, `cloudflared`-only, app-only).
- One source of truth for "where is the live backend right now" so the app never has to be restarted to pick up a new public URL.
- Re-running `scripts/start-tunnel.sh` (or letting `cloudflared` auto-recover) must "just work" — operator runs one command, the new backend URL is registered, old email links keep working.
- Robust error handling: if the registration step fails, the script exits non-zero and the old backend URL in KV is preserved (no half-state).
- No DNS / domain purchase required.
- Free tier compatible (Cloudflare Workers free plan: 100k requests/day, KV 100k reads/day — more than enough for broadcast links).

**Non-goals**
- Re-sending the viewer link to past broadcast recipients whose emails already have the dead trycloudflare URL baked in. (User confirmed on 2026-06-30: "Just fix going forward; old emails are dead.")
- Reverse-proxying the entire app through the Worker (a 307 is one round-trip and keeps the Worker tiny).
- Buying / configuring a custom domain.
- Moving SMTP / WhatsApp credentials into Cloudflare (out of scope for tunnel stability).
- A health-check probe from the Worker to the backend (would burn Worker CPU on every link click; deferred — see §6 *Failure modes*).

## Design

### 0. Composition rule (read this first)

The new system has exactly one new public dependency — the Cloudflare Worker at `https://<subdomain>.<account>.workers.dev`. Everything else (app, Docker stack, `cloudflared` sidecar) stays the same shape as today, except:

| Component                  | Today                                            | After this spec                                            |
|----------------------------|--------------------------------------------------|------------------------------------------------------------|
| `BASE_PUBLIC_URL` in `/admin/settings` (runtime override) | The current random `*.trycloudflare.com` URL | The stable `<subdomain>.<account>.workers.dev` URL (set once) |
| `start-tunnel.sh`          | Captures URL, writes `.env`, recreates `app`     | Captures URL, writes URL to Cloudflare KV, no `app` restart |
| Email link in body         | `https://<random>.trycloudflare.com/b/<token>`  | `https://<subdomain>.<account>.workers.dev/b/<token>`     |
| Runtime redirect           | n/a                                              | Worker reads KV → 307 to current `*.trycloudflare.com` URL |
| Operator's mental model    | "every restart = new URL = resend everything"    | "every restart = re-run `start-tunnel.sh` once, all links keep working" |

The two critical invariants:

1. **`settings.base_public_url` never changes after first setup.** It is the workers.dev URL, which Cloudflare guarantees is stable for the lifetime of the Worker.
2. **The Worker always knows the current `*.trycloudflare.com` URL.** Updated by `start-tunnel.sh` after every tunnel (re)start. Old value preserved on failure.

### 1. Cloudflare Worker

A single Worker with a single purpose: read the current backend URL from a KV binding and respond with `307 Temporary Redirect` to `<backend><path>`. The Worker has no business logic, no auth, no logging beyond platform defaults.

**Deployed URL** (stable, operator notes this once): `https://<subdomain>.<account>.workers.dev`

**Code (`worker/src/index.js`):**

```javascript
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
    target.host = backend.replace(/^https?:\/\//, "").replace(/\/.*$/, "");
    // 307 (not 302) so POST/PUT bodies are preserved — the viewer page
    // has a "post a comment" form that would silently lose its body on
    // a 302 downgrade to GET. 307 is universally supported by modern
    // browsers and is correct for both GET and non-GET methods here.
    return Response.redirect(target.toString(), 307);
  },
};
```

**Configuration (`worker/wrangler.toml`):**

```toml
name = "rollick-broadcaster-redirect"
main = "src/index.js"
compatibility_date = "2025-09-01"

[[kv_namespaces]]
binding = "BACKEND_URLS"
id = "<filled-by-wrangler-kv:namespace-create>"
```

### 2. Cloudflare KV namespace

One namespace, one key:

| Key       | Value                                           | TTL | Set by                            | Read by             |
|-----------|-------------------------------------------------|-----|-----------------------------------|---------------------|
| `current` | The live `https://<random>.trycloudflare.com`   | none | `start-tunnel.sh` via CF API      | Worker (per request)|

- Namespace created once: `wrangler kv:namespace create BACKEND_URLS`
- The namespace ID is baked into `wrangler.toml` and also stored in `.env` as `CF_KV_NAMESPACE_ID` so the host can write to it from `start-tunnel.sh`.
- If `current` is missing, the Worker returns `503` (see §6).

### 3. `.env` additions (operator fills these in once during setup)

```bash
# Cloudflare credentials used by scripts/start-tunnel.sh to write the
# current *.trycloudflare.com URL into KV. Read-only on the Worker side;
# write-only from the host. Scope the API token to:
#   Account → Workers KV Storage → Edit
# on namespace BACKEND_URLS only.
CF_API_TOKEN=...
CF_ACCOUNT_ID=...
CF_KV_NAMESPACE_ID=...
```

**What stays in `.env` (unchanged):** `ADMIN_PASSWORD`, `SESSION_SECRET`, `SMTP_*`, `WHATSAPP_*`, `DATABASE_URL`, etc.

**What is no longer written by `start-tunnel.sh`:** the `BASE_PUBLIC_URL` line in `.env` that the old script used to overwrite on every run. After this spec, `.env`'s `BASE_PUBLIC_URL` (if present) is set to the stable workers.dev URL **once** during the §7 setup checklist, then never touched again. The runtime-override value at `/admin/settings` is the active source of truth — `.env` is a fallback in case the DB is ever reset. The default in `broadcaster/settings.py` (`"http://localhost:8123"`) stays as a development fallback for local-only work.

### 4. `scripts/start-tunnel.sh` — modified flow

```
1.  cd "$(dirname "$0")/.."
2.  Pre-flight: source .env; require BASE_PUBLIC_URL, CF_API_TOKEN,
                 CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID; otherwise exit 1.
3.  Bring up cloudflared:  docker compose --profile tunnel up -d cloudflared
4.  Wait up to 60s for the trycloudflare URL to appear in cloudflared logs.
5.  Capture URL into $URL.  (existing logic, unchanged)
6.  PUT $URL into Cloudflare KV under key "current":
       curl -fsS -X PUT \
         "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/storage/kv/namespaces/$CF_KV_NAMESPACE_ID/values/current" \
         -H "Authorization: Bearer $CF_API_TOKEN" \
         --data "$URL"
    If this fails → print error + last 30 lines of cloudflared logs, exit 1.
    The previous KV value is untouched on failure.
7.  Verify the write by GET-ing the value back and comparing to $URL.
    If mismatch → exit 1.
8.  Sanity-check the live redirect: curl -fsS -o /dev/null -w "%{http_code}\n"
       -L "$BASE_PUBLIC_URL/api/health"  → must print 200.
    If not 200 → exit 1 with the actual code.
9.  Skip the `docker compose up -d --force-recreate app` step entirely
    (BASE_PUBLIC_URL never changes; the app reads from .env on boot and
    on every settings read).
10. Print summary:
       "✓ Stable URL:    $BASE_PUBLIC_URL
        ✓ Current tunnel: $URL
        ✓ Health check:   200"
```

**Removed** (vs. today): step that wrote `BASE_PUBLIC_URL=$URL` to `.env`, and the `docker compose up -d --force-recreate app` step.

**New** (vs. today): the PUT-to-KV step (6) and the GET-back-to-verify step (7) and the end-to-end health probe (8).

**Stop behavior** (when operator runs `docker compose --profile tunnel down`): unchanged. The KV value `current` is left as-is. If a new tunnel is brought up later, step 6 overwrites it. If the operator wants the Worker to stop serving traffic entirely (e.g., for maintenance), they can `wrangler kv:key delete --binding=BACKEND_URLS current` manually.

### 5. What the app code needs to change

**Nothing in `broadcaster/`.** The app reads `settings.base_public_url` in three places:

- `broadcaster/services/broadcasts.py:326` — email body
- `broadcaster/routes/viewer.py:55`     — viewer page redirects
- `broadcaster/routes/viewer.py:116`    — template context

After this spec, `settings.base_public_url` is the workers.dev URL. Every email and every viewer link the app produces is a workers.dev URL. The Worker then 307s to the live trycloudflare backend. The app is unaware of the trycloudflare URL as long as `.env`'s `BASE_PUBLIC_URL` is set correctly to the workers.dev URL.

**On first migration** the operator has to do exactly one of:

- **Option A (preferred):** open `/admin/settings`, paste the workers.dev URL into the "Public base URL" field, click Save. This goes through the `runtime_overrides` path (`base_public_url` is in the allowlist of keys that update without an app restart — see `broadcaster/services/settings.py:5`). No container restart, no email-server restart, takes effect on the next broadcast.
- **Option B:** edit `.env`'s `BASE_PUBLIC_URL` to the workers.dev URL, then `docker compose up -d --force-recreate app` so the new value is read on container start. Pick this only if `/admin/settings` is unreachable (e.g., the app is broken in some other way).

For the very first deploy, use Option A. Subsequent re-runs of `start-tunnel.sh` do **not** require either — only the trycloudflare URL in KV changes, and the app's `BASE_PUBLIC_URL` (workers.dev) is unaffected.

### 6. Failure modes & how each is handled

| Failure                                            | What user sees                                       | What script / system does                                  |
|----------------------------------------------------|------------------------------------------------------|-------------------------------------------------------------|
| `cloudflared` fails to start, no URL in logs       | `start-tunnel.sh` exits 1; KV unchanged              | Old KV value still serves. App keeps working with old URL.  |
| Cloudflare API PUT to KV fails (network, 401)      | `start-tunnel.sh` exits 1; cloudflared stays up      | Old KV value still serves. Operator can re-run script.      |
| KV PUT succeeds but GET-back mismatches            | `start-tunnel.sh` exits 1; old KV preserved          | Operator re-runs; Cloudflare rarely returns this.           |
| `cloudflared` Docker restarts mid-session          | New random URL appears; old URL is dead              | Operator re-runs `start-tunnel.sh`. KV updated. All email links keep working because they go through the stable workers.dev URL. |
| App container restarts                             | App reads same runtime-overridden `base_public_url`; emails still good | Nothing to do.                                              |
| PC reboots                                         | Docker auto-starts `app` (and may auto-start `cloudflared` via `restart: unless-stopped` in `docker-compose.yml`); the auto-started `cloudflared` gets a new random URL but KV is **stale** | Operator must re-run `start-tunnel.sh` once after reboot to refresh KV. The script's step 6 will overwrite the stale value. **Mitigation (post-v1)**: cron / systemd timer that re-runs the script on boot — out of scope for this spec. |
| KV is empty (first deploy, never ran script)       | Any click on the workers.dev URL → `503 "Tunnel not configured. Run scripts/start-tunnel.sh on the host."` | Operator runs `start-tunnel.sh` once.                        |
| Backend down but KV has URL                        | Click → 307 → trycloudflare returns 502/1033          | Re-run `start-tunnel.sh` after fixing the backend.          |
| Operator pastes wrong value into `/admin/settings` "Public base URL" (typo, missing `https://`, etc.) | New emails go to a non-existent URL; click → "site not found" | Fix via `/admin/settings` again. Past emails sent with the bad value are dead — but the new value takes effect immediately on the next broadcast. |

**Out of scope for this spec but worth noting:**

- A Worker-side health probe before the 307 (e.g., `await fetch(backend + "/api/health")`) would convert the 502/1033 case into a clean 503 with a useful message. This adds one outbound request per link click. Defer to a v1.1 if the user reports it as confusing.
- A systemd / launchd unit on the host that auto-runs `start-tunnel.sh` after reboot. Without it, the user must remember to run the script after a PC reboot (the `cloudflared` container is `restart: unless-stopped`, so it WILL come up, but with a stale KV).

### 7. Setup checklist (one-time, manual)

Documented in the spec so the user can execute it; the implementation plan turns each step into a verifiable task.

1. Confirm `wrangler` is installed (`npx wrangler --version`). Install if missing: `npm install -g wrangler`.
2. `wrangler login` (opens browser, authenticates to the user's existing Cloudflare account).
3. From the new `worker/` dir in this repo: `wrangler kv:namespace create BACKEND_URLS` — copy the printed `id = "..."` into `worker/wrangler.toml`.
4. `wrangler deploy` — note the printed `https://<subdomain>.<account>.workers.dev` URL.
5. Cloudflare dashboard → My Profile → API Tokens → Create Token → Edit Cloudflare Workers template → scope to: Account `<the account>`, Account Resources `Workers KV Storage:Edit` on namespace `BACKEND_URLS`. Copy token to `.env` as `CF_API_TOKEN`.
6. Cloudflare dashboard → right sidebar → "Account ID" → copy to `.env` as `CF_ACCOUNT_ID`.
7. From `.env`, set:
   - `BASE_PUBLIC_URL=https://<subdomain>.<account>.workers.dev`  (from step 4) — kept in sync with the runtime override so a DB reset still leaves the right default
   - `CF_KV_NAMESPACE_ID=<id>`                                    (from step 3)
8. Open `/admin/settings` in a browser, set "Public base URL" to the workers.dev URL from step 4, click Save. Verify by `curl -fsS http://localhost:8123/api/settings` and inspecting the `base_public_url` field (or the admin UI).
9. `bash scripts/start-tunnel.sh` — verify it captures the trycloudflare URL, PUTs it to KV, GETs it back, and the end-to-end health check on the workers.dev URL returns 200.
10. Send a test broadcast to yourself. Click the link in the email. Confirm the 307 lands on the viewer page and the video plays.

### 8. Files added / changed

| File                                                         | Action  | Why                                                              |
|--------------------------------------------------------------|---------|------------------------------------------------------------------|
| `worker/wrangler.toml`                                       | add     | Worker config + KV binding                                       |
| `worker/src/index.js`                                        | add     | Worker code (307 redirector)                                     |
| `worker/.gitignore`                                          | add     | Ignore `node_modules`, `.wrangler/`, `.dev.vars`                 |
| `worker/README.md`                                           | add     | One-command deploy instructions                                  |
| `scripts/start-tunnel.sh`                                    | modify  | Replace `.env` write + app recreate with KV PUT + verify + probe |
| `docs/superpowers/specs/2026-06-30-stable-public-url-cloudflare-worker-design.md` | add | This spec                                          |
| `docs/superpowers/plans/2026-06-30-stable-public-url-cloudflare-worker.md`       | add | Implementation plan (written by writing-plans skill) |
| `.env.example`                                               | modify  | Add `CF_API_TOKEN`, `CF_ACCOUNT_ID`, `CF_KV_NAMESPACE_ID`; document `BASE_PUBLIC_URL=https://<subdomain>.<account>.workers.dev` (placeholder, filled in during §7 step 4) |
| `README.md` (root)                                           | modify  | Add "Stable public URL" section pointing to `worker/README.md`    |

No changes to:
- `broadcaster/` (app code, settings, routes) — `base_public_url` plumbing already accepts any string and is in the runtime-overrides allowlist.
- `docker-compose.yml` — `cloudflared` sidecar unchanged.
- `tests/` — no app behavior change; new tests for `start-tunnel.sh` are shell-level (see §9).

### 9. Testing strategy

**Unit / integration tests (Python, `tests/test_start_tunnel.py`)** — pure logic, no Docker, no Cloudflare:

- `parse_kv_url("https://abc.trycloudflare.com")` → `"https://abc.trycloudflare.com"`.
- `build_kv_put_url(account, ns, "current")` → exact expected Cloudflare API URL.
- `verify_kv_roundtrip(monkeypatched_curl)` → asserts that a mismatched GET-back is rejected.
- `health_probe_passes(200)` / `health_probe_fails(502)` — using `monkeypatch` over `subprocess.run`.

**Shell-level test (manual, but scripted):**

- `tests/manual/test-start-tunnel-e2e.sh` — runs the actual `start-tunnel.sh` against a `localstack`-style stub, asserts the script exits 0 and KV was written. Documented as "manual" because it requires real Cloudflare credentials; not in CI.

**Acceptance test (manual, run once on first deploy):**

1. Bring the stack up: `docker compose up -d` + `bash scripts/start-tunnel.sh`.
2. Send a broadcast to `asim@rollick.co.in`.
3. Open the email → click the link → viewer page loads → video plays.
4. `docker compose restart cloudflared`.
5. Wait 30s for `cloudflared` to be ready again.
6. `bash scripts/start-tunnel.sh` (this should re-capture the new URL and update KV).
7. Click the **same email link** again → still loads. (This is the actual robustness proof.)
8. `docker compose restart app`.
9. Click the **same email link** again → still loads.

If steps 7 and 9 both pass, the design is correct.

## Migration / rollout

1. Land the spec + plan + `worker/` skeleton (Worker that just returns a placeholder 503). No behavior change yet — `start-tunnel.sh` still writes `BASE_PUBLIC_URL` to `.env` until step 4 lands.
2. Operator runs §7 setup checklist steps 1-6 (deploy Worker, get credentials, fill `.env`'s `CF_*`).
3. Operator runs §7 step 8 (set the workers.dev URL in `/admin/settings`).
4. Land the modified `start-tunnel.sh` (no more `.env` write, no more app recreate, just KV PUT + verify + probe).
5. Operator runs `bash scripts/start-tunnel.sh` — first run after the script change:
   - Captures trycloudflare URL (as today).
   - PUTs to KV (new).
   - Verifies and probes (new).
   - **Does not** touch `.env` or recreate `app`.
6. Confirm acceptance test (§9) passes.
7. Subsequent restarts (any kind) just work; `start-tunnel.sh` only needs to be re-run if `cloudflared` (re)started.

If for any reason the operator wants to roll back to the old behavior:
- Restore the prior `start-tunnel.sh` from git: `git checkout HEAD~1 -- scripts/start-tunnel.sh`.
- Open `/admin/settings`, change "Public base URL" back to the trycloudflare URL.
- `docker compose up -d --force-recreate app` (not strictly required since the runtime override would also work, but mirrors the old "blast .env and restart" semantics).

## Out of scope (explicit deferrals)

- Auto-running `start-tunnel.sh` on host boot (systemd / launchd unit). Manual re-run after PC reboot is acceptable for v1.
- Worker-side health probe before 307 (see §6).
- Multiple Workers / regions / failover.
- Migrating the dead trycloudflare URLs out of the SQLite `broadcast_links.token` table or any historical analytics.
- Sending new viewer links to recipients of past broadcasts (user explicitly opted out).
- Cloudflare Access / Zero Trust in front of the Worker (the link itself is the auth — the token is unguessable).
