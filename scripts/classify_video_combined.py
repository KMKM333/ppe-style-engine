"""
classify_video_combined.py — the automated long-form pipeline's single-call
replacement for three separate Anthropic API calls (classify_template.py's
Class rubric, classify_video_breakdown.py's chapter breakdown, and
detect_video_visuals.py's chart/graph detection).

Those three modules stay exactly as they are and remain the source of truth
for their own rubric wording — they're still used standalone for manual/
backfill classification of other channels and short-form videos. This
module reuses their prompt bodies verbatim (see _extract_body, which slices
each source prompt down to just its field-list content, dropping the
"Return a JSON array...VIDEOS:/TITLE:" footer that's specific to that
module's own standalone call shape) and wraps them in one shared JSON
schema, so a long-form video's classification + chapter breakdown +
visual-chart detection happen in a single API call instead of three.

Since the video's ~4,000-6,000 word transcript no longer gets re-sent per
call, this cuts input-token cost roughly 3x per video — the same content,
read once instead of three times.

Only used by auto_process_video.py. If you're backfilling a channel by
hand (the --export/--load workflow), keep using the three original modules
— nothing about them changed.

Cost control (Aug 2026): the "visuals" part — recreating on-screen charts
as SVG — is skipped by default (PPE_SKIP_VISUALS=1), since it's likely the
single biggest output-token line item per long-form video. Set
PPE_SKIP_VISUALS=0, or pass --with-visuals on the manual/backfill CLI
below, to include it for a specific run.

Usage (manual/backfill):
    python3 classify_video_combined.py --video_id 123
    python3 classify_video_combined.py --video_id 123 --with-visuals
"""
import argparse
import json
import os

import classify_template as ct
import classify_video_breakdown as cvb
import detect_video_visuals as dv
import llm_client
from db_init import get_conn

# The combined response now carries what used to be three separate outputs
# (classification + breakdown + visuals) in one JSON object — 16,000 (the
# old breakdown-only budget) truncated mid-response on content-rich videos
# with many chapters and several flagged visuals, producing invalid JSON.
# 32,000 leaves real headroom; books already use 48,000 for a single call
# covering an entire book, so this isn't out of line.
COMBINED_MAX_TOKENS = 32000

# See "Cost control" note above — default ON (visuals skipped).
SKIP_VISUALS_DEFAULT = os.environ.get("PPE_SKIP_VISUALS", "1") == "1"


def _extract_body(prompt_text, start_marker, end_marker):
    """Slices a source prompt constant down to the text strictly between
    start_marker (kept, body starts right after it) and end_marker (body
    ends right before it). Raises loudly if either marker is missing, so a
    future edit to the source prompt's wording can't silently produce a
    truncated/garbled combined prompt."""
    start_idx = prompt_text.index(start_marker) + len(start_marker)
    end_idx = prompt_text.index(end_marker, start_idx)
    return prompt_text[start_idx:end_idx].strip()


def _classification_body():
    return _extract_body(
        ct.CLASSIFICATION_PROMPT,
        "Do not add commentary outside the JSON array.",
        "\nReturn a JSON array, one object per video, each including",
    )


def _breakdown_body():
    # Starts after "...each with:" rather than at the top of the field list —
    # the source prompt's own top-level keys (video_id, summary, sections)
    # describe ITS per-video object, not the per-section object this combined
    # prompt's "sections" array actually needs; only the indented sub-list
    # (section_number, section_title, summary, topics, points, terms,
    # examples) is what belongs here.
    body = _extract_body(
        cvb.LONGFORM_VIDEO_BREAKDOWN_PROMPT,
        "content itself), each with:",
        "\nReturn a JSON array, one object per video.",
    )
    # LONGFORM_VIDEO_BREAKDOWN_PROMPT escapes its literal {"term": ...}-style
    # JSON examples as {{ }} because the source string is itself later run
    # through .format(videos_block=...); this module never formats that
    # string, so the escaping needs to be undone or the model would see
    # literal double braces.
    return body.replace("{{", "{").replace("}}", "}")


def _visuals_body():
    # VISUAL_DETECTION_PROMPT has no batching wrapper to strip a prefix
    # from — its body starts at the top of the string, so this just takes
    # everything before the trailer (and resolves its own {marker_every_words}/
    # {max_visuals} placeholders, since this module doesn't call .format()
    # on the assembled combined prompt as a whole).
    end_marker = "\nReturn a JSON array (empty array if the video has no such moments"
    body = dv.VISUAL_DETECTION_PROMPT[: dv.VISUAL_DETECTION_PROMPT.index(end_marker)].strip()
    return body.format(marker_every_words=dv.MARKER_EVERY_WORDS, max_visuals=dv.MAX_VISUALS)


