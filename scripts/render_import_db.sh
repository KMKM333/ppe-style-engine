#!/bin/bash
# Uploads a local database backup onto your Render service's persistent disk.
# Usage: ./render_import_db.sh <ssh-target> <local-db-file>
#   <ssh-target> looks like: srv-xxxxx-01@ssh.oregon.render.com
#   Find it in the Render dashboard: your service -> Connect -> SSH tab.
set -e
TARGET="$1"
LOCAL_FILE="$2"

if [ -z "$TARGET" ] || [ -z "$LOCAL_FILE" ]; then
  echo "Usage: $0 <ssh-target> <local-db-file>"
  exit 1
fi
if [ ! -f "$LOCAL_FILE" ]; then
  echo "File not found: $LOCAL_FILE"
  exit 1
fi

ssh "$TARGET" "cat > /app/db/ppe_engine.db" < "$LOCAL_FILE"
echo "Uploaded $LOCAL_FILE to the live service."
echo "Restart the service from the Render dashboard so it picks up the new data."
