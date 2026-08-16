"""
auto_process_book.py — runs the full book-classification pipeline
automatically, the same sequence of steps that used to be done by hand
(export the rubric prompt, paste into Claude, save the reply, --load it,
compute readability, rebuild the author's profile).

classify_and_build_profile(book_id) is called from webapp.py's
/api/ingest/book endpoint in a background thread right after a book is
ingested, so a freshly-imported book goes from 'pending' to fully classified
with no manual step. If the Anthropic API call fails or the reply doesn't
match the rubric's controlled vocabulary, the book is marked 'needs_review'
with the reason recorded in book_attributes.classification_error instead of
merging bad data — a book should never look silently mis-classified.

Usage (manual/backfill):
    python3 auto_process_book.py --book_id 18
"""
import argparse
import traceback

import book_profile_builder
import classify_book_template as cbt
import compute_book_readability
import llm_client
from db_init import get_conn

# The largest book classified so far (~112K words) produced ~30K tokens of
# JSON output (19 sections, 160 points, 35 terms, 59 examples). This leaves
# headroom for longer books; bump further if a very long book gets truncated.
CLASSIFICATION_MAX_TOKENS = 48000


def _mark_needs_review(book_id, reason):
    conn = get_conn()
    conn.execute(
        """INSERT INTO book_attributes (book_id, classified_by, classification_error)
           VALUES (?, 'needs_review', ?)
           ON CONFLICT(book_id) DO UPDATE SET
             classified_by = 'needs_review', classification_error = excluded.classification_error""",
        (book_id, reason),
    )
    conn.commit()
    conn.close()
    print(f"[auto_process_book] book_id={book_id} needs review: {reason}")


def classify_and_build_profile(book_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT book_id, title, author, full_text FROM books WHERE book_id = ?", (book_id,)
    ).fetchone()
    conn.close()
    if not row:
        print(f"[auto_process_book] book_id={book_id} not found")
        return
    if not row["full_text"]:
        _mark_needs_review(book_id, "No full_text stored for this book — nothing to classify.")
        return

    prompt = cbt.build_book_prompt([row])

    try:
        results = llm_client.generate_json(prompt, max_tokens=CLASSIFICATION_MAX_TOKENS)
    except Exception as e:
        _mark_needs_review(book_id, f"Anthropic API call failed: {e}")
        traceback.print_exc()
        return

    errors = cbt.validate_book_classification(results)
    if errors:
        _mark_needs_review(book_id, "Classification failed validation: " + "; ".join(errors[:10]))
        return

    try:
        cbt.merge_book_classification_results(results)
        compute_book_readability.run(book_id)
        book_profile_builder.build_all(min_n=1)
    except Exception as e:
        _mark_needs_review(book_id, f"Merge/profile-build step failed: {e}")
        traceback.print_exc()
        return

    print(f"[auto_process_book] book_id={book_id} classified and profile rebuilt.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--book_id", type=int, required=True)
    args = ap.parse_args()
    classify_and_build_profile(args.book_id)
