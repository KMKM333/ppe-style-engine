"""
profile_builder.py — rolls up all analysed videos for a channel into a
style_profile fingerprint: mean/std/median/min/max for numeric attributes,
and % share for categorical attributes. This is Section 11 of the template,
computed automatically.

Usage:
    python3 profile_builder.py --channel "Philosophyminis" --code A.1 --length_band A

Profile code prefix follows length_band: A.* for short-form (Instagram),
BK.* for books, C.* for long-form (YouTube) — e.g.:
    python3 profile_builder.py --channel "Johnny Harris" --code C.1 --length_band C --min_n 1
"""
import argparse
import json
import statistics
import sqlite3

from db_init import get_conn

NUMERIC_ATTRS = [
    "word_count", "beat_count", "avg_sentence_len", "median_sentence_len",
    "sentence_len_variance", "you_freq_per_100w", "i_freq_per_100w", "we_freq_per_100w",
    "question_count", "emdash_count", "quote_count", "jargon_density",
    "readability_score", "filler_count", "word_economy_ratio", "title_word_count",
    "analogy_count", "rule_of_three_count", "cta_count",
    "number_count", "instruction_verb_count", "framework_marker_count",
    "sentence_rhythm_cv", "closing_paragraph_ratio", "lexical_diversity",
    "punctuation_density", "colloquialism_density", "contrast_structure_count",
    "named_entity_count", "humor_marker_count",
    # long-form-only fields — always NULL/absent on short-form videos, so
    # they simply never contribute to an Instagram (A.*) profile's fingerprint
    "chapter_count", "intro_length_sec", "act_count", "re_engagement_hook_count",
    "outro_cta_count", "topic_shift_count",
]

CATEGORICAL_ATTRS = [
    "title_format", "hook_type", "close_type", "citation_style",
    "certainty_register", "domain", "concept_type", "source_era",
    "framing", "script_polish", "cta_type", "cta_placement",
    "beat_sequence", "explanation_mechanism", "rhetorical_mode",
    # ported from books
    "tone", "emotional_register", "narrative_voice", "narrative_density",
    "counter_argument_engagement", "rhetorical_appeal_balance", "prose_rhythm",
    "noun_verb_ratio_style", "syntax_pattern", "pacing", "polemical_tone", "narrative_presence",
    # new shared fields
    "value_promise", "information_density", "relatability_factor", "contrarian_positioning",
    "adjective_intensity", "punctuation_delivery", "vulnerability_depth", "condescension_vs_empowerment",
    # new video-only fields
    "structure_archetype", "shareability_trigger", "product_placement",
    "status_signaling", "niche_slang_usage",
    # long-form-only fields
    "sponsor_segment_position", "outro_type", "pacing_arc",
]


