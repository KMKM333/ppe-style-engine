"""
migrate_add_render_style_ref_source.py — own the reference clip.

The first version of the reference field stored only the id the generation
provider gave back. That is a pointer into somebody else's storage: if the
provider expires the media, or the work moves to a different provider, the
style names a clip nobody can fetch, and the style stops being reproducible.

So the canonical copy is ours — bytes in the engine's own asset store — and
the provider id becomes a cache of "where we already uploaded this". The
source video and timestamps are recorded too, so the clip can be re-cut from
the cached original if both copies are ever lost.

Safe to re-run: skips a column already present.

Usage:
    python3 migrate_add_render_style_ref_source.py
"""
from db_init import get_conn

ADDED = [
    ("render_styles", "reference_url", "TEXT"),        # our copy — canonical
    ("render_styles", "reference_source", "TEXT"),     # which video it was cut from
    ("render_styles", "reference_start_sec", "REAL"),
    ("render_styles", "reference_end_sec", "REAL"),
]


def run():
    conn = get_conn()
    for table, column, coltype in ADDED:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            print(f"[migrate_add_render_style_ref_source] added {table}.{column}")
    conn.commit()
    conn.close()
    print("[migrate_add_render_style_ref_source] ready.")


if __name__ == "__main__":
    run()
