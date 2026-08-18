"""
auto_process_video.py — runs the full long-form video pipeline
automatically: the same sequence of steps that used to be done by hand for
the first YouTube video (export the rubric prompt, paste into Claude, save
the reply, --load it, then the same again for the chapter breakdown, then
profile_builder.py).

classify_and_build_profile(video_id) is called from webapp.py's
/api/ingest/video endpoint in a detached background subprocess right after
a video is ingested, so a freshly-imported long-form video goes from 'auto'
to fully classified, broken down, visual-detected, and profiled with no
manual step — the video counterpart to auto_process_book.py.

The Class rubric (step 1) is the only stage the profile fingerprint
actually depends on, so it's the only one treated as hard-fail
(needs_review, recorded in video_attributes.classification_error); the
chapter breakdown and visual-detection stages are enrichment — a failure
there is logged and the pipeline continues, the same "enrichment, never
fail the whole job over it" convention this codebase already uses (see
diarize_audio() in instagram_transcriber/app.py).

Usage (manual/backfill):
    python3 auto_process_video.py --video_id 123
"""
import argparse
import traceback

import classify_template as ct
import classify_video_breakdown as cvb
import detect_video_visuals
import llm_client
import profile_builder
from db_init import get_conn

CLASSIFICATION_MAX_TOKENS = 8192
BREAKDOWN_MAX_TOKENS = 16000


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
        """SELECT v.video_id, v.title, v.script, c.channel_id, c.channel_name
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

    # Step 1: Class rubric — hard-fail, the profile fingerprint depends on this
    prompt = ct.build_prompt([row])
    try:
        results = llm_client.generate_json(prompt, max_tokens=CLASSIFICATION_MAX_TOKENS)
    except Exception as e:
        _mark_needs_review(video_id, f"Anthropic API call failed (classification): {e}")
        traceback.print_exc()
        return

    results = ct.normalize_classification_results(results)
    errors = ct.validate_classification(results)
    if errors:
        _mark_needs_review(video_id, "Classification failed validation: " + "; ".join(errors[:10]))
        return

    try:
        ct.merge_classification_results(results)
    except Exception as e:
        _mark_needs_review(video_id, f"Merge classification failed: {e}")
        traceback.print_exc()
        return

    # Step 2: chapter-aware breakdown (points/terms/examples/sections) — enrichment only
    try:
        breakdown_prompt = cvb.build_longform_prompt([row])
        breakdown_results = llm_client.generate_json(breakdown_prompt, max_tokens=BREAKDOWN_MAX_TOKENS)
        if isinstance(breakdown_results, dict):
            breakdown_results = [breakdown_results]
        cvb.merge_longform_video_breakdown_results(breakdown_results)
    except Exception as e:
        print(f"[auto_process_video] video_id={video_id} breakdown step failed (non-fatal): {e}")
        traceback.print_exc()

    # Step 3: significant graph/table visual detection — enrichment only
    try:
        visuals = detect_video_visuals.detect_visuals(video_id)
        detect_video_visuals.merge_visuals(video_id, visuals)
    except Exception as e:
        print(f"[auto_process_video] video_id={video_id} visual detection failed (non-fatal): {e}")
        traceback.print_exc()

    # Step 4: profile build — auto-assigns a C.N code if this channel doesn't have one yet
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
