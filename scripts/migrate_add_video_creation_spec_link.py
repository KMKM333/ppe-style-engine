"""
migrate_add_video_creation_spec_link.py — lets a finished video point back at
the spec it was built from.

video_creations predates the spec pipeline: it links a video to a source
production INPUT and a target profile, which describes a video made by hand
from a reference. A video assembled from a generated spec has neither of
those — its lineage is the spec, and through it the creation, the profiles
and the visual brief. Without this column an assembled video landed on disk
and appeared nowhere in the engine at all.

Safe to re-run: skips the columns if they are already there.

Usage:
    python3 migrate_add_video_creation_spec_link.py
"""
from db_init import get_conn

ADDED_COLUMNS = [
    ("video_creations", "spec_creation_id",
     "INTEGER REFERENCES production_spec_creations(creation_id)"),
    ("video_creations", "duration_sec", "REAL"),
    ("video_creations", "n_shots", "INTEGER"),
    ("video_creations", "cost_usd", "REAL"),
]


def run():
    conn = get_conn()
    for table, column, coltype in ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            print(f"[migrate_add_video_creation_spec_link] added {table}.{column}")
    conn.commit()
    conn.close()
    print("[migrate_add_video_creation_spec_link] ready.")


if __name__ == "__main__":
    run()
