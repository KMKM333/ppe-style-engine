"""
classify_production_spec_shots.py — the Production Spec counterpart to
auto_process_shortform_video.py: classifies each shot's content category
from its extracted frame image via Claude vision, computes the input's
aggregate shot/pacing attributes, then rebuilds that source's PS.* profile.

Runs as a detached subprocess launched by webapp.py's
/api/production-spec/inputs/<id>/classify route, after the Instagram Bulk
Transcriber has uploaded every shot's frame image.

Usage:
    python3 classify_production_spec_shots.py --input_id 1
"""
import argparse
import json
import statistics
import traceback

import llm_client
import profile_builder
import production_spec_profile_builder
from db_init import get_conn, PRODUCTION_SPEC_SHOTS_DIR

BATCH_SIZE = 15  # shots per vision call — keeps request size/latency bounded

CONTENT_CATEGORIES = [
    "illustration_panel", "step_card", "narrator_reaction", "map_data_graphic", "cta", "other",
]

CLASSIFY_PROMPT = """You are analysing shots from a short-form explainer video, in the
style of AI-illustrated history/education channels (e.g. an illustrated panel with
Ken-Burns pan/zoom, a "Step N" chapter-divider card, a recurring narrator character
reacting to camera, a map/chart/data graphic, or a closing call-to-action).

Below are {n} frame images, one representative frame per shot, in order. For EACH
image, classify its content category as exactly one of:
- "illustration_panel" — a static illustrated scene (people, places, historical moments)
- "step_card" — a plain background with a "Step N" or similar divider/title card
- "narrator_reaction" — a single recurring character alone on a plain background, a
  reaction/emphasis beat
- "map_data_graphic" — a map, chart, graph, or other data visualization
- "cta" — a call-to-action / product-plug / sponsor shot
- "other" — anything that doesn't clearly fit the above

Return a raw JSON array, one object per image in the same order, each shaped exactly:
{{"shot_number": <int>, "content_category": "<one of the six categories above>"}}

The shot_numbers for these images, in order, are: {shot_numbers}
Return ONLY the JSON array, no commentary, no markdown fence."""


def _mark_needs_review(input_id, reason):
    conn = get_conn()
    conn.execute(
        "UPDATE production_spec_inputs SET status = 'needs_review', classification_error = ? WHERE input_id = ?",
        (reason, input_id),
    )
    conn.commit()
    conn.close()
    print(f"[classify_production_spec_shots] input_id={input_id} needs review: {reason}")


