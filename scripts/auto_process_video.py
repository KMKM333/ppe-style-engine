"""
auto_process_video.py — runs the full long-form video pipeline
automatically: the same sequence of steps that used to be done by hand for
the first YouTube video (export the rubric prompt, paste into Claude, save
the reply, --load it, then the same again for the chapter breakdown, then
profile_builder.py) — now as one Anthropic API call instead of three, via
classify_video_combined.py.

classify_and_build_profile(video_id) is called from webapp.py's
/api/ingest/video endpoint in a detached background subprocess right after
a video is ingested, so a freshly-imported long-form video goes from 'auto'
to fully classified, broken down, visual-detected, and profiled with no
manual step — the video counterpart to auto_process_book.py.

The Class rubric is the only part of the response the profile fingerprint
actually depends on, so it's the only one treated as hard-fail
(needs_review, recorded in video_attributes.classification_error); the
chapter breakdown and visual-detection portions of the SAME response are
enrichment — a failure merging just one of them is logged and the pipeline
continues, the same "enrichment, never fail the whole job over it"
convention this codebase already uses (see diarize_audio() in
instagram_transcriber/app.py). A failure of the API call itself (or of the
classification portion specifically) is still all-or-nothing, since there's
only one call to retry.

Usage (manual/backfill):
    python3 auto_process_video.py --video_id 123
"""
import argparse
import json
import traceback

import classify_template as ct
import classify_video_breakdown as cvb
import classify_video_combined as cvc
import detect_video_visuals as dv
import profile_builder
from db_init import get_conn


def _mark_needs_review(video_id, reason):
    conn = get_conn()
    conn.execute(
        "UPDATE video_attributes SET classified_by = 'needs_review', classification_error = ? WHERE video_id = ?",
        (reason, video_id),
    )
    conn.commit()
    conn.close()
    print(f"[auto_process_video] video_id={video_id} needs review: {reason}")


def classify_and_build_profile(video_id):
    conn = get_conn()
    row = conn.execute(
        """SELECT v.video_id, v.title, v.script, v.timed_transcript_json, c.channel_id, c.channel_name
           FROM videos v JOIN channels c ON c.channel_id = v.channel_id
           WHERE v.video_id = ?""",
        (video_id,),
    ).fetchone()
    conn.close()
    if not row:
        print(f"[auto_process_video] video_id={video_id} not found")
        return
    if not row["script"]:
        _mark_needs_review(video_id, "No script stored for this video — nothing to classify.")
        return

    timed_segments = json.loads(row["timed_transcript_json"]) if row["timed_transcript_json"] else []

    # The single combined call — hard-fail, the profile fingerprint depends
    # on its classification portion and there's nothing to retry piecemeal.
    try:
        result = cvc.classify_video_combined(video_id, row["title"], row["script"], timed_segments)
    except Exception as e:
        _mark_needs_review(video_id, f"Anthropic API call failed (classification): {e}")
        traceback.print_exc()
        return

    errors = cvc.validate_combined_result(video_id, result)
    if errors:
        _mark_needs_review(video_id, "Classification failed validation: " + "; ".join(errors[:10]))
        return

    classification = dict(result.get("classification") or {})
    classification["video_id"] = video_id
    try:
        ct.merge_classification_results([classification])
    except Exception as e:
        _mark_needs_review(video_id, f"Merge classification failed: {e}")
        traceback.print_exc()
        return

    # Chapter breakdown (points/terms/examples/sections) — enrichment only
    try:
        breakdown_result = {"video_id": video_id, "summary": result.get("summary"), "sections": result.get("sections") or []}
        cvb.merge_longform_video_breakdown_results([breakdown_result])
    except Exception as e:
        print(f"[auto_process_video] video_id={video_id} breakdown merge failed (non-fatal): {e}")
        traceback.print_exc()

    # Significant graph/table visuals — enrichment only
    try:
        dv.merge_visuals(video_id, cvc.extract_visuals(result))
    except Exception as e:
        print(f"[auto_process_video] video_id={video_id} visuals merge failed (non-fatal): {e}")
        traceback.print_exc()

    # Profile build — auto-assigns a C.N code if this channel doesn't have one yet
    try:
        conn = get_conn()
        code = profile_builder.get_or_assign_profile_code(conn, row["channel_id"], prefix="C")
        conn.close()
        profile_builder.build_profile(row["channel_name"], code, "C", min_n=1)
    except Exception as e:
        print(f"[auto_process_video] video_id={video_id} profile build failed (non-fatal): {e}")
        traceback.print_exc()

    print(f"[auto_process_video] video_id={video_id} classified and profile rebuilt.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--video_id", type=int, required=True)
    args = ap.parse_args()
    classify_and_build_profile(args.video_id)
