"""
score_engine.py — the rating engine.

Two scoring layers, matching your two bullet points:

1. score_intrinsic(): a rule-based rating of the input ON ITS OWN MERITS
   against generic "good short-form PPE explainer" heuristics (readability
   band, hook presence, has a payoff, isn't a wall of jargon, etc.) —
   independent of any specific channel. Returns 0-100.

2. score_against_profiles(): correlates the input's attributes against every
   confirmed style_profile fingerprint and returns a ranked list of best-fit
   profiles with a per-attribute score breakdown — "ease of use / already
   fits the puzzle" scoring.

Both accept raw text (video script, book paragraph, news article, etc.).
Auto attributes are computed directly; Class attributes are optional — if
you don't have a Claude classification pass for the test input, the engine
just scores on the auto (numeric) attributes it has, and flags which
categorical attributes were skipped.
"""
import argparse
import json
import sqlite3

from db_init import get_conn
from feature_extraction import extract_auto_features
from similarity import find_best_match
from book_profile_builder import BOOLEAN_ATTRS as BOOK_BOOLEAN_ATTRS, CATEGORICAL_ATTRS as BOOK_CATEGORICAL_ATTRS

NUMERIC_ATTRS = [
    "word_count", "beat_count", "avg_sentence_len", "median_sentence_len",
    "sentence_len_variance", "you_freq_per_100w", "i_freq_per_100w", "we_freq_per_100w",
    "question_count", "emdash_count", "quote_count", "jargon_density",
    "readability_score", "filler_count", "word_economy_ratio", "title_word_count",
    "number_count", "instruction_verb_count", "framework_marker_count",
    "sentence_rhythm_cv", "closing_paragraph_ratio", "lexical_diversity",
    "punctuation_density", "colloquialism_density", "contrast_structure_count",
    "named_entity_count", "humor_marker_count", "cta_count",
]

CATEGORICAL_ATTRS_OPTIONAL = [
    "title_format", "hook_type", "close_type", "citation_style",
    "certainty_register", "domain", "concept_type", "source_era",
    "framing", "script_polish", "cta_type", "cta_placement",
    "explanation_mechanism", "rhetorical_mode",
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
]

# The fields scored the SAME WAY on a video and a book — same field name,
# same controlled vocabulary (or same numeric meaning), so a value on one
# side can be looked up directly in the other side's fingerprint. This is
# the field list score_against_profiles() uses for its cross-media branch
# (video creation vs. a Book profile) and webapp._cross_media_shared_fields()
# reuses it for the "Shared with books" pie so the two stay in sync by
# construction rather than by two hand-maintained lists silently drifting.
CROSS_MEDIA_SHARED_FIELDS = [
    "syntax_pattern", "adjective_intensity", "punctuation_delivery",
    "rhetorical_appeal_balance", "rhythmic_repetition",
    "information_density",
    "value_promise",
    "tone", "emotional_register", "narrative_voice", "polemical_tone",
    "narrative_presence", "vulnerability_depth", "condescension_vs_empowerment",
    "curiosity_loop",
    "relatability_factor", "identity_framing",
    "counter_argument_engagement", "contrarian_positioning",
    "narrative_density",
    "prose_rhythm", "noun_verb_ratio_style", "pacing",
    "avg_sentence_len", "readability_score",
]
CROSS_MEDIA_SHARED_NUMERIC = {"avg_sentence_len", "readability_score"}


# ------------------------------------------------------------------
# 1. INTRINSIC RULE-BASED RATING (no profile needed)
# ------------------------------------------------------------------

