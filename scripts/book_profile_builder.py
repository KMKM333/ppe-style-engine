"""
book_profile_builder.py — the book-analysis counterpart to profile_builder.py.
Rolls up an author's classified book(s) into a style_profile fingerprint, the
same shape used for video-creator channels, so books can be cross-referenced
against video profiles on the same subject.

An author is modeled as a "channel" (platform='Book'), one channel per author
name, mirroring how a video channel is one creator's body of work. A single
classified book is already a complete data point for that author (unlike a
single 30-second video), so min_n defaults to 1 rather than profile_builder's
10.

Usage:
    python3 book_profile_builder.py --author "Michael Sandel" --code BK.2
    python3 book_profile_builder.py --all          # (re)build a profile for every author with a classified book
"""
import argparse
import json
import statistics

from db_init import get_conn

# Controlled-vocabulary (fixed allowed-values) fields from BOOK_CLASSIFICATION_PROMPT —
# the only ones that roll up meaningfully into a categorical share_pct fingerprint.
# Free-text fields (thesis_statement, bias_assumptions, named_frameworks_coined,
# ideological_positioning, comparative_positioning, interdisciplinary_fields,
# secondary_evidence_types) are excluded, same reasoning profile_builder.py uses
# for excluding free-text video fields like named_bias_or_law_term.
CATEGORICAL_ATTRS = [
    "primary_goal", "primary_evidence_type", "tone", "structure_style",
    "subheading_density", "target_audience", "vocabulary_complexity",
    "counter_argument_engagement", "argument_architecture", "prescriptiveness",
    "temporal_orientation", "narrative_density", "claim_falsifiability",
    "rhetorical_appeal_balance", "thesis_consistency", "citation_density",
    # Style & Craft (18 new, all controlled-vocabulary like the rest above)
    "emotional_register", "narrative_voice", "polemical_tone", "narrative_presence",
    "jargon_accessibility", "argumentative_density", "abstraction_concreteness_balance",
    "hedging_vs_assertion", "rhetorical_questioning",
    "diction", "syntax_pattern", "pacing", "sensory_language_density", "narrative_distance",
    "figurative_language_density", "prose_rhythm", "noun_verb_ratio_style", "cognitive_metaphor_domain",
    # cross-media shared fields (also on the video rubric)
    "value_promise", "information_density", "relatability_factor", "contrarian_positioning",
    "adjective_intensity", "punctuation_delivery", "vulnerability_depth", "condescension_vs_empowerment",
]

BOOLEAN_ATTRS = ["uses_visual_aids", "curiosity_loop", "identity_framing", "rhythmic_repetition"]


def get_or_create_author_channel(conn, author_name):
    row = conn.execute("SELECT channel_id FROM channels WHERE channel_name = ?", (author_name,)).fetchone()
    if row:
        return row["channel_id"]
    cur = conn.execute(
        "INSERT INTO channels (channel_name, platform, typical_length_band) VALUES (?, 'Book', 'BK')",
        (author_name,),
    )
    conn.commit()
    return cur.lastrowid


def get_or_create_profile(conn, code, channel_id):
    row = conn.execute("SELECT profile_id FROM style_profiles WHERE profile_code = ?", (code,)).fetchone()
    if row:
        return row["profile_id"]
    cur = conn.execute(
        "INSERT INTO style_profiles (profile_code, channel_id, length_band, media_type) VALUES (?, ?, 'BK', 'Book')",
        (code, channel_id),
    )
    conn.commit()
    return cur.lastrowid


