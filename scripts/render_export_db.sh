#!/bin/bash
# Downloads the live database from your Render service to a local backup file.
# Usage: ./render_export_db.sh <ssh-target> [output-file]
#   <ssh-target> looks like: srv-xxxxx-01@ssh.oregon.render.com
#   Find it in the Render dashboard: your service -> Connect -> SSH tab.
set -e
TARGET="$1"
OUT="${2:-../db_backups/ppe_engine_$(date +%Y%m%d_%H%M%S).db}"

if [ -z "$TARGET" ]; then
  echo "Usage: $0 <ssh-target> [output-file]"
  exit 1
fi

mkdir -p "$(dirname "$OUT")"
ssh "$TARGET" "cat /data/ppe_engine.db" > "$OUT"
echo "Saved live database to $OUT ($(du -h "$OUT" | cut -f1))"
