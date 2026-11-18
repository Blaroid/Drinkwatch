#!/usr/bin/env bash
# Mirror price-tag photos to the redmax backup disk (rsync is not installed on
# powermax, so this uses cp). Local disk is a cache; redmax is canonical.
set -euo pipefail
LOCAL="$HOME/drinkwatch/photos"
REMOTE="$HOME/mnt/redmax/drinkwatch/photos"
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

# keep a 14 day local cache
while IFS= read -r -d "" file; do
  rel="${file#$LOCAL/}"
  [ -f "$REMOTE/$rel" ] && rm -f "$file"
done < <(find "$LOCAL" -type f -mtime +14 -print0 2>/dev/null)
exit 0