def build_author_profile(author_name, profile_code, min_n=1):
    conn = get_conn()
    rows = conn.execute(
        """SELECT a.*, b.subject, b.summary, b.title FROM book_attributes a
           JOIN books b ON b.book_id = a.book_id
           WHERE b.author = ? AND a.classified_by IS NOT NULL AND a.classified_by != 'pending'""",
        (author_name,),
    ).fetchall()
    n = len(rows)

    channel_id = get_or_create_author_channel(conn, author_name)
    existing = conn.execute("SELECT profile_id FROM style_profiles WHERE profile_code = ?", (profile_code,)).fetchone()

    if n == 0:
        if not existing:
            print(f"No classified books found for '{author_name}' yet.")
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
        print(f"No classified books left for '{author_name}' — cleared fingerprint for profile {profile_code}.")
        return profile_id

    profile_id = get_or_create_profile(conn, profile_code, channel_id)

    # full replace, same reasoning as profile_builder.build_profile
    conn.execute("DELETE FROM profile_fingerprint_numeric WHERE profile_id = ?", (profile_id,))
    conn.execute("DELETE FROM profile_fingerprint_categorical WHERE profile_id = ?", (profile_id,))

    # boolean attrs roll up as a numeric mean (share true) in the same table shape
    # profile_fingerprint_numeric already uses for video attributes.
    for attr in BOOLEAN_ATTRS:
        vals = [r[attr] for r in rows if r[attr] is not None]
        if not vals:
            continue
        mean_v = statistics.mean(vals)
        conn.execute(
            """INSERT INTO profile_fingerprint_numeric
               (profile_id, attribute, mean_val, std_val, min_val, max_val, median_val, values_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(profile_id, attribute) DO UPDATE SET
                 mean_val=excluded.mean_val, std_val=excluded.std_val,
                 min_val=excluded.min_val, max_val=excluded.max_val,
                 median_val=excluded.median_val, values_json=excluded.values_json""",
            (profile_id, attr, mean_v, statistics.pstdev(vals) if len(vals) > 1 else 0.0,
             min(vals), max(vals), statistics.median(vals), json.dumps(sorted(vals))),
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

    subject = rows[0]["subject"]
    titles = ", ".join(sorted({r["title"] for r in rows}))
    overview = f"{titles}."

    status = "confirmed" if n >= min_n else "draft"
    conn.execute(
        """UPDATE style_profiles SET n_videos_analysed = ?, status = ?, subject = COALESCE(subject, ?),
           overview = ?, updated_at = datetime('now') WHERE profile_id = ?""",
        (n, status, subject, overview, profile_id),
    )
    conn.commit()
    conn.close()
    print(f"Built fingerprint for profile {profile_code} from {n} book(s) by {author_name} (status={status}).")
    return profile_id


def build_all(min_n=1):
    conn = get_conn()
    authors = [
        r["author"] for r in conn.execute(
            """SELECT DISTINCT b.author FROM books b JOIN book_attributes a ON a.book_id = b.book_id
               WHERE b.author IS NOT NULL AND a.classified_by IS NOT NULL AND a.classified_by != 'pending'
               ORDER BY b.ingested_at"""
        ).fetchall()
    ]
    existing_codes = {
        r["profile_code"] for r in conn.execute(
            """SELECT p.profile_code FROM style_profiles p WHERE p.media_type = 'Book'"""
        ).fetchall()
    }
    # figure out the next free BK.N suffix so re-runs don't collide
    used_n = [int(c.split(".")[1]) for c in existing_codes if c.startswith("BK.") and c.split(".")[1].isdigit()]
    next_n = max(used_n, default=0) + 1
    conn.close()

    for author in authors:
        # reuse an existing code for this author's channel if one's already assigned
        conn2 = get_conn()
        ch = conn2.execute("SELECT channel_id FROM channels WHERE channel_name = ?", (author,)).fetchone()
        code = None
        if ch:
            p = conn2.execute(
                "SELECT profile_code FROM style_profiles WHERE channel_id = ? AND media_type = 'Book'",
                (ch["channel_id"],),
            ).fetchone()
            if p:
                code = p["profile_code"]
        conn2.close()
        if not code:
            code = f"BK.{next_n}"
            next_n += 1
        build_author_profile(author, code, min_n=min_n)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--author")
    ap.add_argument("--code", help="profile code, e.g. BK.1")
    ap.add_argument("--min_n", type=int, default=1)
    ap.add_argument("--all", action="store_true", help="build a profile for every author with a classified book")
    args = ap.parse_args()

    if args.all:
        build_all(min_n=args.min_n)
    else:
        if not args.author or not args.code:
            ap.error("--author and --code are required unless --all is given")
        build_author_profile(args.author, args.code, args.min_n)
