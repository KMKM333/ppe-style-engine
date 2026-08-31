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

# Same idempotent-column-add pattern as migrate_add_style_craft_attrs.py,
# for the column auto_process_book.py uses to record why an automatic
# classification attempt needed human review.
python3 migrate_add_book_classification_error.py

# Adds the cross-media attribute columns (video_attributes + book_attributes)
# — same idempotent pattern, safe on every boot.
python3 migrate_add_cross_media_attrs.py

# Adds book_examples.screenshot_page_num, used to render a matched PDF page
# image inline on an example (see match_book_screenshots.py) — same
# idempotent pattern, safe on every boot.
python3 migrate_add_screenshot_page_num.py

# Adds the video_sections chapter layer (+ optional section_id on
# video_points/video_terms/video_examples), needed for long-form YouTube
# content — same idempotent pattern, safe on every boot.
python3 migrate_add_video_sections.py

# Adds video_attributes columns specific to long-form structure (cold
# opens, sponsor segments, act count, outro type, pacing arc...) — same
# idempotent pattern, safe on every boot.
python3 migrate_add_video_longform_fields.py

# Adds video_attributes.classification_error, videos.timed_transcript_json,
# and the video_visuals table (screenshots/recreated SVGs of on-screen
# charts/graphs/tables) for the automated long-form video pipeline — same
# idempotent pattern, safe on every boot.
python3 migrate_add_video_visuals.py

# Adds swipe_candidates, storage for the /swipe mechanism's generated
# pitches — same idempotent pattern, safe on every boot.
python3 migrate_add_swipe_candidates.py

# Adds swipe_candidates.sources_json, needed for multi-source (2-3 input)
# swipe candidates — same idempotent pattern, safe on every boot.
python3 migrate_add_swipe_sources_json.py

exec gunicorn --bind "0.0.0.0:${PORT:-8080}" --worker-class gthread --workers 2 --threads 4 --timeout 120 webapp:app
