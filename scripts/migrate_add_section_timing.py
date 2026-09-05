"""
migrate_add_section_timing.py — chapters need to know WHERE they are.

video_sections records a chapter's order, title, summary and topics, but not
its position in the video. That is enough to read a breakdown and not enough
to do anything with the footage: sampling frames from chapter 3, or cutting a
clip of it, needs a start and an end.

method records HOW a boundary was arrived at — matched against the cue-level
transcript, or allocated by each chapter's share of the words — because a
matched boundary is worth trusting and an allocated one is worth checking,
and a page that cannot tell them apart presents a guess as a measurement.

Safe to re-run: skips any column already present.

Usage:
    python3 migrate_add_section_timing.py
"""
from db_init import get_conn

ADDED_COLUMNS = [
    ("video_sections", "start_sec", "REAL"),
    ("video_sections", "end_sec", "REAL"),
    ("video_sections", "timing_method", "TEXT"),      # matched / allocated
    ("video_sections", "timing_confidence", "REAL"),  # 0-1, how well the match scored
]


def run():
    conn = get_conn()
    for table, column, coltype in ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            print(f"[migrate_add_section_timing] added {table}.{column}")
    conn.commit()
    conn.close()
    print("[migrate_add_section_timing] ready.")


if __name__ == "__main__":
    run()
