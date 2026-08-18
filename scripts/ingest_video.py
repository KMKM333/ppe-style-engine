"""
ingest_video.py — registers one long-form video's transcript + metadata into
the `videos`/`video_attributes` tables and runs auto-feature extraction on
it immediately, the single-video live-engine counterpart to ingest.py's
CSV/XLSX batch path.

Used by webapp.py's /api/ingest/video endpoint, which the Instagram Bulk
Transcriber's new single-video section POSTs to — the same relationship
ingest_book.py has with /api/ingest/book.

Usage (manual/backfill):
    python3 ingest_video.py --title "..." --script transcript.txt \
        --channel "Johnny Harris" --url https://... --duration 1616 \
        --length_band C
"""
import argparse
import json

from db_init import get_conn, init_db
from feature_extraction import extract_auto_features
from ingest import get_or_create_channel
from similarity import normalize_hash


def ingest_video_row(title, script, channel, platform="YouTube", url=None, duration_sec=None,
                      posted_at=None, chapter_count=None, timed_transcript=None, length_band="C"):
    """timed_transcript, if given, is the cue-level [{"start": float, "text":
    str}, ...] list captured during acquisition — stored as-is so
    detect_video_visuals.py can point at an approximate on-screen moment
    without re-transcribing. Content-hash-deduped the same way ingest.py's
    batch path is, so retrying a job that already succeeded (e.g. after a
    screenshot-capture step failed) doesn't create a second video row."""
    title = title.strip()
    script = script.strip()

    conn = get_conn()
    channel_id = get_or_create_channel(conn, channel, platform, length_band)

    chash = normalize_hash(script)
    existing = conn.execute("SELECT video_id FROM videos WHERE content_hash = ?", (chash,)).fetchone()
    if existing:
        conn.close()
        print(f"video_id={existing['video_id']} already exists for this transcript — skipping duplicate insert.")
        return existing["video_id"]

    timed_transcript_json = json.dumps(timed_transcript) if timed_transcript else None
    cur = conn.execute(
        "INSERT INTO videos (channel_id, title, script, url, duration_sec, posted_at, content_hash, "
        "media_type, timed_transcript_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (channel_id, title, script, url, duration_sec, posted_at, chash, platform, timed_transcript_json),
    )
    video_id = cur.lastrowid

    feats = extract_auto_features(script, title)
    if chapter_count is not None:
        feats["chapter_count"] = int(chapter_count)
        feats["has_chapters"] = 1 if int(chapter_count) > 0 else 0
    cols = ", ".join(feats.keys())
    placeholders = ", ".join(["?"] * len(feats))
    conn.execute(
        f"INSERT INTO video_attributes (video_id, {cols}) VALUES (?, {placeholders})",
        (video_id, *feats.values()),
    )
    conn.commit()
    conn.close()

    print(f"Ingested video_id={video_id}: '{title}' (channel_id={channel_id}).")
    return video_id


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--script", required=True, help="path to a .txt file with the transcript")
    ap.add_argument("--channel", required=True)
    ap.add_argument("--platform", default="YouTube")
    ap.add_argument("--url", default=None)
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--posted_at", default=None)
    ap.add_argument("--chapter_count", type=int, default=None)
    ap.add_argument("--length_band", default="C")
    args = ap.parse_args()

    init_db()
    with open(args.script, encoding="utf-8", errors="ignore") as f:
        script_text = f.read()
    ingest_video_row(
        args.title, script_text, args.channel, args.platform, args.url,
        args.duration, args.posted_at, args.chapter_count, None, args.length_band,
    )
