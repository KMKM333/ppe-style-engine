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

from db_init import get_conn, init_db, PRODUCTION_SPEC_SHOTS_DIR
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
            # An input whose frames are all present is finished work: hand
            # back what is stored and charge nothing to re-do it.
            #
            # An input MISSING frames is different, and the reason this
            # branch exists. The stored shot list was written by one look at
            # the video; the frames arriving now come from a fresh look, and
            # the two can disagree — scene detection is not perfectly
            # repeatable and the platform does not always serve a
            # byte-identical file. Keeping the old list then leaves slots
            # nothing will ever fill: one input sat at 38 of 41 frames
            # through five re-imports, because each new look produced 37
            # shots and the three orphans could not be reached. Replacing
            # the list makes it complete by construction, since the shots
            # and the frames now come from the SAME look.
            incomplete = conn.execute(
                "SELECT COUNT(*) FROM production_spec_shots "
                "WHERE input_id = ? AND COALESCE(frame_captured, 0) = 0", (input_id,)
            ).fetchone()[0]
            if not incomplete:
                existing_shots = conn.execute(
                    "SELECT shot_number, shot_id FROM production_spec_shots WHERE input_id = ? ORDER BY shot_number",
                    (input_id,),
                ).fetchall()
                conn.close()
                print(f"input_id={input_id} already exists for this URL — skipping duplicate insert.")
                return input_id, [{"shot_number": r["shot_number"], "shot_id": r["shot_id"]} for r in existing_shots]

            old_ids = [r["shot_id"] for r in conn.execute(
                "SELECT shot_id FROM production_spec_shots WHERE input_id = ?", (input_id,))]
            conn.execute("DELETE FROM production_spec_shots WHERE input_id = ?", (input_id,))
            conn.execute("DELETE FROM production_spec_attributes WHERE input_id = ?", (input_id,))
            conn.execute(
                "UPDATE production_spec_inputs SET status = 'shots_detected', classification_error = NULL, "
                "duration_sec = COALESCE(?, duration_sec), scene_threshold = ? WHERE input_id = ?",
                (duration_sec, scene_threshold, input_id),
            )
            # The frame files for shots that no longer exist would otherwise
            # sit on the disk forever, and the disk filling is what caused
            # this in the first place.
            shot_dir = PRODUCTION_SPEC_SHOTS_DIR / str(input_id)
            for sid in old_ids:
                for ext in ("jpg", "png"):
                    f = shot_dir / f"shot_{sid}.{ext}"
                    if f.exists():
                        try:
                            f.unlink()
                        except OSError:
                            pass
            print(f"input_id={input_id} had {incomplete} shot(s) without a frame — "
                  f"replacing its {len(old_ids)} stored shots with this pass's {len(shots)}.")
            shot_rows = []
            for shot in shots:
                start_sec = shot["start_sec"]
                end_sec = shot.get("end_sec")
                if end_sec is None and duration_sec is not None and shot is shots[-1]:
                    end_sec = duration_sec
                dur = (end_sec - start_sec) if end_sec is not None else None
                cur2 = conn.execute(
                    "INSERT INTO production_spec_shots (input_id, shot_number, start_sec, end_sec, duration_sec) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (input_id, shot["shot_number"], start_sec, end_sec, dur),
                )
                shot_rows.append({"shot_number": shot["shot_number"], "shot_id": cur2.lastrowid})
            conn.commit()
            conn.close()
            return input_id, shot_rows

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
