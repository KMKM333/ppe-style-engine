#!/bin/sh
set -e

mkdir -p /app/db
if [ ! -f /app/db/ppe_engine.db ]; then
  echo "No database found on the persistent disk yet — initializing an empty schema."
  echo "Import your real data with scripts/render_import_db.sh once this deploy is live."
  cd /app/scripts
  python3 db_init.py
fi

cd /app/scripts
exec gunicorn --bind "0.0.0.0:${PORT:-8080}" --worker-class gthread --workers 2 --threads 4 --timeout 120 webapp:app
