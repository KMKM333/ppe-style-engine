#!/bin/sh
set -e

mkdir -p /app/db
cd /app/scripts
# Safe to run on every boot: every table uses CREATE TABLE IF NOT EXISTS and
# every seed row uses INSERT OR IGNORE, so this never touches existing data —
# it only fills in whatever's missing (a fresh disk, or a table added by a
# later schema.sql update).
python3 db_init.py

exec gunicorn --bind "0.0.0.0:${PORT:-8080}" --worker-class gthread --workers 2 --threads 4 --timeout 120 webapp:app
