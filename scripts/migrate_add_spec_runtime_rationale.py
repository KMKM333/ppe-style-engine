"""
migrate_add_spec_runtime_rationale.py — why a spec is the length it is.

Runtime used to be whatever fell out of writing the beats, which is how a
46-second spec became a 95-second video: nothing chose a length, so nothing
was written to one. The spec now picks a runtime between 40 and 120 seconds
as a judgement about the material and says why.

target_runtime_sec already exists and stays the authority — it is summed
from the beats the renderer actually cuts to. This records the reasoning
beside it, and the length the model said it was aiming for, so a spec that
argued for 90 seconds and then wrote 140 is visible as a miss rather than
passing silently.

Safe to re-run: skips any column already present.

Usage:
    python3 migrate_add_spec_runtime_rationale.py
"""
from db_init import get_conn

ADDED_COLUMNS = [
    ("production_spec_creations", "runtime_rationale", "TEXT"),
    ("production_spec_creations", "runtime_intended_sec", "REAL"),
]


def run():
    conn = get_conn()
    for table, column, coltype in ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            print(f"[migrate_add_spec_runtime_rationale] added {table}.{column}")
    conn.commit()
    conn.close()
    print("[migrate_add_spec_runtime_rationale] ready.")


if __name__ == "__main__":
    run()
