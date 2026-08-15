#!/bin/sh
set -e

cd /app/scripts
# Safe to run on every boot: every table uses CREATE TABLE IF NOT EXISTS and
# every seed row uses INSERT OR IGNORE, so this never touches existing data —
# it only fills in whatever's missing (a fresh disk, or a table added by a
# later schema.sql update). PPE_DB_PATH (set in Render's environment) points
# this at the mounted disk instead of the image's built-in db/ directory.
python3 db_init.py

# CREATE TABLE IF NOT EXISTS is a no-op once a table already exists, so new
# columns added to an existing table (like book_attributes here) need an
# explicit migration. Also safe to run on every boot: it skips any column
# that's already present.
python3 migrate_add_style_craft_attrs.py

# CTA (has_cta/cta_type/cta_placement/cta_count) is now a heuristic Auto
# attribute like title_format, computed at ingest — this only backfills
# video_attributes rows from before that heuristic existed (cta_type IS
# NULL), so it's safe on every boot and never overwrites a real
# classification.
python3 backfill_cta_heuristic.py

exec gunicorn --bind "0.0.0.0:${PORT:-8080}" --worker-class gthread --workers 2 --threads 4 --timeout 120 webapp:app
