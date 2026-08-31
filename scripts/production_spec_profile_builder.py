"""
production_spec_profile_builder.py — the shot/pacing-analysis counterpart to
profile_builder.py (writing-style videos) and book_profile_builder.py
(books). Rolls up a source's classified Production Spec inputs (shot-count,
avg shot length, pacing curve, shot-type mix) into a style_profiles
fingerprint, using media_type='ProductionSpec' and the 'PS.*' profile-code
prefix.

A source/creator (e.g. "Null.histories") is modeled as a channels row, the
same way a video channel or a book author is — production_spec_inputs.channel_id
already gets created via ingest_production_spec.py's get_or_create_channel, so
this file doesn't need its own channel-creation helper the way
book_profile_builder.py does for authors.

Usage:
    python3 production_spec_profile_builder.py --channel "Null.histories" --code PS.1
"""
import argparse
import json
import statistics

from db_init import get_conn

NUMERIC_ATTRS = [
    "total_shots", "avg_shot_length_sec", "median_shot_length_sec", "shot_length_stdev",
    "pct_shots_under_1s", "pct_shots_under_2s", "pct_shots_2to5s", "pct_shots_over_5s",
    "pct_illustration_panel", "pct_step_card", "pct_narrator_reaction",
    "pct_map_data_graphic", "pct_cta", "pct_other",
]

CATEGORICAL_ATTRS = ["dominant_shot_category"]


def get_or_create_profile(conn, code, channel_id):
    row = conn.execute("SELECT profile_id FROM style_profiles WHERE profile_code = ?", (code,)).fetchone()
    if row:
        return row["profile_id"]
    cur = conn.execute(
        "INSERT INTO style_profiles (profile_code, channel_id, length_band, media_type) VALUES (?, ?, 'PS', 'ProductionSpec')",
        (code, channel_id),
    )
    conn.commit()
    return cur.lastrowid


def build_profile(channel_name, profile_code, min_n=1):
    conn = get_conn()
    channel = conn.execute("SELECT channel_id FROM channels WHERE channel_name = ?", (channel_name,)).fetchone()
    if not channel:
        conn.close()
        raise ValueError(f"No channel found named '{channel_name}'.")
    channel_id = channel["channel_id"]

    # Dedupe by content_hash: the same source video has been submitted for
    # shot analysis twice before (a known duplicate-submission issue), which
    # would otherwise double-count its shot-pacing numbers in every mean/std
    # below. Inputs sharing a hash keep only the earliest (MIN input_id); a
    # NULL hash (hashing failed) is never collapsed with another NULL — each
    # forms its own singleton group via the input_id fallback key.
    rows = conn.execute(
        """SELECT a.*, i.title FROM production_spec_attributes a
           JOIN production_spec_inputs i ON i.input_id = a.input_id
           WHERE i.channel_id = ? AND a.classified_by IS NOT NULL AND a.classified_by != 'pending'
             AND i.input_id IN (
               SELECT MIN(input_id) FROM production_spec_inputs WHERE channel_id = ?
               GROUP BY COALESCE(content_hash, 'i' || input_id)
             )""",
        (channel_id, channel_id),
    ).fetchall()
    n = len(rows)

    existing = conn.execute("SELECT profile_id FROM style_profiles WHERE profile_code = ?", (profile_code,)).fetchone()

    if n == 0:
        if not existing:
            print(f"No classified Production Spec inputs found for '{channel_name}' yet.")
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
        print(f"No classified Production Spec inputs left for '{channel_name}' — cleared fingerprint for profile {profile_code}.")
        return profile_id

    profile_id = get_or_create_profile(conn, profile_code, channel_id)

    # full replace, same reasoning profile_builder.build_profile/book_profile_builder.build_author_profile use
    conn.execute("DELETE FROM profile_fingerprint_numeric WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM profile_fingerprint_categorical WHERE profile_id = ?", (profile_id,))

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

    for attr in CATEGORICAL_ATTRS:
        vals = [r[attr] for r in rows if r[attr]]
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

    titles = ", ".join(sorted({r["title"] for r in rows if r["title"]}))
    overview = f"Shot/pacing analysis of: {titles}." if titles else None

    status = "confirmed" if n >= min_n else "draft"
    conn.execute(
        """UPDATE style_profiles SET n_videos_analysed = ?, status = ?, overview = COALESCE(?, overview),
           updated_at = datetime('now') WHERE profile_id = ?""",
        (n, status, overview, profile_id),
    )
    conn.commit()
    conn.close()
    print(f"Built fingerprint for profile {profile_code} from {n} Production Spec input(s) for '{channel_name}' (status={status}).")
    return profile_id


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True)
    ap.add_argument("--code", required=True, help="e.g. PS.1 — use profile_builder.get_or_assign_profile_code(conn, channel_id, prefix='PS') to auto-assign")
    ap.add_argument("--min_n", type=int, default=1)
    args = ap.parse_args()
    build_profile(args.channel, args.code, args.min_n)
