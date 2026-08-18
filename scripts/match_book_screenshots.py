"""
match_book_screenshots.py — matches a live book's classified examples to the
PDF page each one most likely came from, and uploads that page image to the
live engine so it renders inline on the example (see /api/books/<id>/examples
and /api/books/<id>/examples/<id>/screenshot in webapp.py).

Runs locally because it needs the original PDF and its per-page PNG renders,
which only ever exist on the machine the Instagram Bulk Transcriber ran on
(the live engine only ever receives extracted plain text, never the PDF).

Matching works by extracting "distinctive tokens" (numbers/money/percentages
and multi-word proper nouns) from each example's title+text, then scoring
every PDF page by how many of those tokens appear on it — the page with the
highest score wins. Examples are LLM paraphrases of the book's own text, so
this is deliberately not exact-substring matching.

Usage:
    export PPE_INGEST_API_KEY=...   # same key the transcriber uses
    python3 match_book_screenshots.py --book_id 32 \
        --pdf_dir ../instagram_transcriber/results/abc6b17dbff9
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

# Capitalized words that are common sentence starters, not proper nouns —
# left in, they add noise to every page's score.
STOPWORDS = {
    "The", "This", "That", "These", "Those", "After", "Before", "Despite", "Using",
    "Also", "Shows", "While", "When", "Where", "Even", "Only", "Most", "Many",
    "Some", "Because", "Since", "Smil", "Author",
}


def extract_page_texts(pdf_path):
    import fitz  # PyMuPDF
    doc = fitz.open(pdf_path)
    return {i: page.get_text() for i, page in enumerate(doc, start=1)}


def weighted_tokens(text):
    nums = re.findall(r"\$?\d[\d,\.]*\s?(?:trillion|billion|million|gigajoules|percent|%)?", text)
    caps = re.findall(r"\b(?:[A-Z][a-z]{2,}(?:\s[A-Z][a-z]{2,}){0,2})\b", text)
    caps = [c for c in caps if c.split()[0] not in STOPWORDS]
    return [(t.strip(), 3) for t in nums if len(t.strip()) > 1] + [(t, 2 if " " in t else 1) for t in caps]


def best_page(example_title, example_text, page_texts):
    toks = weighted_tokens((example_title or "") + " " + (example_text or ""))
    scores = [(sum(w for t, w in toks if t in ptext), pno) for pno, ptext in page_texts.items()]
    scores.sort(reverse=True)
    return scores[0] if scores else (0, None)


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
    ap.add_argument("--book_id", type=int, required=True)
    ap.add_argument("--pdf_dir", required=True,
                     help="transcriber results/<job_id> dir containing original.pdf and pages/")
    ap.add_argument("--min_score", type=int, default=4, help="skip examples below this match confidence")
    ap.add_argument("--dry_run", action="store_true", help="print matches without uploading")
    args = ap.parse_args()

    if not API_KEY:
        sys.exit("PPE_INGEST_API_KEY is not set — export it (same value the transcriber uses).")

    pdf_path = os.path.join(args.pdf_dir, "original.pdf")
    pages_dir = os.path.join(args.pdf_dir, "pages")
    if not os.path.isfile(pdf_path):
        sys.exit(f"No original.pdf at {pdf_path}")

    page_texts = extract_page_texts(pdf_path)

    try:
        data = api_get(f"/api/books/{args.book_id}/examples")
    except urllib.error.HTTPError as e:
        sys.exit(f"Couldn't fetch examples for book {args.book_id}: HTTP {e.code} {e.read().decode()[:300]}")

    matched = skipped = 0
    for ex in data["examples"]:
        if ex.get("screenshot_page_num"):
            print(f"[skip] already matched: {ex['example_title']} -> page {ex['screenshot_page_num']}")
            continue
        score, page_num = best_page(ex.get("example_title"), ex.get("example_text"), page_texts)
        if score < args.min_score or page_num is None:
            print(f"[no match] {ex['example_title']} (best score {score})")
            skipped += 1
            continue
        print(f"[match] {ex['example_title']} -> page {page_num} (score {score})")
        if args.dry_run:
            matched += 1
            continue
        image_path = os.path.join(pages_dir, f"page_{page_num:04d}.png")
        if not os.path.isfile(image_path):
            print(f"  !! page image missing on disk: {image_path}")
            continue
        with open(image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode()
        try:
            result = api_post(
                f"/api/books/{args.book_id}/examples/{ex['example_id']}/screenshot",
                {"page_num": page_num, "image_base64": image_b64},
            )
            print(f"  -> uploaded: {result}")
            matched += 1
        except urllib.error.HTTPError as e:
            print(f"  !! upload failed: HTTP {e.code} {e.read().decode()[:300]}")

    print(f"\n{matched} matched, {skipped} skipped (no confident match) "
          f"of {len(data['examples'])} example(s).")


if __name__ == "__main__":
    main()
