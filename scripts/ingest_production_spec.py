"""
ingest_production_spec.py — registers one video submitted for SHOT/PACING
analysis (not transcript/content classification) into the
production_spec_inputs/production_spec_shots tables.

Used by webapp.py's /api/ingest/production-spec endpoint, which the
Instagram Bulk Transcriber's shot-analysis job POSTs to after running
ffmpeg scene-detection locally — the video/content-classification
counterpart is ingest_video.py + /api/ingest/video.

Usage (manual/backfill):
    python3 ingest_production_spec.py --title "..." --channel "Null.histories" \
        --platform Facebook --url https://... --duration 175.4 \
        --shots shots.json   # [{"shot_number":1,"start_sec":0,"end_sec":5.0}, ...]
"""
import argparse
import json

from db_init import get_conn, init_db
from similarity import normalize_hash

# get_or_create_channel is duplicated from ingest.py/ingest_video.py rather
# than imported, for the same reason ingest_video.py gives: keeps this
# module free of ingest.py's module-level pandas import, which isn't
# installed in production (webapp.py's environment).


def get_or_create_channel(conn, name, platform):
    row = conn.execute("SELECT channel_id FROM channels WHERE channel_name = ?", (name,)).fetchone()
    if row:
        return row["channel_id"]
    cur = conn.execute(
        "INSERT INTO channels (channel_name, platform, typical_length_band) VALUES (?, ?, ?)",
        (name, platform, "PS"),
    )
    conn.commit()
    return cur.lastrowid


def ingest_production_spec_row(title, channel, platform, url, duration_sec, posted_at,
                                scene_threshold, shots):
    """shots: [{"shot_number": int, "start_sec": float, "end_sec": float}, ...],
    already ordered. duration_sec fills in the final shot's end_sec if the
    caller didn't. Content-hash-deduped by URL (there's no transcript here to
    hash against, unlike ingest_video.py) so resubmitting the same URL reuses
    the existing input rather than creating a duplicate.

    Returns (input_id, [{"shot_number":..., "shot_id":...}, ...])."""
    conn = get_conn()
    channel_id = get_or_create_channel(conn, channel, platform)

    chash = normalize_hash(url) if url else None
    if chash:
        existing = conn.execute(
            "SELECT input_id FROM production_spec_inputs WHERE content_hash = ?", (chash,)
        ).fetchone()
        if existing:
            input_id = existing["input_id"]
            existing_shots = conn.execute(
                "SELECT shot_number, shot_id FROM production_spec_shots WHERE input_id = ? ORDER BY shot_number",
                (input_id,),
            ).fetchall()
            conn.close()
            print(f"input_id={input_id} already exists for this URL — skipping duplicate insert.")
            return input_id, [{"shot_number": r["shot_number"], "shot_id": r["shot_id"]} for r in existing_shots]

    cur = conn.execute(
        "INSERT INTO production_spec_inputs (channel_id, title, platform, url, duration_sec, "
        "posted_at, content_hash, scene_threshold, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'shots_detected')",
        (channel_id, title, platform, url, duration_sec, posted_at, chash, scene_threshold),
    )
    input_id = cur.lastrowid

    shot_rows = []
    for shot in shots:
        start_sec = shot["start_sec"]
        end_sec = shot.get("end_sec")
        if end_sec is None and duration_sec is not None and shot is shots[-1]:
            end_sec = duration_sec
        duration = (end_sec - start_sec) if end_sec is not None else None
        shot_cur = conn.execute(
            "INSERT INTO production_spec_shots (input_id, shot_number, start_sec, end_sec, duration_sec) "
            "VALUES (?, ?, ?, ?, ?)",
            (input_id, shot["shot_number"], start_sec, end_sec, duration),
        )
        shot_rows.append({"shot_number": shot["shot_number"], "shot_id": shot_cur.lastrowid})

    conn.commit()
    conn.close()

    print(f"Ingested production_spec input_id={input_id}: '{title}' ({len(shot_rows)} shots, channel_id={channel_id}).")
    return input_id, shot_rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--channel", required=True)
    ap.add_argument("--platform", default="Instagram")
    ap.add_argument("--url", default=None)
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--posted_at", default=None)
    ap.add_argument("--scene_threshold", type=float, default=0.25)
    ap.add_argument("--shots", required=True, help="path to a JSON file: [{\"shot_number\":1,\"start_sec\":0,\"end_sec\":5.0}, ...]")
    args = ap.parse_args()

    init_db()
    with open(args.shots, encoding="utf-8") as f:
        shots_data = json.load(f)
    ingest_production_spec_row(
        args.title, args.channel, args.platform, args.url,
        args.duration, args.posted_at, args.scene_threshold, shots_data,
    )
