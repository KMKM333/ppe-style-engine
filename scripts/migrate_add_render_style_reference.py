"""
migrate_add_render_style_reference.py — the reference clip belongs to the style.

A render style described a look in words. Handing a video model ten seconds of
the creator's real work beat every written description of it: the generation
reproduced his palette, his figure construction and his caption plate, and
ignored the six hex values the prompt supplied. The words were the weaker
input, and the clip that produced the good result lived nowhere.

reference_media_id is the id in the generation provider's own media store, and
reference_note records what the clip is and why that stretch — a style whose
reference cannot be found again is a style that cannot be reproduced.

Safe to re-run: skips a column already present.

Usage:
    python3 migrate_add_render_style_reference.py
"""
from db_init import get_conn

ADDED = [
    ("render_styles", "reference_media_id", "TEXT"),
    ("render_styles", "reference_note", "TEXT"),
]


def run():
    conn = get_conn()
    for table, column, coltype in ADDED:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            print(f"[migrate_add_render_style_reference] added {table}.{column}")
    conn.commit()
    conn.close()
    print("[migrate_add_render_style_reference] ready.")


if __name__ == "__main__":
    run()
