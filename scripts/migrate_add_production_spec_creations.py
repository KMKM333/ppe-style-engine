"""
migrate_add_production_spec_creations.py — adds production_spec_creations, the
table backing the "Production Specs Creations" page: tracks written production
spec documents (like "The Militia Divide") that combine a content source video,
a writing-style profile, and a Production Spec (shot-pacing) profile, with a
link out to the published document. Separate from video_creations, which
tracks actual rendered video outputs.

Safe to re-run: uses CREATE TABLE/INDEX IF NOT EXISTS.

Usage:
    python3 migrate_add_production_spec_creations.py
"""
from db_init import get_conn

PRODUCTION_SPEC_CREATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS production_spec_creations (
    creation_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title                  TEXT NOT NULL,
    source_video_id          INTEGER REFERENCES videos(video_id),
    style_profile_id            INTEGER REFERENCES style_profiles(profile_id),
    production_profile_id          INTEGER REFERENCES style_profiles(profile_id),
    view_url                          TEXT,
    created_at                           TEXT DEFAULT (datetime('now'))
)
"""


def run():
    conn = get_conn()
    conn.execute(PRODUCTION_SPEC_CREATIONS_TABLE)
    conn.commit()
    conn.close()
    print("production_spec_creations table ensured.")


if __name__ == "__main__":
    run()
