#!/bin/sh
set -e

# Every migration below is idempotent and, on an existing database, almost
# always a no-op. They are still allowed to FAIL without taking the site
# down: with a full data disk (1 GB, shared with every frame and book page)
# a migration's journal write fails, and under a bare set -e that turned
# into a crash loop — a 502 on every route, including the one that would
# have shown the disk was full. Better to boot read-only and be diagnosable.

cd /app/scripts
# Safe to run on every boot: every table uses CREATE TABLE IF NOT EXISTS and
# every seed row uses INSERT OR IGNORE, so this never touches existing data —
# it only fills in whatever's missing (a fresh disk, or a table added by a
# later schema.sql update). PPE_DB_PATH (set in Render's environment) points
# this at the mounted disk instead of the image's built-in db/ directory.
python3 db_init.py || echo "WARNING: db_init.py failed — continuing so the site still serves; see /api/health"

# CREATE TABLE IF NOT EXISTS is a no-op once a table already exists, so new
# columns added to an existing table (like book_attributes here) need an
# explicit migration. Also safe to run on every boot: it skips any column
# that's already present.
python3 migrate_add_style_craft_attrs.py || echo "WARNING: migrate_add_style_craft_attrs.py failed — continuing so the site still serves; see /api/health"

# CTA (has_cta/cta_type/cta_placement/cta_count) is now a heuristic Auto
# attribute like title_format, computed at ingest — this only backfills
# video_attributes rows from before that heuristic existed (cta_type IS
# NULL), so it's safe on every boot and never overwrites a real
# classification.
python3 backfill_cta_heuristic.py || echo "WARNING: backfill_cta_heuristic.py failed — continuing so the site still serves; see /api/health"

# Same idempotent-column-add pattern as migrate_add_style_craft_attrs.py,
# for the column auto_process_book.py uses to record why an automatic
# classification attempt needed human review.
python3 migrate_add_book_classification_error.py || echo "WARNING: migrate_add_book_classification_error.py failed — continuing so the site still serves; see /api/health"

# Adds the cross-media attribute columns (video_attributes + book_attributes)
# — same idempotent pattern, safe on every boot.
python3 migrate_add_cross_media_attrs.py || echo "WARNING: migrate_add_cross_media_attrs.py failed — continuing so the site still serves; see /api/health"

# Adds book_examples.screenshot_page_num, used to render a matched PDF page
# image inline on an example (see match_book_screenshots.py) — same
# idempotent pattern, safe on every boot.
python3 migrate_add_screenshot_page_num.py || echo "WARNING: migrate_add_screenshot_page_num.py failed — continuing so the site still serves; see /api/health"

# Adds the video_sections chapter layer (+ optional section_id on
# video_points/video_terms/video_examples), needed for long-form YouTube
# content — same idempotent pattern, safe on every boot.
python3 migrate_add_video_sections.py || echo "WARNING: migrate_add_video_sections.py failed — continuing so the site still serves; see /api/health"

# Adds video_attributes columns specific to long-form structure (cold
# opens, sponsor segments, act count, outro type, pacing arc...) — same
# idempotent pattern, safe on every boot.
python3 migrate_add_video_longform_fields.py || echo "WARNING: migrate_add_video_longform_fields.py failed — continuing so the site still serves; see /api/health"

# Adds video_attributes.classification_error, videos.timed_transcript_json,
# and the video_visuals table (screenshots/recreated SVGs of on-screen
# charts/graphs/tables) for the automated long-form video pipeline — same
# idempotent pattern, safe on every boot.
python3 migrate_add_video_visuals.py || echo "WARNING: migrate_add_video_visuals.py failed — continuing so the site still serves; see /api/health"

# Adds swipe_candidates, storage for the /swipe mechanism's generated
# pitches — same idempotent pattern, safe on every boot.
python3 migrate_add_swipe_candidates.py || echo "WARNING: migrate_add_swipe_candidates.py failed — continuing so the site still serves; see /api/health"

# Adds swipe_candidates.sources_json, needed for multi-source (2-3 input)
# swipe candidates — same idempotent pattern, safe on every boot.
python3 migrate_add_swipe_sources_json.py || echo "WARNING: migrate_add_swipe_sources_json.py failed — continuing so the site still serves; see /api/health"