def score_intrinsic(features: dict) -> dict:
    """Generic rubric for 'is this a well-built short-form PPE explainer',
    independent of matching any one creator. Each rule is worth points;
    total is normalised to 0-100. Fully transparent / auditable."""
    checks = []

    def add(name, passed, points, detail=""):
        checks.append({"rule": name, "passed": bool(passed), "points": points if passed else 0, "max": points, "detail": detail})

    wc = features.get("word_count", 0)
    add("Length in short-form spoken-script band (150-350 words)", 150 <= wc <= 350, 15,
        f"word_count={wc}")

    rd = features.get("readability_score", 0)
    add("Readability in accessible-but-substantive band (grade 6-11)", 6 <= rd <= 11, 15,
        f"flesch_kincaid_grade={rd}")

    you = features.get("you_freq_per_100w", 0)
    add("Uses direct address ('you') at a meaningful rate (>=1.5/100w)", you >= 1.5, 15,
        f"you_freq_per_100w={you}")

    q = features.get("question_count", 0)
    add("Contains at least one question (engagement/hook mechanic)", q >= 1, 10,
        f"question_count={q}")

    jd = features.get("jargon_density", 0)
    add("Jargon density not overwhelming (<=3% of words)", jd <= 0.03, 15,
        f"jargon_density={jd}")

    quotes = features.get("quote_count", 0)
    add("Contains at least one quoted/anchoring line", quotes >= 1, 10,
        f"quote_count={quotes}")

    beats = features.get("beat_count", 0)
    add("Has multi-beat structure (3+ paragraphs — hook/body/close, not one block)", beats >= 3, 10,
        f"beat_count={beats}")

    add("Ends on a question (reflection/comment-bait close)", features.get("ends_on_question", 0) == 1, 10,
        f"ends_on_question={features.get('ends_on_question')}")

    total_points = sum(c["max"] for c in checks)
    earned = sum(c["points"] for c in checks)
    score = round(earned / total_points * 100, 1) if total_points else 0.0

    return {"total_score": score, "checks": checks}


# ------------------------------------------------------------------
# 2. PROFILE-CORRELATION RATING (rule-based similarity to each fingerprint)
# ------------------------------------------------------------------

def _numeric_subscore(value, fp_row):
    """Score 0-100 for how well `value` fits the profile's OBSERVED
    distribution for this attribute (not an assumed normal curve).

    Uses the raw sorted values captured at profile-build time
    (fp_row['values_json']): anything within the observed [min, max] range
    scores 60-100 depending on distance from the median; anything outside
    the observed range decays further the more it overshoots. This matters
    because real per-video variance (e.g. quote_count ranging 0-13 across
    genuine philosophyminis videos) shouldn't be penalised as if it were
    noise around a single "correct" value — the whole observed range IS
    the style.
    """
    if value is None or fp_row is None:
        return None

    mean_v, std_v = fp_row["mean_val"], fp_row["std_val"]
    min_v, max_v, median_v = fp_row["min_val"], fp_row["max_val"], fp_row["median_val"]
    values_json = fp_row["values_json"] if "values_json" in fp_row.keys() else None

    if values_json:
        vals = json.loads(values_json)
        rng = max_v - min_v
        half_range = max(rng / 2, 1e-6)
        if min_v <= value <= max_v:
            # within the profile's own observed range -> 60-100 band,
            # 100 at the median, tapering to 60 at either extreme actually seen
            dist_from_median = abs(value - median_v)
            score = 100 - 40 * (dist_from_median / half_range)
            return round(max(60.0, score), 1)
        else:
            # outside the observed range -> decays from 60 downward based on
            # how many "range-widths" past the edge it lands
            overshoot = (min_v - value) if value < min_v else (value - max_v)
            overshoot_ratio = overshoot / max(rng, 1e-6)
            score = max(0.0, 60 - overshoot_ratio * 40)
            return round(score, 1)

    # fallback if no raw values stored (shouldn't happen once profile_builder
    # has been re-run) -> old z-score decay
    if std_v in (None, 0):
        std_v = max(abs(mean_v) * 0.1, 1e-6)
    z = abs(value - mean_v) / std_v
    return round(max(0.0, 100 - (z ** 1.5) * 30), 1)


def _categorical_subscore(value, profile_id, attribute, conn):
    """Score 0-100 based on the % share that value holds in the profile's
    fingerprint for that attribute. Exact match to the dominant value =
    ~100; a value the profile rarely/never uses scores low."""
    if value is None:
        return None
    row = conn.execute(
        "SELECT share_pct FROM profile_fingerprint_categorical WHERE profile_id=? AND attribute=? AND value=?",
        (profile_id, attribute, value),
    ).fetchone()
    share = row["share_pct"] if row else 0.0
    # scale share (0-100%) into a score, with a floor so "never seen" isn't
    # literally zero-out-of-hand (some allowance for genuine novelty)
    return round(20 + share * 0.8, 1)