def build_combined_prompt(title, script, timed_segments, skip_visuals=None):
    if skip_visuals is None:
        skip_visuals = SKIP_VISUALS_DEFAULT

    classification_body = _classification_body()
    breakdown_body = _breakdown_body()
    timestamped = dv.build_timestamped_transcript(timed_segments) if timed_segments else script

    if skip_visuals:
        intro = (
            "You are analysing a long-form PPE (philosophy/politics/economics) YouTube video. "
            "Do TWO things in a single pass and return exactly one JSON object with two "
            'top-level keys: "classification", "sections" — plus a third, "summary", '
            "a 1-2 sentence plain-language description of what the whole video covers.\n\n"
        )
    else:
        intro = (
            "You are analysing a long-form PPE (philosophy/politics/economics) YouTube video. "
            "Do THREE things in a single pass and return exactly one JSON object with three "
            'top-level keys: "classification", "sections", "visuals" — plus a fourth, "summary", '
            "a 1-2 sentence plain-language description of what the whole video covers.\n\n"
        )

    body = (
        "============================================================\n"
        '1. CLASSIFICATION — the "classification" key, an object with exactly these fields\n'
        "============================================================\n"
        f"{classification_body}\n\n"
        "============================================================\n"
        '2. CHAPTER BREAKDOWN — the "sections" key, an array of objects, one per distinct\n'
        "chapter/segment of the video, IN ORDER (use YouTube's own chapter markers if given;\n"
        "otherwise infer natural segment breaks). Each section object has exactly these fields:\n"
        "============================================================\n"
        f"{breakdown_body}\n\n"
    )

    if skip_visuals:
        tail = (
            'Return exactly one JSON object of the shape {"summary": "...", "classification": {...}, '
            '"sections": [...]}. Do not add commentary outside the JSON object.\n\n'
            f"TITLE: {title}\n\n"
            "TRANSCRIPT:\n"
            f"{timestamped}\n"
        )
    else:
        visuals_body = _visuals_body()
        body += (
            "============================================================\n"
            '3. SIGNIFICANT VISUALS — the "visuals" key, an array (empty if none — most videos\n'
            "won't have any) of objects, each with exactly these fields:\n"
            "============================================================\n"
            f"{visuals_body}\n\n"
        )
        tail = (
            "The transcript below has inline [MM:SS] timestamp markers — used only for part 3, to "
            "point at an approximate on-screen moment.\n\n"
            'Return exactly one JSON object of the shape {"summary": "...", "classification": {...}, '
            '"sections": [...], "visuals": [...]}. Do not add commentary outside the JSON object.\n\n'
            f"TITLE: {title}\n\n"
            "TRANSCRIPT:\n"
            f"{timestamped}\n"
        )

    return intro + body + tail


def classify_video_combined(video_id, title, script, timed_segments, skip_visuals=None):
    """Runs the single combined API call and returns the parsed dict —
    does NOT write to the DB (see merge_combined_result)."""
    prompt = build_combined_prompt(title, script, timed_segments, skip_visuals=skip_visuals)
    # Batch API: half price, in exchange for the answer landing whenever the
    # batch finishes (usually minutes, Anthropic's own ceiling is 24h)
    # instead of immediately. Safe here specifically because this whole
    # function only ever runs inside a detached background subprocess (see
    # auto_process_video.py) — nothing interactive is waiting on it.
    result = llm_client.generate_json_batch(prompt, max_tokens=COMBINED_MAX_TOKENS)
    if not isinstance(result, dict):
        raise ValueError(f"Expected a single JSON object, got {type(result).__name__}")
    return result


def validate_combined_result(video_id, result):
    """Validates just the classification portion against
    classify_template.py's controlled vocabulary — the same check
    merge_classification_results()'s caller (auto_process_video.py) already
    relies on to catch a hallucinated enum value before it's merged."""
    classification = dict(result.get("classification") or {})
    classification["video_id"] = video_id
    return ct.validate_classification([classification])


def merge_combined_result(video_id, result):
    """Writes all three parts of a validated combined result to the DB:
    classification -> video_attributes (classify_template.merge), sections/
    points/terms/examples -> the same tables classify_video_breakdown.merge
    writes (full replace), visuals -> video_visuals (detect_video_visuals.merge,
    full replace). Reuses each module's own merge function rather than
    duplicating the SQL, so the combined path and the manual/backfill path
    write identically-shaped data."""
    classification = dict(result.get("classification") or {})
    classification["video_id"] = video_id
    ct.merge_classification_results([classification])

    breakdown_result = {
        "video_id": video_id,
        "summary": result.get("summary"),
        "sections": result.get("sections") or [],
    }
    cvb.merge_longform_video_breakdown_results([breakdown_result])
    dv.merge_visuals(video_id, extract_visuals(result))


def extract_visuals(result):
    """Pulls the "visuals" array out of a combined result and shapes it the
    way detect_video_visuals.merge_visuals() expects — dropping any flagged
    moment whose marker doesn't parse back into a real timestamp or that
    has no SVG fallback. Shared by merge_combined_result() above and
    auto_process_video.py's more granular (enrichment-only-on-failure)
    orchestration, so this list-building logic lives in exactly one place.
    Returns [] as-is when the visuals step was skipped (result has no
    "visuals" key) — merge_visuals() on an empty list is a no-op."""
    visuals = []
    for v in result.get("visuals") or []:
        ts = dv._parse_timestamp(v.get("nearest_marker"))
        if ts is None or not v.get("recreated_svg"):
            continue
        visuals.append({"timestamp_sec": ts, "caption": v.get("caption"), "recreated_svg": v.get("recreated_svg")})
    return visuals


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--video_id", type=int, required=True)
    ap.add_argument(
        "--with-visuals", action="store_true",
        help="Include chart/visual detection for this run even if PPE_SKIP_VISUALS is set (costs more output tokens).",
    )
    args = ap.parse_args()

    conn = get_conn()
    row = conn.execute("SELECT title, script, timed_transcript_json FROM videos WHERE video_id = ?", (args.video_id,)).fetchone()
    conn.close()
    if not row:
        raise SystemExit(f"No such video: {args.video_id}")

    timed = json.loads(row["timed_transcript_json"]) if row["timed_transcript_json"] else []
    result = classify_video_combined(
        args.video_id, row["title"], row["script"], timed,
        skip_visuals=(False if args.with_visuals else None),
    )
    errors = validate_combined_result(args.video_id, result)
    if errors:
        raise SystemExit("Validation failed: " + "; ".join(errors))
    merge_combined_result(args.video_id, result)
    print(f"Combined classification merged for video_id={args.video_id}.")
