#!/usr/bin/env bash
# Manual SQLite backup (no Docker).
# Usage:  ./scripts/backup.sh
# Cron example (nightly at 02:30):
#   30 2 * * * /opt/rollick-broadcaster/scripts/backup.sh
set -euo pipefail

cd "$(dirname "$0")/.."

DB="${DATABASE_URL:-broadcaster.db}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT="backups/broadcaster-${TS}.db"

mkdir -p backups
if [[ ! -f "$DB" ]]; then
  echo "[backup] $DB not found — nothing to do" >&2
  exit 0
fi

sqlite3 "$DB" ".backup $OUT"
echo "[backup] wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Keep only the last 14 days
find backups -name 'broadcaster-*.db' -mtime +14 -delete