def classify_and_build_profile(input_id, batch_size=BATCH_SIZE):
    conn = get_conn()
    input_row = conn.execute(
        """SELECT i.input_id, i.title, i.channel_id, c.channel_name
           FROM production_spec_inputs i JOIN channels c ON c.channel_id = i.channel_id
           WHERE i.input_id = ?""",
        (input_id,),
    ).fetchone()
    if not input_row:
        conn.close()
        print(f"[classify_production_spec_shots] input_id={input_id} not found")
        return

    shots = conn.execute(
        "SELECT shot_id, shot_number, start_sec, end_sec, duration_sec, frame_captured "
        "FROM production_spec_shots WHERE input_id = ? ORDER BY shot_number",
        (input_id,),
    ).fetchall()
    conn.close()

    if not shots:
        _mark_needs_review(input_id, "No shots recorded for this input.")
        return
    missing_frames = [s["shot_number"] for s in shots if not s["frame_captured"]]
    if missing_frames:
        _mark_needs_review(input_id, f"Frames missing for shot(s): {missing_frames[:10]}")
        return

    # --- classify each shot's content category, in bounded-size batches ---
    shot_dir = PRODUCTION_SPEC_SHOTS_DIR / str(input_id)
    category_by_shot = {}
    for i in range(0, len(shots), batch_size):
        batch = shots[i:i + batch_size]
        images = []
        for s in batch:
            frame_path = shot_dir / f"shot_{s['shot_id']}.png"
            if not frame_path.is_file():
                _mark_needs_review(input_id, f"Missing frame file for shot_id={s['shot_id']}.")
                return
            images.append(("image/png", frame_path.read_bytes()))
        shot_numbers = [s["shot_number"] for s in batch]
        prompt = CLASSIFY_PROMPT.format(n=len(batch), shot_numbers=shot_numbers)
        try:
            result = llm_client.generate_json_with_images(prompt, images, max_tokens=2048)
        except Exception as e:
            _mark_needs_review(input_id, f"Anthropic API call failed (shot classification, batch starting shot {shot_numbers[0]}): {e}")
            traceback.print_exc()
            return
        if not isinstance(result, list):
            _mark_needs_review(input_id, f"Shot classification didn't return a JSON array (batch starting shot {shot_numbers[0]}).")
            return
        for item in result:
            cat = item.get("content_category")
            if cat not in CONTENT_CATEGORIES:
                cat = "other"
            category_by_shot[item.get("shot_number")] = cat

    conn = get_conn()
    for s in shots:
        cat = category_by_shot.get(s["shot_number"], "other")
        conn.execute(
            "UPDATE production_spec_shots SET content_category = ?, classified_by = 'claude' WHERE shot_id = ?",
            (cat, s["shot_id"]),
        )
    conn.commit()
    conn.close()

    # --- compute aggregate production_spec_attributes for this input ---
    durations = [s["duration_sec"] for s in shots if s["duration_sec"] is not None]
    n_shots = len(shots)
    n_dur = len(durations)

    def pct(pred):
        return round(100 * sum(1 for d in durations if pred(d)) / n_dur, 1) if n_dur else None

    categories = [category_by_shot.get(s["shot_number"], "other") for s in shots]

    def pct_cat(cat):
        return round(100 * sum(1 for c in categories if c == cat) / n_shots, 1) if n_shots else None

    dominant = statistics.mode(categories) if categories else None
    pacing_curve = [s["duration_sec"] for s in shots]

    conn = get_conn()
    conn.execute(
        """INSERT INTO production_spec_attributes
           (input_id, total_shots, avg_shot_length_sec, median_shot_length_sec, shot_length_stdev,
            pct_shots_under_1s, pct_shots_under_2s, pct_shots_2to5s, pct_shots_over_5s,
            pct_illustration_panel, pct_step_card, pct_narrator_reaction, pct_map_data_graphic,
            pct_cta, pct_other, dominant_shot_category, pacing_curve_json, classified_by)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'claude')
           ON CONFLICT(input_id) DO UPDATE SET
             total_shots=excluded.total_shots, avg_shot_length_sec=excluded.avg_shot_length_sec,
             median_shot_length_sec=excluded.median_shot_length_sec, shot_length_stdev=excluded.shot_length_stdev,
             pct_shots_under_1s=excluded.pct_shots_under_1s, pct_shots_under_2s=excluded.pct_shots_under_2s,
             pct_shots_2to5s=excluded.pct_shots_2to5s, pct_shots_over_5s=excluded.pct_shots_over_5s,
             pct_illustration_panel=excluded.pct_illustration_panel, pct_step_card=excluded.pct_step_card,
             pct_narrator_reaction=excluded.pct_narrator_reaction, pct_map_data_graphic=excluded.pct_map_data_graphic,
             pct_cta=excluded.pct_cta, pct_other=excluded.pct_other,
             dominant_shot_category=excluded.dominant_shot_category, pacing_curve_json=excluded.pacing_curve_json,
             classified_by='claude', classified_at=datetime('now')""",
        (input_id, n_shots,
         round(statistics.mean(durations), 2) if durations else None,
         round(statistics.median(durations), 2) if durations else None,
         round(statistics.pstdev(durations), 2) if len(durations) > 1 else 0.0,
         pct(lambda d: d < 1), pct(lambda d: d < 2), pct(lambda d: 2 <= d <= 5), pct(lambda d: d > 5),
         pct_cat("illustration_panel"), pct_cat("step_card"), pct_cat("narrator_reaction"),
         pct_cat("map_data_graphic"), pct_cat("cta"), pct_cat("other"),
         dominant, json.dumps(pacing_curve)),
    )
    conn.execute(
        "UPDATE production_spec_inputs SET status = 'classified', classification_error = NULL WHERE input_id = ?",
        (input_id,),
    )
    conn.commit()
    conn.close()

    # --- rebuild the source's PS.* profile ---
    try:
        conn = get_conn()
        code = profile_builder.get_or_assign_profile_code(conn, input_row["channel_id"], prefix="PS")
        conn.close()
        production_spec_profile_builder.build_profile(input_row["channel_name"], code, min_n=1)
    except Exception as e:
        print(f"[classify_production_spec_shots] input_id={input_id} profile build failed (non-fatal): {e}")
        traceback.print_exc()

    print(f"[classify_production_spec_shots] input_id={input_id} classified ({n_shots} shots) and profile rebuilt.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--input_id", type=int, required=True)
    ap.add_argument("--batch_size", type=int, default=BATCH_SIZE,
                     help="Shots per vision call — lower this for a stubborn input that keeps failing "
                          "at the default batch size (e.g. an empty/malformed reply from Claude).")
    args = ap.parse_args()
    classify_and_build_profile(args.input_id, batch_size=args.batch_size)
