"""
migrate_add_screenshot_page_num.py — one-off migration adding
book_examples.screenshot_page_num, which records the matched PDF page number
for an example's inline screenshot (see match_book_screenshots.py and
webapp.py's /api/books/<id>/examples/<id>/screenshot).

Deliberately a new column rather than reusing the existing page_range TEXT
column: page_range already carries legacy data from an earlier classification
era (single pages and ranges like "143-144" as free text), which isn't safe
to feed into the <int:page_num> URL route. Safe to re-run: skips if the
column is already present.

Usage:
    python3 migrate_add_screenshot_page_num.py
"""
from db_init import get_conn


def run():
    conn = get_conn()
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(book_examples)").fetchall()}
    if "screenshot_page_num" in existing:
        print("book_examples.screenshot_page_num already present — nothing to do.")
    else:
        conn.execute("ALTER TABLE book_examples ADD COLUMN screenshot_page_num INTEGER")
        conn.commit()
        print("Added book_examples.screenshot_page_num")
    conn.close()


if __name__ == "__main__":
    run()
