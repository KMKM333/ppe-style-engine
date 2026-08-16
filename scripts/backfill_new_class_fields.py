"""
backfill_new_class_fields.py — targeted backfill for the 29 new Class
(LLM-judged) fields added to video_attributes this session, for videos that
were ALREADY classified before those fields existed. Unlike
classify_template.py's merge_classification() (which overwrites every
CLASS_FIELDS column), this only ever touches the new fields — the original
classification (hook_type, beat_sequence, etc.) is left untouched.

Safety: refuses to write into any video whose hook_type is NULL (i.e. a
video that's never been classified at all — that's classify_template.py's
job, not this backfill's).

Usage:
    python3 backfill_new_class_fields.py --export --channel fryrsquared --limit 10
    # classify the printed batch, save JSON array (video_id + new fields only)
    python3 backfill_new_class_fields.py --load results.json
"""
import argparse
import json

from db_init import get_conn

NEW_VIDEO_FIELDS = [
    # ported from the book rubric
    "tone", "emotional_register", "narrative_voice", "narrative_density",
    "counter_argument_engagement", "rhetorical_appeal_balance", "prose_rhythm",
    "noun_verb_ratio_style", "syntax_pattern", "pacing", "polemical_tone", "narrative_presence",
    # new shared fields
    "value_promise", "information_density", "curiosity_loop", "relatability_factor",
    "identity_framing", "contrarian_positioning", "adjective_intensity", "punctuation_delivery",
    "rhythmic_repetition", "vulnerability_depth", "condescension_vs_empowerment",
    # new video-only fields
    "structure_archetype", "shareability_trigger", "product_placement",
    "core_value_reinforcement", "status_signaling", "niche_slang_usage",
]


def export_for_backfill(channel_name, limit=50):
    conn = get_conn()
    rows = conn.execute(
        """SELECT v.video_id, v.title, v.script
           FROM videos v
           JOIN video_attributes a ON a.video_id = v.video_id
           JOIN channels c ON c.channel_id = v.channel_id
           WHERE c.channel_name = ? AND a.hook_type IS NOT NULL AND a.structure_archetype IS NULL
           ORDER BY v.video_id LIMIT ?""",
        (channel_name, limit),
    ).fetchall()
    conn.close()
    return rows


def merge_new_fields(json_path):
    with open(json_path) as f:
        results = json.load(f)

    conn = get_conn()
    n = 0
    skipped = 0
    for r in results:
        video_id = r["video_id"]
        existing = conn.execute(
            "SELECT hook_type FROM video_attributes WHERE video_id = ?", (video_id,)
        ).fetchone()
        if not existing or existing["hook_type"] is None:
            print(f"SKIP video_id={video_id}: not already classified (hook_type is NULL)")
            skipped += 1
            continue
        keys = [k for k in NEW_VIDEO_FIELDS if k in r]
        if not keys:
            continue
        set_clause = ", ".join([f"{k} = ?" for k in keys])
        values = [r[k] for k in keys]
        conn.execute(
            f"UPDATE video_attributes SET {set_clause} WHERE video_id = ?",
            (*values, video_id),
        )
        n += 1
    conn.commit()
    conn.close()
    print(f"Backfilled new fields for {n} video(s), skipped {skipped}.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--channel", default=None)
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--load", default=None)
    args = ap.parse_args()

    if args.export:
        rows = export_for_backfill(args.channel, args.limit)
        for r in rows:
            print(f'--- video_id: {r["video_id"]} ---\nTitle: {r["title"]}\nScript:\n{r["script"]}\n')
    elif args.load:
        merge_new_fields(args.load)
    else:
        print("Use --export --channel <name> or --load results.json")
