"""
migrate_add_render_style_aspect.py — the frame is part of the look.

A render style carried its palette, its medium and its caption treatment but
not its SHAPE, and shape is not a detail: Guijooorge works in 16:9 landscape
with stage-set framing and generous negative space, while every other style in
the system is vertical. Rendering his look into a 9:16 frame reproduces the
palette and loses the composition.

Safe to re-run: skips a column already present.

Usage:
    python3 migrate_add_render_style_aspect.py
"""
from db_init import get_conn

ADDED = [("render_styles", "aspect", "TEXT")]   # e.g. "16:9" / "9:16"


def run():
    conn = get_conn()
    for table, column, coltype in ADDED:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            print(f"[migrate_add_render_style_aspect] added {table}.{column}")
    conn.commit()
    conn.close()
    print("[migrate_add_render_style_aspect] ready.")


if __name__ == "__main__":
    run()
