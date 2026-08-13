"""
compute_book_readability.py — computes a Flesch-Kincaid readability score for
each book from its full_text, combining two underlying attributes (average
sentence length, average syllables per word) the same way
feature_extraction.py does for video scripts. This is a deterministic/auto
metric, not a Claude classification field, so it lives in book_attributes
alongside — but independent of — the classification pass:
merge_book_classification() never touches these three columns.

Usage:
    python3 compute_book_readability.py            # all books with full_text
    python3 compute_book_readability.py --book_id 3
"""
import argparse

from db_init import get_conn
from feature_extraction import _sentences, _words, _syllable_count


def compute_readability(text):
    """Returns (avg_sentence_len, avg_syllables_per_word, readability_score) —
    the same Flesch-Kincaid grade formula as feature_extraction.flesch_kincaid_grade,
    with its two component averages surfaced separately."""
    sents = _sentences(text)
    words = _words(text)
    if not sents or not words:
        return None, None, None
    n_sent, n_word = len(sents), len(words)
    avg_sentence_len = round(n_word / n_sent, 2)
    syllables = sum(_syllable_count(w) for w in words)
    avg_syllables_per_word = round(syllables / n_word, 3)
    grade = round(0.39 * avg_sentence_len + 11.8 * avg_syllables_per_word - 15.59, 2)
    return avg_sentence_len, avg_syllables_per_word, grade


def run(book_id=None):
    conn = get_conn()
    q = "SELECT book_id, title, full_text FROM books WHERE full_text IS NOT NULL AND full_text != ''"
    params = []
    if book_id:
        q += " AND book_id = ?"
        params.append(book_id)
    rows = conn.execute(q, params).fetchall()

    n = 0
    for r in rows:
        avg_sent, avg_syll, grade = compute_readability(r["full_text"])
        if grade is None:
            print(f"Skipped book_id {r['book_id']} ({r['title']}) — no extractable text.")
            continue
        conn.execute(
            """INSERT INTO book_attributes (book_id, avg_sentence_len, avg_syllables_per_word, readability_score)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(book_id) DO UPDATE SET
                 avg_sentence_len=excluded.avg_sentence_len,
                 avg_syllables_per_word=excluded.avg_syllables_per_word,
                 readability_score=excluded.readability_score""",
            (r["book_id"], avg_sent, avg_syll, grade),
        )
        n += 1
        print(f"book_id {r['book_id']} ({r['title']}): grade {grade} "
              f"(avg sentence {avg_sent} words, {avg_syll} syllables/word)")
    conn.commit()
    conn.close()
    print(f"Updated readability for {n} book(s).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--book_id", type=int)
    args = ap.parse_args()
    run(args.book_id)
