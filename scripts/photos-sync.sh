#!/usr/bin/env bash
# Mirror price-tag photos to the redmax backup disk (rsync is not installed on
# powermax, so this uses cp). Local disk is a cache; redmax is canonical.
#
# Deleting a local copy must not leave a row advertising a URL that stops working,
# so rows are marked `pruned_at` first and files are only removed once that update
# has committed. If the database cannot be reached, the cache is kept and retried on
# the next run instead of being pruned without a mark.
set -euo pipefail
LOCAL="$HOME/drinkwatch/photos"
REMOTE="$HOME/mnt/redmax/drinkwatch/photos"
COMPOSE_DIR="${DRINKWATCH_DIR:-$HOME/drinkwatch}"
KEEP_DAYS=14
[ -d "$LOCAL" ] || exit 0
[ -d "$REMOTE" ] || exit 0

copied=0
while IFS= read -r -d "" file; do
  rel="${file#$LOCAL/}"
  target="$REMOTE/$rel"
  [ -f "$target" ] && continue
  mkdir -p "$(dirname "$target")"
  cp -p "$file" "$target"
  copied=$((copied + 1))
done < <(find "$LOCAL" -type f -print0 2>/dev/null)
[ "$copied" -gt 0 ] && echo "synced $copied photo(s)"

# Mark the rows behind the files about to go. Thumbnails share their photo's row, so
# only the full-size name is matched; image names are posix paths relative to LOCAL.
mark_pruned() {
  local sql="" rel
  for rel in "$@"; do
    case "$rel" in *"_thumb.jpg") continue ;; esac
    rel=${rel//\'/\'\'}
    sql+="${sql:+,}'$rel'"
  done
  [ -n "$sql" ] || return 0
  # -1: one transaction, so a partial update can never hide a row behind a deleted file.
  docker compose --project-directory "$COMPOSE_DIR" -f "$COMPOSE_DIR/docker-compose.yml" \
    exec -T db psql -qAt -1 -v ON_ERROR_STOP=1 -U drinkwatch -d drinkwatch <<SQL
update photos set pruned_at = now() where pruned_at is null and filename in ($sql);
-- observations keep the URL they were submitted with; clear it only where it is this
-- app's own URL for a pruned photo, so a client is never handed a dead link again.
update observations o
   set photo_url = null
  from photos p
 where p.id = o.photo_id
   and p.pruned_at is not null
   and o.photo_url like '%/v1/photos/' || p.token || '%';
SQL
}

# keep a 14 day local cache
candidates=()
while IFS= read -r -d "" file; do
  rel="${file#$LOCAL/}"
  [ -f "$REMOTE/$rel" ] || continue
  candidates+=("$rel")
done < <(find "$LOCAL" -type f -mtime +"$KEEP_DAYS" -print0 2>/dev/null)

if [ "${#candidates[@]}" -gt 0 ]; then
  if mark_pruned "${candidates[@]}"; then
    for rel in "${candidates[@]}"; do
      rm -f "$LOCAL/$rel"
    done
    echo "pruned ${#candidates[@]} local file(s)"
  else
    echo "could not mark pruned photos; keeping local cache" >&2
  fi
fi
exit 0
