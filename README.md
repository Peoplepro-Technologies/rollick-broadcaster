# Rollick Broadcaster

Full-stack broadcast app: an admin schedules broadcasts (title + media + target group); each target subscriber gets a unique link; clicking the link plays the media and lets them leave an anonymous comment. Everything lives on one server.

See [`features.md`](./features.md) for the admin surface spec and [`BUILD_PLAN.md`](./BUILD_PLAN.md) for the build plan with rationale.

## Quick start (Docker — recommended)

```bash
# 1. Configure secrets
cp .env.example .env
# Edit .env: at minimum change ADMIN_PASSWORD, SESSION_SECRET,
# IP_HASH_PEPPER. Use 32+ random chars for SESSION_SECRET.

# 2. Boot
docker compose up -d

# 3. Open
#    Admin login:  http://localhost:8123/admin/login
#    Health:       http://localhost:8123/api/health
#    API docs:     http://localhost:8123/api/docs
```

**Useful commands:**
```bash
docker compose logs -f app             # tail logs
docker compose exec app bash          # shell into the container
docker compose restart app            # restart after .env change
docker compose down                   # stop (keeps volumes + DB)
docker compose down -v                # ⚠️  stop + delete volumes (DB is gone)
```

### Optional: nightly SQLite backup

```bash
docker compose --profile backup up -d
```

This adds a sidecar that runs `sqlite3 .backup` every 24h, writes to `./backups/`, and prunes anything older than 14 days.

### Optional: stable public URL via Cloudflare Worker

By default the BROADCASTER is reachable only at `http://localhost:8123`. To send
broadcasts whose links work from any network, the app needs a public URL.
A Cloudflare **Quick Tunnel** (`*.trycloudflare.com`) is free and zero-config,
but the URL changes every time `cloudflared` restarts — breaking every
previously-sent email.

This repo ships a small Cloudflare Worker that solves that: it gives the
BROADCASTER a **stable `*.workers.dev` URL** that 307-redirects to the
current tunnel URL. Old emails keep working across Docker / PC / cloudflared
restarts.

```bash
# 1. Deploy the Worker (one-time, ~5 min)
cd worker
npm install -g wrangler          # if you don't have it
wrangler login
wrangler kv:namespace create BACKEND_URLS
# → copy the printed id into wrangler.toml (replaces the placeholder)
wrangler deploy
# → copy the printed https://<subdomain>.<account>.workers.dev URL

# 2. Wire the BROADCASTER to it
#    Add the URL + credentials to .env (see .env.example "Cloudflare Worker"
#    section) and set the same URL in /admin/settings as "Public base URL".

# 3. Bring up the tunnel + KV registration
cd ..
bash scripts/start-tunnel.sh     # idempotent; re-run after any cloudflared restart
```

Full setup walkthrough: [`worker/README.md`](./worker/README.md).

## Quick start (local dev, no Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env: change ADMIN_PASSWORD, SESSION_SECRET, IP_HASH_PEPPER
uvicorn app:app --reload
```

## Tests

```bash
pytest -v
```

Each test uses a fresh SQLite DB in a tmp directory; the dev DB is never touched. The conftest also resets the APScheduler singleton between tests.

### Smoke test (end-to-end)

Runs the full pipeline in-process against a fresh temp DB:

```bash
python scripts/smoke.py            # 11/11 steps on a healthy install
python scripts/smoke.py --keep-on-fail  # keep tmpdir for debugging
```

Covers: login → seed users → text content → broadcast (auto-mints links) → send (MockSender writes to `sent_log/`) → viewer GET (records view) → anonymous comment POST (passes honeypot + time-to-fill) → analytics counters → link revocation.

## Production checklist

When deploying beyond local dev, complete these steps in order:

- [ ] Set strong `ADMIN_PASSWORD`, `SESSION_SECRET` (32+ random chars), `IP_HASH_PEPPER`
- [ ] Run behind HTTPS (Caddy / nginx + Let's Encrypt). Enable `https_only=True` on the session middleware in `app.py`
- [ ] Uncomment the HSTS line in `app.py` (only after HTTPS is confirmed)
- [ ] Configure real SMTP (`SMTP_HOST`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`) and/or WhatsApp Business API credentials (`WHATSAPP_PHONE_ID`, `WHATSAPP_ACCESS_TOKEN`)
- [ ] For WhatsApp production sends: register message templates in Meta Business Manager (free-form text only works for customer-initiated; business-initiated requires pre-approved templates — see BUILD_PLAN §8 Q5)
- [ ] Set `BASE_PUBLIC_URL` to the public URL. If you enabled the Cloudflare Worker (above), use the stable `*.workers.dev` URL — **do not** use the `*.trycloudflare.com` URL directly, because it changes on every restart and breaks all previously-sent emails.
- [ ] Mount `./backups` on durable storage if you enabled the backup sidecar
- [ ] Set up log forwarding (the compose file already caps stdout at 10MB × 3 files)

## Project layout

```
app.py                          ← FastAPI shell, lifespan, security headers
broadcaster/
  settings.py                   ← Pydantic env loader
  db.py                         ← SQLite + schema DDL
  security.py                   ← bcrypt password hashing
  routes/                       ← admin_auth, admin_users, admin_groups,
                                   admin_content, admin_broadcasts,
                                   admin_comments, admin_settings, viewer
  services/
    admin.py                    ← bootstrap + auth
    users.py / groups.py / content.py
    broadcasts.py / links.py
    senders.py / whatsapp.py / email.py
    antispam.py                 ← 9 active anti-spam layers
    views.py / comments.py
    analytics.py                ← totals + per-day views + CSV export
    settings.py
    scheduler.py                ← APScheduler for scheduled broadcasts
    privacy.py                  ← rotating IP/UA hash with pepper
  templates/
    base.html, admin/, viewer/
static/
  css/tokens.css, admin.css, viewer.css
  js/users.js (admin form handlers)
tests/                          ← 161 tests, function-scoped DB per test
scripts/                        ← smoke.py (e2e), backup.sh (cron-ready), tunnel_kv.py (CF API client for the start-tunnel script)
docker-compose.yml              ← one-command deploy
worker/                         ← Cloudflare Worker: stable *.workers.dev redirector (optional)
Dockerfile                      ← non-root, healthcheck
```

## Build phases

All 8 build phases + APScheduler integration are complete (see [`BUILD_PLAN.md`](./BUILD_PLAN.md)). 161 tests pass across 13 commits.
