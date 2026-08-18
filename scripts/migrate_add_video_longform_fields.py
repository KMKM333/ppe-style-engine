"""
migrate_add_video_longform_fields.py — adds video_attributes columns that
only make sense for long-form (YouTube, 20-40+ min) content: cold opens,
sponsor segments, act structure, re-engagement hooks, richer outros, and
topic range. All nullable — short-form Instagram rows stay NULL forever,
same pattern as every other media-specific field already on this table.

Safe to re-run: skips whatever columns already exist.

Usage:
    python3 migrate_add_video_longform_fields.py
"""
from db_init import get_conn

NEW_COLUMNS = [
    ("chapter_count", "INTEGER"),              # auto, from yt-dlp chapter metadata at ingest time
    ("has_chapters", "INTEGER"),                # auto, boolean 0/1
    ("cold_open_present", "INTEGER"),           # class, boolean 0/1
    ("intro_length_sec", "REAL"),               # class
    ("sponsor_segment_present", "INTEGER"),     # auto, boolean 0/1
    ("sponsor_segment_position", "TEXT"),       # auto, enum: early/mid/late/none
    ("act_count", "INTEGER"),                   # class
    ("re_engagement_hook_count", "INTEGER"),    # class
    ("outro_cta_count", "INTEGER"),             # auto
    ("outro_type", "TEXT"),                     # class, enum
    ("pacing_arc", "TEXT"),                     # class, enum
    ("topic_shift_count", "INTEGER"),           # class
]


def run():
    conn = get_conn()
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(video_attributes)").fetchall()}
    added = 0
    for col, sqltype in NEW_COLUMNS:
        if col in existing:
            continue
        conn.execute(f"ALTER TABLE video_attributes ADD COLUMN {col} {sqltype}")
        print(f"Added video_attributes.{col}")
        added += 1
    conn.commit()
    conn.close()
    print(f"{added} column(s) added, {len(NEW_COLUMNS) - added} already present.")


if __name__ == "__main__":
    run()
