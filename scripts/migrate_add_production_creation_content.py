"""
migrate_add_production_creation_content.py — adds the generated-content
columns to production_spec_creations, upgrading it from a manual
title+link tracker into a real generation target (mirrors what
`transformations` is for Studio's Creations, just with a richer shape:
a beat sheet + pacing/shot-mix numbers instead of one script blob).

dek/beats_json/production_notes_json hold Claude's generated content.
target_runtime_sec/target_shot_count_min/max are computed server-side
from the beats (not the LLM) once generated. status tracks
draft -> generated / failed, same vocabulary as production_spec_inputs.status.

Safe to re-run: skips any column that already exists.

Usage:
    python3 migrate_add_production_creation_content.py
"""
from db_init import get_conn

NEW_COLUMNS = [
    ("dek", "TEXT"),
    ("beats_json", "TEXT"),
    ("production_notes_json", "TEXT"),
    ("target_runtime_sec", "REAL"),
    ("target_shot_count_min", "INTEGER"),
    ("target_shot_count_max", "INTEGER"),
    ("status", "TEXT DEFAULT 'draft'"),
    ("generation_error", "TEXT"),
    ("generated_at", "TEXT"),
]


def run():
    conn = get_conn()
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(production_spec_creations)").fetchall()}
    for name, coltype in NEW_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE production_spec_creations ADD COLUMN {name} {coltype}")
            print(f"Added production_spec_creations.{name}")
        else:
            print(f"production_spec_creations.{name} already exists")
    conn.commit()
    conn.close()


if __name__ == "__main__":
    run()
