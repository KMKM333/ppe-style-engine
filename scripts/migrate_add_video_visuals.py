"""
migrate_add_video_visuals.py — adds the schema needed for the automated
long-form video pipeline: video_attributes.classification_error (so a video
can be marked needs_review like books), videos.timed_transcript_json (cue
timing captured for YouTube videos), and the new video_visuals table
(screenshots/recreated SVGs of on-screen charts/graphs/tables).

Safe to re-run: skips whatever already exists.

Usage:
    python3 migrate_add_video_visuals.py
"""
from db_init import get_conn

NEW_VIDEO_ATTRIBUTES_COLUMNS = [
    ("classification_error", "TEXT"),
]

NEW_VIDEOS_COLUMNS = [
    ("timed_transcript_json", "TEXT"),
]

VIDEO_VISUALS_TABLE = """
CREATE TABLE IF NOT EXISTS video_visuals (
    visual_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id             INTEGER REFERENCES videos(video_id),
    section_id           INTEGER REFERENCES video_sections(section_id),
    timestamp_sec        REAL,
    caption              TEXT,
    recreated_svg        TEXT,
    screenshot_captured  INTEGER DEFAULT 0,
    created_at           TEXT DEFAULT (datetime('now'))
)
"""


def run():
    conn = get_conn()
    added = 0

    for table, columns in (("video_attributes", NEW_VIDEO_ATTRIBUTES_COLUMNS), ("videos", NEW_VIDEOS_COLUMNS)):
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, sqltype in columns:
            if col in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {sqltype}")
            print(f"Added {table}.{col}")
            added += 1

    conn.execute(VIDEO_VISUALS_TABLE)
    conn.commit()
    conn.close()
    print(f"{added} column(s) added; video_visuals table ensured.")


if __name__ == "__main__":
    run()