def score_against_profiles(features: dict, weights: dict | None = None, raw_text: str | None = None,
                            exclude_video_id: int | None = None):
    """PRE-CREATION scoring: for each confirmed profile, check content
    membership first (is raw_text literally/near-literally one of that
    profile's training videos?), and only fall back to attribute
    correlation when it isn't. This is what makes a genuine philosophyminis
    script score ~100% against A.1: it doesn't need to "resemble" the
    fingerprint statistically, it IS a data point the fingerprint was built
    from (or an exact re-submission of one).

    `exclude_video_id`: pass this when re-scoring a video that's already IN
    the DB (e.g. testing the pipeline on a training video itself) so it
    doesn't just match against its own row trivially in a misleading way —
    in that case membership is still correctly detected, just via other
    near-identical rows if any exist, or genuinely falls through to
    correlation scoring against the rest of the corpus.
    """
    conn = get_conn()
    profiles = conn.execute("SELECT * FROM style_profiles WHERE status = 'confirmed'").fetchall()
    if not profiles:
        profiles = conn.execute("SELECT * FROM style_profiles").fetchall()

    if weights is None:
        weights = {r["attribute"]: r["weight"] for r in conn.execute("SELECT * FROM scoring_weights")}

    results = []
    for p in profiles:
        pid = p["profile_id"]
        breakdown = {}

        # --- membership check (content axis) ---
        is_member, match_video_id, match_sim, match_type = False, None, 0.0, None
        if raw_text is not None:
            candidates = conn.execute(
                """SELECT v.video_id, v.script, v.content_hash FROM videos v
                   WHERE v.channel_id = ? AND v.video_id != COALESCE(?, -1)""",
                (p["channel_id"], exclude_video_id),
            ).fetchall()
            if candidates:
                match_video_id, match_sim, match_type = find_best_match(raw_text, candidates)
                is_member = match_type is not None

        if is_member:
            # content match found -> this profile's score reflects TRUE
            # membership, not a statistical estimate. Exact match = 100.
            # Near-duplicate (minor transcription/edit differences) scales
            # down slightly from 100 but stays clearly distinct from a
            # merely-similar-style script.
            total = 100.0 if match_type == "exact" else round(85 + match_sim * 15, 1)
            breakdown = {"content_match": match_type, "similarity": round(match_sim, 4)}
            results.append({
                "profile_code": p["profile_code"], "profile_id": pid,
                "n_videos_analysed": p["n_videos_analysed"], "status": p["status"],
                "total_score": total, "breakdown": breakdown,
                "is_corpus_member": True, "match_video_id": match_video_id, "match_similarity": match_sim,
            })
            continue

        # --- book profiles: score only on the cross-media SHARED fields ---
        # Most of NUMERIC_ATTRS/CATEGORICAL_ATTRS_OPTIONAL are video-shaped
        # (word_count, hook_type, cta_type...) and genuinely have no book
        # counterpart. But CROSS_MEDIA_SHARED_FIELDS (tone, pacing,
        # rhetorical_appeal_balance, avg_sentence_len...) are scored with
        # the exact same field name and controlled vocabulary on both sides,
        # and a book profile's fingerprint now has entries for them (see
        # book_profile_builder.CATEGORICAL_ATTRS/NUMERIC_ATTRS) — so those
        # can be looked up directly via the same media-agnostic
        # _numeric_subscore/_categorical_subscore helpers used below.
        # total_score stays None (breakdown={"cross_media": ...}) only when
        # this specific input has none of the shared fields populated (e.g.
        # it was never run through classification), which still signals
        # "not mechanically comparable" honestly rather than faking a score.
        if p["media_type"] == "Book":
            cm_weighted_sum, cm_weight_total = 0.0, 0.0
            cm_breakdown = {}
            for attr in CROSS_MEDIA_SHARED_FIELDS:
                if attr not in features or features[attr] is None:
                    continue
                if attr in CROSS_MEDIA_SHARED_NUMERIC:
                    fp = conn.execute(
                        "SELECT * FROM profile_fingerprint_numeric WHERE profile_id=? AND attribute=?", (pid, attr)
                    ).fetchone()
                    sub = _numeric_subscore(features[attr], fp) if fp else None
                else:
                    sub = _categorical_subscore(features[attr], pid, attr, conn)
                if sub is None:
                    continue
                w = weights.get(attr, 1.0)
                cm_breakdown[attr] = sub
                cm_weighted_sum += sub * w
                cm_weight_total += w

            if cm_weight_total:
                total = round(cm_weighted_sum / cm_weight_total, 1)
                results.append({
                    "profile_code": p["profile_code"], "profile_id": pid,
                    "n_videos_analysed": p["n_videos_analysed"], "status": p["status"],
                    "total_score": total, "breakdown": cm_breakdown,
                    "is_corpus_member": False, "match_video_id": match_video_id, "match_similarity": match_sim,
                })
            else:
                results.append({
                    "profile_code": p["profile_code"], "profile_id": pid,
                    "n_videos_analysed": p["n_videos_analysed"], "status": p["status"],
                    "total_score": None,
                    "breakdown": {"cross_media": "This input has none of the cross-media shared fields "
                                                  "populated yet (needs a Class-attribute classification pass)"},
                    "is_corpus_member": False, "match_video_id": match_video_id, "match_similarity": match_sim,
                })
            continue

        # --- attribute correlation (structural/stylistic axis) ---
        weighted_sum, weight_total = 0.0, 0.0

        for attr in NUMERIC_ATTRS:
            fp = conn.execute(
                "SELECT * FROM profile_fingerprint_numeric WHERE profile_id=? AND attribute=?", (pid, attr)
            ).fetchone()
            if not fp or attr not in features:
                continue
            sub = _numeric_subscore(features[attr], fp)
            if sub is None:
                continue
            w = weights.get(attr, 1.0)
            breakdown[attr] = sub
            weighted_sum += sub * w
            weight_total += w

        for attr in CATEGORICAL_ATTRS_OPTIONAL:
            if attr not in features or features[attr] is None:
                continue
            sub = _categorical_subscore(features[attr], pid, attr, conn)
            if sub is None:
                continue
            w = weights.get(attr, 1.0)
            breakdown[attr] = sub
            weighted_sum += sub * w
            weight_total += w

        total = round(weighted_sum / weight_total, 1) if weight_total else 0.0
        results.append({
            "profile_code": p["profile_code"], "profile_id": pid,
            "n_videos_analysed": p["n_videos_analysed"], "status": p["status"],
            "total_score": total, "breakdown": breakdown,
            "is_corpus_member": False, "match_video_id": match_video_id, "match_similarity": match_sim,
        })

    conn.close()
    # None (book profiles, see above) sorts last; real scores descending
    results.sort(key=lambda r: (r["total_score"] is not None, r["total_score"] or 0), reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i
    return results


def score_book_against_profiles(book_attrs: dict, weights: dict | None = None):
    """Book-side counterpart to score_against_profiles(): correlates a
    classified book's own attribute values against every confirmed BOOK
    style_profile fingerprint.

    Reuses _numeric_subscore()/_categorical_subscore() as-is — both are
    already media-agnostic (they just look up a profile_id/attribute/value
    in profile_fingerprint_numeric/categorical, whatever the field name
    means), so the only thing that changes here versus the video version is
    which field list and which profile subset gets looped: book_profile_builder's
    BOOLEAN_ATTRS/CATEGORICAL_ATTRS (the actual book rubric field names)
    against style_profiles WHERE media_type='Book'. No content-membership
    check here (unlike videos) — books have no raw-text corpus-matching
    step, so this is pure attribute correlation throughout; a book scoring
    near-100 against its own author's profile is expected (the profile was
    built from that book), not a bug.
    """
    conn = get_conn()
    profiles = conn.execute(
        "SELECT * FROM style_profiles WHERE status = 'confirmed' AND media_type = 'Book'"
    ).fetchall()
    if not profiles:
        profiles = conn.execute("SELECT * FROM style_profiles WHERE media_type = 'Book'").fetchall()

    if weights is None:
        weights = {}  # no dedicated book weighting scheme yet -> uniform (1.0 each)

    results = []
    for p in profiles:
        pid = p["profile_id"]
        breakdown = {}
        weighted_sum, weight_total = 0.0, 0.0

        for attr in BOOK_BOOLEAN_ATTRS:
            fp = conn.execute(
                "SELECT * FROM profile_fingerprint_numeric WHERE profile_id=? AND attribute=?", (pid, attr)
            ).fetchone()
            if not fp or attr not in book_attrs or book_attrs[attr] is None:
                continue
            sub = _numeric_subscore(book_attrs[attr], fp)
            if sub is None:
                continue
            w = weights.get(attr, 1.0)
            breakdown[attr] = sub
            weighted_sum += sub * w
            weight_total += w

        for attr in BOOK_CATEGORICAL_ATTRS:
            if attr not in book_attrs or book_attrs[attr] is None:
                continue
            sub = _categorical_subscore(book_attrs[attr], pid, attr, conn)
            if sub is None:
                continue
            w = weights.get(attr, 1.0)
            breakdown[attr] = sub
            weighted_sum += sub * w
            weight_total += w

        total = round(weighted_sum / weight_total, 1) if weight_total else 0.0
        results.append({
            "profile_code": p["profile_code"], "profile_id": pid,
            "n_videos_analysed": p["n_videos_analysed"], "status": p["status"],
            "total_score": total, "breakdown": breakdown,
        })

    conn.close()
    results.sort(key=lambda r: r["total_score"], reverse=True)
    for i, r in enumerate(results, 1):
        r["rank"] = i
    return results


# ------------------------------------------------------------------
# Orchestration: run both scoring layers on a raw text input, store it
# ------------------------------------------------------------------

def rate_input(raw_text: str, title: str = "", source_label: str = "", input_type: str = "video_script",
                class_features: dict | None = None, save: bool = True, exclude_video_id: int | None = None):
    """PRE-CREATION rating: score a raw input (video script / book paragraph /
    news article / anything) against every confirmed style profile, checking
    content membership first and falling back to attribute correlation.
    """
    features = extract_auto_features(raw_text, title)
    if class_features:
        features.update(class_features)

    intrinsic = score_intrinsic(features)
    profile_scores = score_against_profiles(features, raw_text=raw_text, exclude_video_id=exclude_video_id)

    result = {
        "source_label": source_label,
        "input_type": input_type,
        "features": features,
        "intrinsic_rating": intrinsic,
        "profile_scores": profile_scores,
        "best_fit_profile": profile_scores[0]["profile_code"] if profile_scores else None,
    }

    if save:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO test_inputs (source_label, input_type, raw_text) VALUES (?, ?, ?)",
            (source_label, input_type, raw_text),
        )
        test_id = cur.lastrowid
        for ps in profile_scores:
            conn.execute(
                "INSERT INTO test_scores (test_id, profile_id, total_score, rank, is_corpus_member, "
                "match_video_id, match_similarity, score_breakdown) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (test_id, ps["profile_id"], ps["total_score"], ps["rank"],
                 int(ps.get("is_corpus_member", False)), ps.get("match_video_id"),
                 ps.get("match_similarity"), json.dumps(ps["breakdown"])),
            )
        conn.commit()
        conn.close()
        result["test_id"] = test_id

    return result


