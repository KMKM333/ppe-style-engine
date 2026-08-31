"""
migrate_add_production_creation_source_kinds.py — generalizes
production_spec_creations' source from "always a whole video" to any of:
whole video, one video/book chapter, a whole book, or an existing Studio
Creation (transformations row) — so a production spec can be built from a
tighter/more specific source than a full 30-minute video.

source_kind picks which of the id columns below is populated:
  'video'         -> source_video_id
  'video_section' -> source_section_id (a video_sections.section_id)
  'book'          -> source_book_id
  'book_section'  -> source_section_id (a book_sections.section_id)
  'creation'      -> source_transformation_id

source_video_id already existed (used for 'video'); this only adds the rest.

Safe to re-run: skips any column that already exists.

Usage:
    python3 migrate_add_production_creation_source_kinds.py
"""
from db_init import get_conn

NEW_COLUMNS = [
    ("source_kind", "TEXT DEFAULT 'video'"),
    ("source_book_id", "INTEGER REFERENCES books(book_id)"),
    ("source_section_id", "INTEGER"),
    ("source_transformation_id", "INTEGER REFERENCES transformations(transformation_id)"),
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
