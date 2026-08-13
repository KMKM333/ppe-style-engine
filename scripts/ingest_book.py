"""
ingest_book.py — registers a non-fiction book's metadata + full text into
the `books` table, ready for classification against the rubric in
classify_book_template.py.

Books are a different shape from videos (one long document instead of many
short scripts), so ingestion is a single-file operation rather than a
CSV/XLSX batch — pass a .txt file with the book's full text.

Usage:
    python3 ingest_book.py --file mybook.txt --title "Book Title" \
                            --author "Author Name" --subject Economics \
                            [--year 2023] [--source_note "user-provided .txt"]

After ingesting, run:
    python3 classify_book_template.py --export --book_id N
to get the classification prompt for that book.
"""
import argparse

from db_init import get_conn, init_db


def ingest_book(file_path, title, author, subject, year=None, source_note=None):
    with open(file_path, encoding="utf-8", errors="ignore") as f:
        full_text = f.read().strip()

    word_count = len(full_text.split())

    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO books (title, author, subject, publication_year, word_count, full_text, source_note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (title, author, subject, year, word_count, full_text, source_note),
    )
    book_id = cur.lastrowid
    # book_attributes row is created lazily by merge_book_classification(),
    # but a 'pending' placeholder row makes the book show up as un-classified
    # immediately in the Books list rather than looking like it errored out
    conn.execute(
        "INSERT INTO book_attributes (book_id, classified_by) VALUES (?, 'pending')",
        (book_id,),
    )
    conn.commit()
    conn.close()

    print(f"Ingested book_id={book_id}: '{title}' ({word_count} words). "
          f"Next: python3 classify_book_template.py --export --book_id {book_id}")
    return book_id


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="path to a .txt file with the book's full text")
    ap.add_argument("--title", required=True)
    ap.add_argument("--author", default=None)
    ap.add_argument("--subject", default=None,
                     help="Economics / Politics / Philosophy / Psychology / Sustainability / Science / Technology")
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--source_note", default=None)
    args = ap.parse_args()

    init_db()  # safe to call repeatedly, uses IF NOT EXISTS
    ingest_book(args.file, args.title, args.author, args.subject, args.year, args.source_note)