def get_or_assign_profile_code(conn, channel_id, prefix="C"):
    """Returns the channel's existing style_profiles.profile_code if it
    already has one, otherwise assigns the next unused '{prefix}.N' code
    (e.g. C.1, C.2, C.3...) — the same period-separated convention already
    established for A.* (Instagram) and BK.* (books). Lets the automated
    video pipeline build a profile without a human picking a code by hand,
    the way the first YouTube channel's C.1 was assigned manually."""
    row = conn.execute(
        "SELECT profile_code FROM style_profiles WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    if row:
        return row["profile_code"]

    existing_codes = [
        r["profile_code"] for r in conn.execute(
            "SELECT profile_code FROM style_profiles WHERE profile_code LIKE ?", (f"{prefix}.%",)
        ).fetchall()
    ]
    max_n = 0
    for code in existing_codes:
        suffix = code.split(".", 1)[1]
        if suffix.isdigit():
            max_n = max(max_n, int(suffix))
    return f"{prefix}.{max_n + 1}"


def get_or_create_profile(conn, code, channel_id, length_band):
    row = conn.execute("SELECT profile_id FROM style_profiles WHERE profile_code = ?", (code,)).fetchone()
    if row:
        return row["profile_id"]
    media_type_row = conn.execute(
        """SELECT media_type FROM videos WHERE channel_id = ? AND media_type IS NOT NULL
           GROUP BY media_type ORDER BY COUNT(*) DESC LIMIT 1""",
        (channel_id,),
    ).fetchone()
    media_type = media_type_row["media_type"] if media_type_row else "Instagram"
    cur = conn.execute(
        "INSERT INTO style_profiles (profile_code, channel_id, length_band, media_type) VALUES (?, ?, ?, ?)",
        (code, channel_id, length_band, media_type),
    )
    conn.commit()
    return cur.lastrowid


def build_profile(channel_name, profile_code, length_band, min_n=10):
    """(Re)builds a profile's fingerprint from scratch — a full replace, not
    a merge. This matters when re-running after deleting videos: if an
    attribute (or the whole channel) has lost all its data, the OLD
    fingerprint rows for it must disappear too, not just fail to update.
    Without this, a deleted video's numbers keep silently pulling on the
    averages, macro-attribute coverage, and every score computed against
    this profile."""
    conn = get_conn()
    ch = conn.execute("SELECT channel_id FROM channels WHERE channel_name = ?", (channel_name,)).fetchone()
    if not ch:
        raise ValueError(f"No such channel: {channel_name}")
    channel_id = ch["channel_id"]

    rows = conn.execute(
        """SELECT a.* FROM video_attributes a
           JOIN videos v ON v.video_id = a.video_id
           WHERE v.channel_id = ?""",
        (channel_id,),
    ).fetchall()
    n = len(rows)

    existing = conn.execute("SELECT profile_id FROM style_profiles WHERE profile_code = ?", (profile_code,)).fetchone()

    if n == 0:
        if not existing:
            print("No videos with attributes found for this channel yet.")
            conn.close()
            return None
        profile_id = existing["profile_id"]
        conn.execute("DELETE FROM profile_fingerprint_numeric WHERE profile_id = ?", (profile_id,))
        conn.execute("DELETE FROM profile_fingerprint_categorical WHERE profile_id = ?", (profile_id,))
        conn.execute(
            "UPDATE style_profiles SET n_videos_analysed = 0, status = 'draft', updated_at = datetime('now') WHERE profile_id = ?",
            (profile_id,),
        )
        conn.commit()
        conn.close()
        print(f"No videos left for '{channel_name}' — cleared fingerprint for profile {profile_code}.")
        return profile_id

    profile_id = get_or_create_profile(conn, profile_code, channel_id, length_band)

    # full replace: clear the profile's existing fingerprint before
    # recomputing, so attributes that lost all their data (or videos that
    # were deleted) don't leave stale rows behind
    conn.execute("DELETE FROM profile_fingerprint_numeric WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM profile_fingerprint_categorical WHERE profile_id = ?", (profile_id,))

    # numeric fingerprint
    for attr in NUMERIC_ATTRS:
        vals = [r[attr] for r in rows if r[attr] is not None]
        if not vals:
            continue
        mean_v = statistics.mean(vals)
        std_v = statistics.pstdev(vals) if len(vals) > 1 else 0.0
        sorted_vals = sorted(vals)
        conn.execute(
            """INSERT INTO profile_fingerprint_numeric
               (profile_id, attribute, mean_val, std_val, min_val, max_val, median_val, values_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(profile_id, attribute) DO UPDATE SET
                 mean_val=excluded.mean_val, std_val=excluded.std_val,
                 min_val=excluded.min_val, max_val=excluded.max_val,
                 median_val=excluded.median_val, values_json=excluded.values_json""",
            (profile_id, attr, mean_v, std_v, min(vals), max(vals), statistics.median(vals),
             json.dumps(sorted_vals)),
        )

    # categorical fingerprint (share % per observed value)
    for attr in CATEGORICAL_ATTRS:
        vals = [r[attr] for r in rows if r[attr] is not None]
        if not vals:
            continue
        counts = {}
        for v in vals:
            counts[v] = counts.get(v, 0) + 1
        for v, c in counts.items():
            share = round(c / len(vals) * 100, 1)
            conn.execute(
                """INSERT INTO profile_fingerprint_categorical
                   (profile_id, attribute, value, share_pct)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(profile_id, attribute, value) DO UPDATE SET share_pct=excluded.share_pct""",
                (profile_id, attr, v, share),
            )

    status = "confirmed" if n >= min_n else "draft"
    conn.execute(
        "UPDATE style_profiles SET n_videos_analysed = ?, status = ?, updated_at = datetime('now') WHERE profile_id = ?",
        (n, status, profile_id),
    )
    conn.commit()
    conn.close()
    print(f"Built fingerprint for profile {profile_code} from {n} videos (status={status}).")
    return profile_id


def show_fingerprint(profile_code):
    conn = get_conn()
    p = conn.execute("SELECT * FROM style_profiles WHERE profile_code = ?", (profile_code,)).fetchone()
    if not p:
        print("No such profile.")
        return
    print(f"\n=== Style Profile {profile_code} (n={p['n_videos_analysed']}, status={p['status']}) ===\n")
    print("-- Numeric --")
    for r in conn.execute(
        "SELECT * FROM profile_fingerprint_numeric WHERE profile_id = ? ORDER BY attribute", (p["profile_id"],)
    ):
        print(f"  {r['attribute']:28s} mean={r['mean_val']:.2f}  std={r['std_val']:.2f}  "
              f"median={r['median_val']:.2f}  range=[{r['min_val']:.1f}, {r['max_val']:.1f}]")
    print("\n-- Categorical (top values) --")
    cur = conn.execute(
        "SELECT * FROM profile_fingerprint_categorical WHERE profile_id = ? ORDER BY attribute, share_pct DESC",
        (p["profile_id"],),
    )
    last_attr = None
    for r in cur:
        if r["attribute"] != last_attr:
            print(f"  {r['attribute']}:")
            last_attr = r["attribute"]
        print(f"      {r['value']:40s} {r['share_pct']:.1f}%")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True)
    ap.add_argument("--code", required=True, help="profile code, e.g. A.1")
    ap.add_argument("--length_band", default="A")
    ap.add_argument("--min_n", type=int, default=10)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    build_profile(args.channel, args.code, args.length_band, args.min_n)
    if args.show:
        show_fingerprint(args.code)
