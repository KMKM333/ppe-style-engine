"""
migrate_add_production_creation_format_profile.py — lets a production spec
creation record the P+S format profile (PVS.*) it was built against.

A production spec already carries two profiles: the voice it should sound
like (A.*) and the pacing it should be cut to (PS.*). Neither says how the
words and pictures should RELATE — whether the visuals illustrate the
script, carry the meaning alone, or are themselves the content. That is
exactly what the PVS layer measures, and it is the difference between a
spec that can be executed and one that still needs a human to decide the
format.

Optional by design: a spec built before the P+S layer existed, or for an
account with no PVS profile yet, is still a valid spec.

Safe to re-run: skips the column if it is already there.

Usage:
    python3 migrate_add_production_creation_format_profile.py
"""
from db_init import get_conn

ADDED_COLUMNS = [
    ("production_spec_creations", "format_profile_id",
     "INTEGER REFERENCES format_profiles(format_profile_id)"),
]


def run():
    conn = get_conn()
    for table, column, coltype in ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            print(f"[migrate_add_production_creation_format_profile] added {table}.{column}")
    conn.commit()
    conn.close()
    print("[migrate_add_production_creation_format_profile] ready.")


if __name__ == "__main__":
    run()
