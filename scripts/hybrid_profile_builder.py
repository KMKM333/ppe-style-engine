"""
hybrid_profile_builder.py — synthesizes a new "hybrid" style_profile by
blending two or more existing profiles' fingerprints, weighted.

Used by the Rewrite/Transform screen's "build a hybrid target" option: pick
N source profiles (any mix of video-channel and book-author profiles) plus
relative weights, and this produces a brand-new style_profiles row
(profile_code 'H.N', channel platform='Hybrid') whose numeric fingerprint is
the weighted blend of each source's mean/median/min/max/std per attribute,
and whose categorical fingerprint is the weighted blend of each source's
value shares. The result is a normal, immediately-confirmed style_profiles
row, so it needs no special-casing downstream — transform.build_transform_prompt()
and score_engine.score_against_profiles() consume it exactly like any
observed profile.

Usage:
    python3 hybrid_profile_builder.py --profiles A.1,BK.9 --weights 60,40
    python3 hybrid_profile_builder.py --profiles A.1,BK.9,A.3   # equal weights
"""
import argparse
import json

from db_init import get_conn


def _next_hybrid_code(conn):
    rows = conn.execute("SELECT profile_code FROM style_profiles WHERE profile_code LIKE 'H.%'").fetchall()
    used = [int(r["profile_code"].split(".")[1]) for r in rows if r["profile_code"].split(".")[1].isdigit()]
    return f"H.{max(used, default=0) + 1}"


