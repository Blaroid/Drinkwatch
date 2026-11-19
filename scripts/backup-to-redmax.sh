#!/usr/bin/env bash
# Nightly database dump to the redmax backup disk.
#
# Lives on the backup disk rather than in git: it is the shop's price ledger, and
# a laptop-sized repo should not carry 40 MB of SQL every week.
set -euo pipefail
cd "$HOME/drinkwatch"
REMOTE="$HOME/mnt/redmax/drinkwatch/backups"
[ -d "$HOME/mnt/redmax" ] || { echo "redmax is not mounted; skipping backup" >&2; exit 0; }
mkdir -p "$REMOTE"
STAMP="$(date +%Y%m%d-%H%M)"
FILE="$REMOTE/drinkwatch-$STAMP.sql.gz"
docker compose exec -T db pg_dump -U drinkwatch drinkwatch | gzip > "$FILE"
SIZE=$(stat -c %s "$FILE")
if [ "$SIZE" -lt 1024 ]; then
  echo "dump looks empty ($SIZE bytes); keeping it for inspection" >&2
  exit 1
fi
# fourteen days of history
find "$REMOTE" -name 'drinkwatch-*.sql.gz' -mtime +14 -delete
echo "backup: $FILE ($SIZE bytes)"
