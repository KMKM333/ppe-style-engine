"""
detect_video_visuals.py — flags moments in a long-form video's transcript
where the speaker is clearly referencing an on-screen chart/graph/table
(e.g. "as you can see in this chart...") and has the model recreate each one
as a simple SVG. This SVG recreation is always produced as a fallback, the
same spirit as book_sections.diagram_svg; instagram_transcriber's local
screenshot-capture step (which has access to the actual video) later tries
to replace it with a real frame grabbed at the matched timestamp — see
video_visuals.screenshot_captured.

Runs as one step of auto_process_video.py's pipeline. Long-form only: a
60-second Instagram clip essentially never cuts to an on-screen data visual,
so this is never invoked for short-form videos, and relies on
videos.timed_transcript_json (cue-level timing), which is only ever
captured for YouTube acquisitions.

Usage (manual/backfill):
    python3 detect_video_visuals.py --video_id 123
"""
import argparse
import json

import llm_client
from db_init import get_conn

MAX_VISUALS = 8
MARKER_EVERY_WORDS = 40

VISUAL_DETECTION_PROMPT = """You are reviewing the transcript of a long-form YouTube video for
moments where the speaker is clearly referencing an on-screen chart, graph, table, or other
data visualization (phrases like "as you can see here", "this chart shows", "look at this
graph", "this table breaks down..."). The transcript below has inline timestamp markers like
[12:34] scattered through it, roughly every {marker_every_words} words.

Flag at most {max_visuals} of the MOST significant/informative such moments — skip anything
vague or throwaway. For each, return a JSON object with:
- nearest_marker: string — copy the exact bracketed marker text (e.g. "12:34") immediately
  before or closest to the moment being described
- caption: string — one plain-language sentence describing what the chart/graph/table shows
- recreated_svg: string — a clean, simple SVG recreation (viewBox="0 0 400 300", no external
  fonts/images) approximating the chart/graph/table's structure, based on whatever numbers,
  categories, or trend the surrounding transcript describes. Make a reasonable best-effort
  reconstruction even from a loose verbal description — axis labels and a rough shape/values
  are more useful than a blank chart.

Return a JSON array (empty array if the video has no such moments — most videos won't).
Do not add commentary outside the JSON array.

TITLE: {title}

TRANSCRIPT:
{timestamped_transcript}
"""


def _format_timestamp(seconds):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _parse_timestamp(marker_text):
    """Parses a 'MM:SS' or 'HH:MM:SS' marker back into seconds. Returns None
    if it doesn't look like a timestamp at all (a hallucinated/garbled
    marker) — arithmetic is done here in code rather than trusting the model
    to convert MM:SS to seconds itself."""
    parts = (marker_text or "").strip().split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if len(nums) == 2:
        m, s = nums
        return float(m * 60 + s)
    h, m, s = nums
    return float(h * 3600 + m * 60 + s)


def build_timestamped_transcript(timed_segments, marker_every_words=MARKER_EVERY_WORDS):
    """timed_segments: [{"start": float, "text": str}, ...]. Inserts a
    [MM:SS] marker before roughly every `marker_every_words` words, so the
    model can point back at an approximate on-screen moment without doing
    its own MM:SS<->seconds arithmetic (parsed back out via
    _parse_timestamp instead)."""
    lines = []
    words_since_marker = marker_every_words  # force a marker at the very start
    for seg in timed_segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if words_since_marker >= marker_every_words:
            lines.append(f"[{_format_timestamp(seg['start'])}]")
            words_since_marker = 0
        lines.append(text)
        words_since_marker += len(text.split())
    return " ".join(lines)


def build_visual_prompt(title, timed_segments, max_visuals=MAX_VISUALS, marker_every_words=MARKER_EVERY_WORDS):
    timestamped = build_timestamped_transcript(timed_segments, marker_every_words)
    return VISUAL_DETECTION_PROMPT.format(
        title=title, timestamped_transcript=timestamped,
        max_visuals=max_visuals, marker_every_words=marker_every_words,
    )


def detect_visuals(video_id, title=None, timed_segments=None):
    """Runs the visual-detection LLM pass for one video and returns a list
    of {"timestamp_sec": float, "caption": str, "recreated_svg": str} dicts.
    A flagged moment whose marker doesn't parse back into a real timestamp
    is dropped rather than stored with a garbage/None timestamp. Does NOT
    write to the DB — see merge_visuals()."""
    conn = get_conn()
    row = conn.execute(
        "SELECT title, timed_transcript_json FROM videos WHERE video_id = ?", (video_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise ValueError(f"No such video: {video_id}")

    if timed_segments is None:
        if not row["timed_transcript_json"]:
            return []  # nothing to work with, e.g. a video ingested before timing was captured
        timed_segments = json.loads(row["timed_transcript_json"])
    if title is None:
        title = row["title"]
    if not timed_segments:
        return []

    prompt = build_visual_prompt(title, timed_segments)
    results = llm_client.generate_json(prompt, max_tokens=4096)
    if not isinstance(results, list):
        results = []

    visuals = []
    for r in results[:MAX_VISUALS]:
        ts = _parse_timestamp(r.get("nearest_marker"))
        if ts is None or not r.get("recreated_svg"):
            continue
        visuals.append({"timestamp_sec": ts, "caption": r.get("caption"), "recreated_svg": r.get("recreated_svg")})
    return visuals


def merge_visuals(video_id, visuals):
    """Replaces video_id's video_visuals rows with `visuals` — a full
    replace, not an append, so re-running detection on the same video
    doesn't pile up duplicates."""
    conn = get_conn()
    conn.execute("DELETE FROM video_visuals WHERE video_id = ?", (video_id,))
    for v in visuals:
        conn.execute(
            "INSERT INTO video_visuals (video_id, timestamp_sec, caption, recreated_svg) VALUES (?, ?, ?, ?)",
            (video_id, v["timestamp_sec"], v.get("caption"), v.get("recreated_svg")),
        )
    conn.commit()
    conn.close()
    print(f"Stored {len(visuals)} visual(s) for video_id={video_id}.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--video_id", type=int, required=True)
    args = ap.parse_args()
    found = detect_visuals(args.video_id)
    merge_visuals(args.video_id, found)
