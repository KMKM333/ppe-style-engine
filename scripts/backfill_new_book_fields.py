"""
backfill_new_book_fields.py — targeted backfill for the 11 new cross-media
shared fields added to book_attributes this session, for books that were
ALREADY classified before those fields existed. Only ever touches the new
fields — the original classification is left untouched.

Safety: refuses to write into any book whose primary_goal is NULL (i.e. a
book that's never been classified at all).

Usage:
    python3 backfill_new_book_fields.py --load results.json
"""
import argparse
import json

from db_init import get_conn

NEW_BOOK_FIELDS = [
    "value_promise", "information_density", "curiosity_loop", "relatability_factor",
    "identity_framing", "contrarian_positioning", "adjective_intensity", "punctuation_delivery",
    "rhythmic_repetition", "vulnerability_depth", "condescension_vs_empowerment",
]


def merge_new_fields(json_path):
    with open(json_path) as f:
        results = json.load(f)

    conn = get_conn()
    n = 0
    skipped = 0
    for r in results:
        book_id = r["book_id"]
        existing = conn.execute(
            "SELECT primary_goal FROM book_attributes WHERE book_id = ?", (book_id,)
        ).fetchone()
        if not existing or existing["primary_goal"] is None:
            print(f"SKIP book_id={book_id}: not already classified (primary_goal is NULL)")
            skipped += 1
            continue
        keys = [k for k in NEW_BOOK_FIELDS if k in r]
        if not keys:
            continue
        set_clause = ", ".join([f"{k} = ?" for k in keys])
        values = [r[k] for k in keys]
        conn.execute(
            f"UPDATE book_attributes SET {set_clause} WHERE book_id = ?",
            (*values, book_id),
        )
        n += 1
    conn.commit()
    conn.close()
    print(f"Backfilled new fields for {n} book(s), skipped {skipped}.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", required=True)
    args = ap.parse_args()
    merge_new_fields(args.load)
