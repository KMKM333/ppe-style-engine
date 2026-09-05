"""
migrate_add_format_input_excluded.py — not every minute of a video is style.

A long-form video is ingested as a run of 70-second segments, and some of
those segments are not editorial content at all: they are the creator
selling something — their own venture, a sponsor, a membership tier. The
first long-form video analysed spent its last five segments on a pricing
table for the host's startup.

Those readings are true, and they describe a sales pitch. Rolled into the
profile they would teach the system that this creator's documentary style
includes membership pricing, which is exactly backwards. So a segment can be
marked excluded: the reading is kept and still visible on its own page, and
aggregate_profile skips it.

segment_kind records what the classifier judged the segment to BE, separately
from whether it is excluded, so a later change of policy can re-decide
without re-classifying (and paying again).

Safe to re-run: skips any column already present.

Usage:
    python3 migrate_add_format_input_excluded.py
"""
from db_init import get_conn

ADDED_COLUMNS = [
    ("format_inputs", "segment_kind", "TEXT"),       # editorial / promotional
    ("format_inputs", "excluded", "INTEGER DEFAULT 0"),
    ("format_inputs", "exclusion_reason", "TEXT"),
]


def run():
    conn = get_conn()
    for table, column, coltype in ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            print(f"[migrate_add_format_input_excluded] added {table}.{column}")
    conn.commit()
    conn.close()
    print("[migrate_add_format_input_excluded] ready.")


if __name__ == "__main__":
    run()
