"""
reassess_video_durations.py — backfills videos.duration_sec by matching each
video's URL (via its Instagram shortcode) against the scrape manifests under
instagram_transcriber/results/*/manifest.json, then reading the end timestamp
of the last cue in the matching .srt subtitle file as the video's length.

Videos with no matching manifest/srt (e.g. ingested from a plain CSV with no
raw scrape artifacts) are left untouched.

Usage:
    python3 reassess_video_durations.py [--dry_run]
"""
import argparse
import glob
import json
import re
from pathlib import Path

from db_init import get_conn

RESULTS_DIR = Path(__file__).resolve().parent.parent / "instagram_transcriber" / "results"

SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|reels)/([A-Za-z0-9_-]+)")
SRT_TIME_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})")


def shortcode_of(url):
    if not url:
        return None
    m = SHORTCODE_RE.search(url)
    return m.group(1) if m else None


def srt_duration_sec(srt_path):
    """Returns the end timestamp of the last subtitle cue, in seconds."""
    text = srt_path.read_text(errors="ignore")
    matches = SRT_TIME_RE.findall(text)
    if not matches:
        return None
    h, m, s, ms = matches[-1][4:8]
    return round(int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000, 2)


def build_shortcode_index():
    """Maps Instagram shortcode -> resolved .srt Path, across every manifest."""
    index = {}
    for manifest_path in glob.glob(str(RESULTS_DIR / "*" / "manifest.json")):
        job_dir = Path(manifest_path).parent
        data = json.loads(Path(manifest_path).read_text())
        for item in data.get("items", []):
            code = shortcode_of(item.get("url") or item.get("webpage_url"))
            srt_filename = item.get("srt_filename")
            if not code or not srt_filename:
                continue
            srt_path = job_dir / srt_filename
            if srt_path.exists():
                index[code] = srt_path
    return index


def run(dry_run=False):
    index = build_shortcode_index()
    conn = get_conn()
    rows = conn.execute("SELECT video_id, url, duration_sec FROM videos").fetchall()

    n_updated = 0
    n_unmatched = 0
    n_no_cues = 0
    for row in rows:
        code = shortcode_of(row["url"])
        srt_path = index.get(code) if code else None
        if not srt_path:
            n_unmatched += 1
            continue
        duration = srt_duration_sec(srt_path)
        if duration is None:
            n_no_cues += 1
            continue
        if not dry_run:
            conn.execute("UPDATE videos SET duration_sec = ? WHERE video_id = ?", (duration, row["video_id"]))
        n_updated += 1

    if not dry_run:
        conn.commit()
    conn.close()

    print(f"Matched and {'would update' if dry_run else 'updated'} duration for {n_updated} video(s).")
    print(f"No matching manifest/srt for {n_unmatched} video(s); {n_no_cues} matched srt had no parseable cues.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry_run", action="store_true")
    args = ap.parse_args()
    run(dry_run=args.dry_run)
