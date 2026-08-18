"""
upload_book_pdfs.py — uploads every book PDF from the local "PPE Books" desktop
folder to the live engine, so the source file survives on the persistent disk
instead of only ever living on this machine (see /books/<id>/pdf and
/api/books/<id>/pdf in webapp.py, and the Sources page that links to them).

Matches PDFs to books by filename, since the two don't always agree exactly
(e.g. "IB Economics School Book.pdf" vs. "Oxford IB Diploma Programme:
Economics Course Companion", or a spelling difference like "In Defense of
Open Society.pdf" vs. "In Defence of Open Society") — scores every book title
against every filename by token overlap (Jaccard similarity) and takes the
best match above a confidence threshold, printing the match either way so a
low-confidence guess is easy to catch by eye. Already-uploaded books
(has_pdf=true) are skipped, so it's safe to re-run after adding new PDFs.

Usage:
    export PPE_INGEST_API_KEY=...
    python3 upload_book_pdfs.py --pdf_dir "/Users/you/Desktop/PPE Books"
"""
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

LIVE_BASE_URL = os.environ.get("PPE_LIVE_URL", "https://ppe-style-engine.onrender.com")
API_KEY = os.environ.get("PPE_INGEST_API_KEY")

DEFAULT_PDF_DIR = os.path.expanduser("~/Desktop/PPE Books")


def tokens(text):
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def best_book_match(filename, books):
    stem = os.path.splitext(filename)[0]
    ftoks = tokens(stem)
    scored = sorted(
        ((jaccard(ftoks, tokens(b["title"])), b) for b in books),
        key=lambda pair: pair[0], reverse=True,
    )
    return scored[0] if scored else (0.0, None)


def api_get(path):
    req = urllib.request.Request(f"{LIVE_BASE_URL}{path}", headers={"X-Ingest-Key": API_KEY})
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def api_post(path, payload):
    req = urllib.request.Request(
        f"{LIVE_BASE_URL}{path}", data=json.dumps(payload).encode(),
        headers={"X-Ingest-Key": API_KEY, "Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf_dir", default=DEFAULT_PDF_DIR, help="folder of book PDFs to upload")
    ap.add_argument("--min_score", type=float, default=0.35, help="skip filenames below this match confidence")
    ap.add_argument("--dry_run", action="store_true", help="print matches without uploading")
    args = ap.parse_args()

    if not API_KEY:
        sys.exit("PPE_INGEST_API_KEY is not set — export it (same value the transcriber uses).")
    if not os.path.isdir(args.pdf_dir):
        sys.exit(f"No such directory: {args.pdf_dir}")

    data = api_get("/api/books")
    books = data["books"]
    already = {b["book_id"] for b in books if b["has_pdf"]}

    pdf_files = sorted(f for f in os.listdir(args.pdf_dir) if f.lower().endswith(".pdf"))
    if not pdf_files:
        sys.exit(f"No PDFs found in {args.pdf_dir}")

    matched_book_ids = set()
    uploaded = skipped_existing = skipped_low_confidence = 0

    for filename in pdf_files:
        score, book = best_book_match(filename, books)
        if book is None or score < args.min_score:
            print(f"[no match] {filename} (best score {score:.2f})")
            skipped_low_confidence += 1
            continue

        matched_book_ids.add(book["book_id"])
        if book["book_id"] in already:
            print(f"[skip] already has a PDF: {filename} -> book_id {book['book_id']} \"{book['title']}\"")
            skipped_existing += 1
            continue

        print(f"[match {score:.2f}] {filename} -> book_id {book['book_id']} \"{book['title']}\"")
        if args.dry_run:
            uploaded += 1
            continue

        path = os.path.join(args.pdf_dir, filename)
        with open(path, "rb") as f:
            pdf_b64 = base64.b64encode(f.read()).decode()
        try:
            result = api_post(f"/api/books/{book['book_id']}/pdf", {"pdf_base64": pdf_b64})
            print(f"  -> uploaded: {result}")
            uploaded += 1
        except urllib.error.HTTPError as e:
            print(f"  !! upload failed: HTTP {e.code} {e.read().decode()[:300]}")

    unmatched_books = [b for b in books if b["book_id"] not in matched_book_ids and not b["has_pdf"]]
    print(f"\n{uploaded} uploaded, {skipped_existing} already had a PDF, "
          f"{skipped_low_confidence} filename(s) had no confident match.")
    if unmatched_books:
        print(f"{len(unmatched_books)} book(s) still have no PDF and weren't matched by any filename here:")
        for b in unmatched_books:
            print(f"  book_id {b['book_id']}: {b['title']}")


if __name__ == "__main__":
    main()