def create_hybrid_profile(profile_codes: list[str], weights: list[float] | None = None) -> str:
    profile_codes = [c.strip() for c in profile_codes if c.strip()]
    if len(profile_codes) < 2:
        raise ValueError("Pick at least 2 source profiles to build a hybrid.")
    if len(set(profile_codes)) != len(profile_codes):
        raise ValueError("Source profiles must be distinct.")

    conn = get_conn()
    sources = []
    for code in profile_codes:
        row = conn.execute(
            """SELECT p.*, c.channel_name FROM style_profiles p
               JOIN channels c ON c.channel_id = p.channel_id
               WHERE p.profile_code = ?""",
            (code,),
        ).fetchone()
        if not row:
            conn.close()
            raise ValueError(f"No such profile: {code}")
        sources.append(dict(row))

    if weights is None:
        weights = [1.0] * len(sources)
    if len(weights) != len(sources):
        conn.close()
        raise ValueError("weights must match profile_codes 1:1")
    total_w = sum(weights)
    if total_w <= 0:
        conn.close()
        raise ValueError("weights must sum to a positive number")
    weights = [w / total_w for w in weights]  # normalise to fractions summing to 1

    # --- virtual "Hybrid" channel + profile row ---
    label = " + ".join(f"{s['profile_code']} {round(w * 100)}%" for s, w in zip(sources, weights))
    channel_name = f"Hybrid: {label}"
    ch = conn.execute("SELECT channel_id FROM channels WHERE channel_name = ?", (channel_name,)).fetchone()
    if ch:
        channel_id = ch["channel_id"]
    else:
        cur = conn.execute(
            "INSERT INTO channels (channel_name, platform, typical_length_band) VALUES (?, 'Hybrid', 'H')",
            (channel_name,),
        )
        channel_id = cur.lastrowid

    code = _next_hybrid_code(conn)
    n_backing = sum((s["n_videos_analysed"] or 0) * w for s, w in zip(sources, weights))
    overview = "Synthesized hybrid style blending: " + "; ".join(
        f"{s['profile_code']} ({s['channel_name']}, {round(w * 100)}%)" for s, w in zip(sources, weights)
    )
    cur = conn.execute(
        """INSERT INTO style_profiles
           (profile_code, channel_id, length_band, n_videos_analysed, status, overview, media_type)
           VALUES (?, ?, 'H', ?, 'confirmed', ?, 'Hybrid')""",
        (code, channel_id, int(round(n_backing)), overview),
    )
    profile_id = cur.lastrowid

    conn.execute("DELETE FROM style_profile_hybrid_sources WHERE profile_id = ?", (profile_id,))
    for s, w in zip(sources, weights):
        conn.execute(
            "INSERT INTO style_profile_hybrid_sources (profile_id, source_profile_id, weight_pct) VALUES (?, ?, ?)",
            (profile_id, s["profile_id"], round(w * 100, 1)),
        )

    # --- blend numeric fingerprint: weighted average of each stat, renormalised
    # per-attribute across only the sources that actually have that attribute ---
    numeric_by_attr = {}
    for s, w in zip(sources, weights):
        for r in conn.execute("SELECT * FROM profile_fingerprint_numeric WHERE profile_id = ?", (s["profile_id"],)):
            numeric_by_attr.setdefault(r["attribute"], []).append((dict(r), w))

    for attr, rows in numeric_by_attr.items():
        wsum = sum(w for _, w in rows)
        if wsum <= 0:
            continue

        def blend(field, rows=rows, wsum=wsum):
            return sum((r[field] or 0) * w for r, w in rows) / wsum

        merged_vals = sorted(v for r, _ in rows if r["values_json"] for v in json.loads(r["values_json"]))
        conn.execute(
            """INSERT INTO profile_fingerprint_numeric
               (profile_id, attribute, mean_val, std_val, min_val, max_val, median_val, values_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(profile_id, attribute) DO UPDATE SET
                 mean_val=excluded.mean_val, std_val=excluded.std_val, min_val=excluded.min_val,
                 max_val=excluded.max_val, median_val=excluded.median_val, values_json=excluded.values_json""",
            (profile_id, attr, round(blend("mean_val"), 3), round(blend("std_val"), 3),
             round(blend("min_val"), 3), round(blend("max_val"), 3), round(blend("median_val"), 3),
             json.dumps(merged_vals) if merged_vals else None),
        )

    # --- blend categorical fingerprint: weighted share_pct per value, renormalised
    # per-attribute across only the sources that classified that attribute ---
    cat_by_attr = {}
    for s, w in zip(sources, weights):
        source_attrs = set()
        for r in conn.execute("SELECT * FROM profile_fingerprint_categorical WHERE profile_id = ?", (s["profile_id"],)):
            cat_by_attr.setdefault(r["attribute"], {"rows": [], "source_weights": {}})
            cat_by_attr[r["attribute"]]["rows"].append((dict(r), w))
            source_attrs.add(r["attribute"])
        # one weight per DISTINCT contributing source per attribute, not per value-row,
        # since a source contributes several rows (one per categorical value) per attribute
        for attr in source_attrs:
            cat_by_attr[attr]["source_weights"][s["profile_id"]] = w

    for attr, bucket in cat_by_attr.items():
        wsum = sum(bucket["source_weights"].values())
        if wsum <= 0:
            continue
        share_by_value = {}
        for r, w in bucket["rows"]:
            share_by_value[r["value"]] = share_by_value.get(r["value"], 0.0) + (r["share_pct"] or 0.0) * w
        for value, share in share_by_value.items():
            conn.execute(
                """INSERT INTO profile_fingerprint_categorical (profile_id, attribute, value, share_pct)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(profile_id, attribute, value) DO UPDATE SET share_pct=excluded.share_pct""",
                (profile_id, attr, value, round(share / wsum, 1)),
            )

    conn.commit()
    conn.close()
    print(f"Created hybrid profile {code}: {overview}")
    return code


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--profiles", required=True, help="comma-separated source profile codes, e.g. A.1,BK.9")
    ap.add_argument("--weights", default=None,
                     help="comma-separated weights matching --profiles order, e.g. 60,40 (default: equal)")
    args = ap.parse_args()

    codes = [c.strip() for c in args.profiles.split(",") if c.strip()]
    w = [float(x.strip()) for x in args.weights.split(",")] if args.weights else None
    create_hybrid_profile(codes, w)
