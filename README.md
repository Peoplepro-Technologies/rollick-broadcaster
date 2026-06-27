# Rollick Broadcaster

Full-stack broadcast app: an admin schedules broadcasts (title + media + target group); each target subscriber gets a unique link; clicking the link plays the media and lets them leave an anonymous comment. Everything lives on one server.

See [`features.md`](./features.md) for the admin surface spec and [`BUILD_PLAN.md`](./BUILD_PLAN.md) for the build plan with rationale.

## Quick start

```bash
# 1. Setup
cp .env.example .env
# Edit .env: change ADMIN_PASSWORD, SESSION_SECRET, IP_HASH_PEPPER, MEDIA_SIGN_SECRET

# 2. Install + run
pip install -r requirements.txt
uvicorn app:app --reload

# 3. Open
#    Admin login:  http://localhost:8000/admin/login
#    Health:       http://localhost:8000/api/health
#    API docs:     http://localhost:8000/api/docs
```

## Docker

```bash
docker build -t rollick-broadcaster .
docker run -p 8000:8000 --env-file .env -v $(pwd)/broadcaster.db:/app/broadcaster.db -v $(pwd)/uploads:/app/uploads rollick-broadcaster
```

## Tests

```bash
pytest -v
```

Each test uses a fresh SQLite DB in a tmp directory; the dev DB (`broadcaster.db`) is never touched.

## Project layout

```
app.py                          ← FastAPI shell, router wiring
broadcaster/
  settings.py                   ← Pydantic env loader
  db.py                         ← SQLite + schema DDL
  models/                       ← pydantic schemas (per phase)
  routes/                       ← FastAPI routers (per phase)
  services/                     ← business logic (whatsapp, email, links, antispam)
  templates/
    base.html                   ← shared layout
    admin/                      ← admin pages
    viewer/                     ← public /v/{token} pages
static/
  css/tokens.css                ← design tokens
  css/admin.css, viewer.css
  js/                           ← small per-page enhancements
tests/                          ← pytest, httpx.AsyncClient
scripts/                        ← seed.py, future migrate_from_content_scheduler.py
```

## Build phases

See [`BUILD_PLAN.md`](./BUILD_PLAN.md) §6 for the full phased plan. Current state: **Phase 0 — Scaffold** (project skeleton, schema, health endpoint, login placeholder).
