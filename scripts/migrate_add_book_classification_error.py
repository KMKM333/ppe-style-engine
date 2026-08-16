"""
migrate_add_book_classification_error.py — one-off migration adding
book_attributes.classification_error, which records why an automatic
classification attempt (auto_process_book.classify_and_build_profile) failed,
so the "needs review" pill has something to show. Safe to re-run: skips if
the column is already present.

Usage:
    python3 migrate_add_book_classification_error.py
"""
from db_init import get_conn


def run():
    conn = get_conn()
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(book_attributes)").fetchall()}
    if "classification_error" in existing:
        print("book_attributes.classification_error already present — nothing to do.")
    else:
        conn.execute("ALTER TABLE book_attributes ADD COLUMN classification_error TEXT")
        conn.commit()
        print("Added book_attributes.classification_error")
    conn.close()


if __name__ == "__main__":
    run()