# Adds the Production Spec pipeline tables (production_spec_inputs/shots/
# attributes + video_creations) — these were previously only ever created
# by a manual one-off run against the live disk, so a fresh disk would be
# missing them; same idempotent pattern, safe on every boot.
python3 migrate_add_production_spec.py || echo "WARNING: migrate_add_production_spec.py failed — continuing so the site still serves; see /api/health"

# Adds production_spec_creations, the written-production-spec-document
# tracker (e.g. "The Militia Divide") — same idempotent pattern, safe on
# every boot.
python3 migrate_add_production_spec_creations.py || echo "WARNING: migrate_add_production_spec_creations.py failed — continuing so the site still serves; see /api/health"

# Adds production_spec_creations' generated-content columns (dek,
# beats_json, production_notes_json, target runtime/shot-count, status),
# upgrading it from a manual link tracker into a real generation target —
# same idempotent pattern, safe on every boot.
python3 migrate_add_production_creation_content.py || echo "WARNING: migrate_add_production_creation_content.py failed — continuing so the site still serves; see /api/health"

# Generalizes production_spec_creations' source from "always a whole
# video" to a video/book chapter or an existing Studio Creation — same
# idempotent pattern, safe on every boot.
python3 migrate_add_production_creation_source_kinds.py || echo "WARNING: migrate_add_production_creation_source_kinds.py failed — continuing so the site still serves; see /api/health"

# One-time content backfill for the pre-existing "The Militia Divide" row
# (predates the generation pipeline) — transcribes its real hand-authored
# beat sheet in, rather than leaving it stuck on "Not generated yet."
# Safe on every boot: only touches the row if beats_json is still NULL.
python3 migrate_backfill_militia_divide_creation.py || echo "WARNING: migrate_backfill_militia_divide_creation.py failed — continuing so the site still serves; see /api/health"

# Adds the Production Inputs (P+S) layer: format_profiles (PVS.*) +
# format_profile_attributes, describing how a creator's visuals and script
# relate. Seeds five preliminary profiles. Safe on every boot: CREATE TABLE
# IF NOT EXISTS + INSERT OR IGNORE, so it never overwrites a value a real
# classification pass has since refined.
python3 migrate_add_format_profiles.py || echo "WARNING: migrate_add_format_profiles.py failed — continuing so the site still serves; see /api/health"

# Per-video storage for the P+S pipeline (format_inputs / frames / readings),
# so a format reading is recorded per VIDEO and aggregated into the profile —
# same idempotent pattern, safe on every boot.
python3 migrate_add_format_inputs.py || echo "WARNING: migrate_add_format_inputs.py failed — continuing so the site still serves; see /api/health"

# Lets a production spec creation record the PVS format profile it was built
# against, so a spec can say how words and pictures should relate and not just
# how it should sound and be cut — same idempotent pattern, safe on every boot.
python3 migrate_add_production_creation_format_profile.py || echo "WARNING: migrate_add_production_creation_format_profile.py failed — continuing so the site still serves; see /api/health"

# Stores the LOOK of a production account — palette, art style, composition,
# typography — derived from its own stored frames. Neither the PS nor the PVS
# profile captures it, so nothing could reproduce an account's visual
# identity. Same idempotent pattern, safe on every boot.
python3 migrate_add_visual_style_brief.py || echo "WARNING: migrate_add_visual_style_brief.py failed — continuing so the site still serves; see /api/health"

# Lets a finished video point back at the spec it was assembled from —
# video_creations predates the spec pipeline and could only describe a video
# made by hand from a reference. Same idempotent pattern, safe on every boot.
python3 migrate_add_video_creation_spec_link.py || echo "WARNING: migrate_add_video_creation_spec_link.py failed — continuing so the site still serves; see /api/health"

# Measured editing, sound and movement per video — computed with ffmpeg from
# the cached source, so it costs nothing and is recomputable. Same idempotent
# pattern, safe on every boot.
python3 migrate_add_video_timelines.py || echo "WARNING: migrate_add_video_timelines.py failed — continuing so the site still serves; see /api/health"

exec gunicorn --bind "0.0.0.0:${PORT:-8080}" --worker-class gthread --workers 2 --threads 4 --timeout 120 webapp:app