def print_rating(result: dict):
    print(f"\n=== Rating: {result.get('source_label', '(untitled)')} ===")
    print(f"\nIntrinsic rule-based score: {result['intrinsic_rating']['total_score']}/100")
    for c in result["intrinsic_rating"]["checks"]:
        mark = "PASS" if c["passed"] else "FAIL"
        print(f"  [{mark}] {c['rule']:65s} ({c['points']}/{c['max']}) — {c['detail']}")

    print("\nStyle-profile fit (PRE-CREATION, ranked):")
    for ps in result["profile_scores"]:
        tag = "CONTENT MATCH" if ps.get("is_corpus_member") else "attribute correlation"
        score_str = f"{ps['total_score']:5.1f}" if ps["total_score"] is not None else "  n/a"
        print(f"  #{ps['rank']}  {ps['profile_code']:6s}  score={score_str}  [{tag}]  "
              f"(n_analysed={ps['n_videos_analysed']}, status={ps['status']})")
    if result["profile_scores"]:
        top = result["profile_scores"][0]
        print(f"\n  Best fit: {top['profile_code']} — breakdown:")
        for attr, sub in top["breakdown"].items():
            print(f"      {attr:25s} {sub}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="path to a .txt file with the raw script/paragraph/article")
    ap.add_argument("--title", default="")
    ap.add_argument("--label", default="")
    ap.add_argument("--type", default="video_script")
    args = ap.parse_args()

    with open(args.file) as f:
        text = f.read()

    result = rate_input(text, title=args.title, source_label=args.label, input_type=args.type)
    print_rating(result)
