"""
migrate_add_swipe_sources_json.py — adds swipe_candidates.sources_json,
needed for multi-source (2-3 input) swipe candidates. See db/schema.sql
for the column's shape.

Safe to re-run: skips the column if it already exists.

Usage:
    python3 migrate_add_swipe_sources_json.py
"""
from db_init import get_conn


def run():
    conn = get_conn()
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(swipe_candidates)").fetchall()}
    if "sources_json" not in existing:
        conn.execute("ALTER TABLE swipe_candidates ADD COLUMN sources_json TEXT")
        print("Added swipe_candidates.sources_json")
    else:
        print("swipe_candidates.sources_json already exists")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    run()
