"""
migrate_add_video_sections.py — adds a chapter/section layer to video, for
long-form YouTube content. Short-form Instagram scripts never needed this
(a 60-second clip has no chapters), but a 30-40 minute video's points/terms/
examples deserve the same section-scoped structure books already get.

Mirrors book_sections exactly, minus book-only fields (no diagram_svg — that
was built for illustrating a book's conceptual model, not video structure).
video_points/video_terms/video_examples each get a nullable section_id FK:
NULL means "not attributed to a chapter" (the only state possible for
existing short-form rows), matching the pattern book_examples already uses
for its own unsectioned rows.

Safe to re-run: skips whatever already exists.

Usage:
    python3 migrate_add_video_sections.py
"""
from db_init import get_conn


def run():
    conn = get_conn()

    existing_tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "video_sections" not in existing_tables:
        conn.execute("""
            CREATE TABLE video_sections (
                section_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id          INTEGER REFERENCES videos(video_id),
                section_number     INTEGER,
                section_title       TEXT NOT NULL,
                summary               TEXT,
                topics                 TEXT,
                created_at               TEXT DEFAULT (datetime('now'))
            )
        """)
        print("Created video_sections")
    else:
        print("video_sections already present — nothing to do.")

    for table in ("video_points", "video_terms", "video_examples"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "section_id" in cols:
            print(f"{table}.section_id already present — nothing to do.")
        else:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN section_id INTEGER REFERENCES video_sections(section_id)")
            print(f"Added {table}.section_id")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    run()
