"""
similarity.py — content-identity checks.

This is what separates the two axes the rating system now scores on:
  - CONTENT MATCH: is this literally (or almost literally) one of the
    videos a profile was built from? -> membership, not correlation.
  - ATTRIBUTE CORRELATION: independent of content, how closely do this
    text's structural/stylistic numbers resemble the profile's fingerprint?

A genuine philosophyminis video fed back into the engine should hit the
first check (content match -> 100%). A brand-new script that merely
*resembles* the style should only ever be scored on the second (attribute
correlation), which tops out below 100 by design — perfect correlation
across a dozen independent attributes basically never happens by chance
for text that isn't the same text.
"""
import hashlib
import re
from difflib import SequenceMatcher


def normalize_text(text: str) -> str:
    t = text.lower()
    t = re.sub(r"[\u2018\u2019]", "'", t)
    t = re.sub(r"[\u201c\u201d]", '"', t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^a-z0-9' ]", "", t)
    return t.strip()


def normalize_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode()).hexdigest()


def text_similarity(a: str, b: str) -> float:
    """0-1 similarity ratio between two texts (normalized). Cheap and
    dependency-free; good enough to catch exact matches and near-duplicates
    (minor edits, re-transcriptions, punctuation differences)."""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def find_best_match(raw_text: str, candidate_rows, threshold_exact=0.995, threshold_near=0.85):
    """candidate_rows: iterable of sqlite3.Row with video_id, script.
    Returns (video_id, similarity, match_type) or (None, 0.0, None).
    match_type: 'exact' | 'near' | None
    """
    target_hash = normalize_hash(raw_text)
    best_id, best_sim = None, 0.0

    for row in candidate_rows:
        if row["content_hash"] == target_hash:
            return row["video_id"], 1.0, "exact"
        sim = text_similarity(raw_text, row["script"])
        if sim > best_sim:
            best_id, best_sim = row["video_id"], sim

    if best_sim >= threshold_exact:
        return best_id, best_sim, "exact"
    if best_sim >= threshold_near:
        return best_id, best_sim, "near"
    return None, best_sim, None
