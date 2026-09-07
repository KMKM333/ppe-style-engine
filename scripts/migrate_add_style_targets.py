"""
migrate_add_style_targets.py — the measured numbers a render must hit.

A style already says what it should LOOK like in words. It could not say what it
should MEASURE, so nothing could check a render against it, and nothing did.

Scoring the renders we had showed why that matters: saturation was the largest
error term in every one of them, and it pointed the wrong way. Renders aimed at
@johnnyharris (measured 0.29, desaturated archival) came out at 0.65-0.72, while
renders aimed at @guijooorge (measured 0.70, loud flat vector) came out at
0.30-0.39. The two accounts had each other's saturation, and no stage of the
pipeline was in a position to notice.

These columns hold what the account's own clips measure, so a correction has
something to aim at. They are written by the ppe-reference-style loop, not by
hand — a hand-typed target is the eyeballed palette problem again.

Safe to re-run: each column is added only if missing.

Usage:
    python3 migrate_add_style_targets.py
"""
from db_init import get_conn

COLUMNS = [
    ("render_styles", "target_saturation", "REAL"),   # mean area-weighted HSV S
    ("render_styles", "target_flatness", "REAL"),     # share of frame in top 3 colours
    ("render_styles", "target_dark_area", "REAL"),    # dark AND unsaturated share
    ("render_styles", "targets_measured_at", "TEXT"), # when, and from how many clips
]


def run():
    conn = get_conn()
    for table, col, decl in COLUMNS:
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if col not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            print(f"[migrate_add_style_targets] + {table}.{col}")
    conn.commit()
    conn.close()
    print("[migrate_add_style_targets] ready.")


if __name__ == "__main__":
    run()
