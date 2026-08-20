"""
auto_process_shortform_video.py — the short-form (Instagram) counterpart to
auto_process_video.py. Short-form Reels are too brief to have chapters or
on-screen data visuals, so unlike the long-form pipeline this is just one
Class-rubric LLM call (classify_template.py's existing short-form prompt,
the same one previously only run by hand via its --export/--load workflow)
followed by a channel profile rebuild — no breakdown or visual-detection
steps.

classify_and_build_profile(video_id) is called from webapp.py's
/api/ingest/video endpoint in a detached background subprocess right after
a non-YouTube video is ingested, mirroring auto_process_video.py's role for
long-form content — so a freshly-imported Instagram video goes from 'auto'
to fully classified and profiled with no manual step.

Usage (manual/backfill):
    python3 auto_process_shortform_video.py --video_id 123
"""
import argparse
import traceback

import llm_client
import profile_builder
from classify_template import (
    build_prompt,
    merge_classification_results,
    normalize_classification_results,
    validate_classification,
)
from db_init import get_conn

CLASSIFICATION_MAX_TOKENS = 12000


def _mark_needs_review(video_id, reason):
    conn = get_conn()
    conn.execute(
        "UPDATE video_attributes SET classified_by = 'needs_review', classification_error = ? WHERE video_id = ?",
        (reason, video_id),
    )
    conn.commit()
    conn.close()
    print(f"[auto_process_shortform_video] video_id={video_id} needs review: {reason}")


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
        print(f"[auto_process_shortform_video] video_id={video_id} not found")
        return
    if not row["script"]:
        _mark_needs_review(video_id, "No script stored for this video — nothing to classify.")
        return

    prompt = build_prompt([row])
    try:
        result = llm_client.generate_json(prompt, max_tokens=CLASSIFICATION_MAX_TOKENS)
    except Exception as e:
        _mark_needs_review(video_id, f"Anthropic API call failed (classification): {e}")
        traceback.print_exc()
        return

    results = normalize_classification_results(result)
    errors = validate_classification(results)
    if errors:
        _mark_needs_review(video_id, "Classification failed validation: " + "; ".join(errors[:10]))
        return

    try:
        merge_classification_results(results)
    except Exception as e:
        _mark_needs_review(video_id, f"Merge classification failed: {e}")
        traceback.print_exc()
        return

    # Profile build — auto-assigns an A.N code if this channel doesn't have one yet
    try:
        conn = get_conn()
        code = profile_builder.get_or_assign_profile_code(conn, row["channel_id"], prefix="A")
        conn.close()
        profile_builder.build_profile(row["channel_name"], code, "A", min_n=1)
    except Exception as e:
        print(f"[auto_process_shortform_video] video_id={video_id} profile build failed (non-fatal): {e}")
        traceback.print_exc()

    print(f"[auto_process_shortform_video] video_id={video_id} classified and profile rebuilt.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--video_id", type=int, required=True)
    args = ap.parse_args()
    classify_and_build_profile(args.video_id)
