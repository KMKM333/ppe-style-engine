"""
expand_book_examples.py — one-time enrichment pass over book_examples.

The original classification (classify_book_template.py) writes each example as a
single terse sentence and reinforces_point as a short phrase. This script asks
Claude to expand both fields to 2-3 sentences each, using only the book/section
context already stored in the DB (book title/author/subject, section title +
summary, the example's own existing text) — it's explicitly told not to invent
new facts, names, or numbers that aren't already implied by the current text, so
this is an elaboration pass, not a re-read of the source book.

Requires ANTHROPIC_API_KEY to be set (see llm_client.py).

Usage:
    python3 expand_book_examples.py --all
    python3 expand_book_examples.py --book_id 9
    python3 expand_book_examples.py --all --dry_run
"""
import argparse

from db_init import get_conn
from llm_client import generate_json

EXPAND_PROMPT = """You are expanding a set of book-example annotations from "{title}" by {author}
(subject: {subject}) so they're more useful as a standalone reference. Each example below currently
has a terse one-sentence example_text and a short-phrase reinforces_point. Rewrite each one to be more
comprehensive, WITHOUT inventing new facts, names, or numbers that aren't already implied by the
current text:

- example_text: expand into 2-3 sentences. Add explanatory context about what's being illustrated and
  why it's a notable/useful example — stay strictly grounded in what the current text already says.
- reinforces_point: expand into 1-2 sentences that explain not just WHAT point the example supports,
  but HOW or WHY it demonstrates that point (the mechanism or logic connecting the example to the point).

EXAMPLES (each tagged with its section for context):
{examples_block}

Return exactly a JSON array, one object per example, each with "example_id" (matching the id given),
"example_text", and "reinforces_point". No commentary outside the JSON array.
"""


def _load_examples(conn, book_id=None):
    q = """
        SELECT e.example_id, e.book_id, e.example_text, e.reinforces_point,
               b.title, b.author, b.subject,
               s.section_title, s.summary AS section_summary
        FROM book_examples e
        JOIN books b ON b.book_id = e.book_id
        LEFT JOIN book_sections s ON s.section_id = e.section_id
    """
    params = []
    if book_id:
        q += " WHERE e.book_id = ?"
        params.append(book_id)
    q += " ORDER BY e.book_id, e.section_id, e.example_id"
    return conn.execute(q, params).fetchall()


def _group_by_book(rows):
    groups = {}
    for r in rows:
        groups.setdefault(r["book_id"], []).append(r)
    return groups


def expand_book_examples(book_id=None, dry_run=False):
    conn = get_conn()
    rows = _load_examples(conn, book_id)
    if not rows:
        print("No examples found for that filter.")
        conn.close()
        return

    groups = _group_by_book(rows)
    total_updated = 0
    for bid, items in groups.items():
        r0 = items[0]
        examples_block = "\n".join(
            f'- example_id: {r["example_id"]}\n'
            f'  section: {r["section_title"] or "(unsectioned)"}{" — " + r["section_summary"] if r["section_summary"] else ""}\n'
            f'  example_text: {r["example_text"]}\n'
            f'  reinforces_point: {r["reinforces_point"] or "(none given)"}'
            for r in items
        )
        prompt = EXPAND_PROMPT.format(
            title=r0["title"], author=r0["author"] or "Unknown", subject=r0["subject"] or "—",
            examples_block=examples_block,
        )
        print(f"Book {bid} ({r0['title']}): expanding {len(items)} example(s)...")
        results = generate_json(prompt, max_tokens=max(4096, len(items) * 1000))
        by_id = {r["example_id"]: r for r in results}

        for r in items:
            new = by_id.get(r["example_id"])
            if not new:
                print(f"  WARNING: no result returned for example_id {r['example_id']}, skipping")
                continue
            if dry_run:
                print(f"  #{r['example_id']}: {new['example_text']}")
                continue
            conn.execute(
                "UPDATE book_examples SET example_text = ?, reinforces_point = ? WHERE example_id = ?",
                (new["example_text"], new["reinforces_point"], r["example_id"]),
            )
            total_updated += 1
        if not dry_run:
            conn.commit()

    conn.close()
    print(f"\nDone. {total_updated} example(s) updated across {len(groups)} book(s).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--book_id", type=int, default=None, help="limit to one book; omit with --all for every book")
    ap.add_argument("--all", action="store_true", help="run across every book")
    ap.add_argument("--dry_run", action="store_true", help="print results without writing to the DB")
    args = ap.parse_args()

    if not args.book_id and not args.all:
        ap.error("pass --book_id N or --all")

    expand_book_examples(args.book_id, dry_run=args.dry_run)
