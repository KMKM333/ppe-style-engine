"""
transform.py — the POST-CREATION half of the rating system.

Workflow:
  1. Input X gets a pre-creation score against every profile (score_engine.rate_input).
  2. For a chosen target profile (or several), we generate a NEW version of
     Input X that keeps its CONTENT but adopts that profile's structural/
     stylistic attributes (tone, length, hook type, beat sequence, diction).
     This generation step needs an LLM — build_transform_prompt() produces
     a prompt seeded with the profile's live fingerprint (not a vague
     description), for Claude to fill in.
  3. The generated text is scored again with the SAME engine used for
     pre-creation (score_against_profiles) — but now measuring the rewrite's
     fit against ITS TARGET profile specifically. That's the post-creation
     score. score_delta = post_score - pre_score tells you whether the
     rewrite actually moved the text toward the target style, and by how
     much — the "did this transformation work" metric.

Usage:
    # 1. get a transform prompt for test_id=3 targeting profile A.1
    python3 transform.py --prompt --test_id 3 --profile A.1

    # 2. paste that to Claude, save the reply's script text into a .txt file

    # 3. save + score the generated text
    python3 transform.py --save --test_id 3 --profile A.1 \
        --title "Goodhart, The Vanishing Number" --file generated.txt
"""
import argparse
import json

from db_init import get_conn
from feature_extraction import extract_auto_features
from score_engine import score_against_profiles


TRANSFORM_PROMPT = """Rewrite the CONTENT below so that it adopts the STYLE PROFILE
described afterward. Keep the underlying subject/argument/facts from the content —
change only how it's told: length, structure, hook type, sentence rhythm, diction,
direct-address level, and closing move.

CONTENT TO REWRITE:
---
{content}
---

TARGET STYLE PROFILE ({profile_code}):
- Target word count: ~{word_count_median} words (observed range {word_count_min}-{word_count_max})
- Target beat/paragraph count: ~{beat_count_median}
- Target sentence length: ~{sent_len_median} words/sentence on average
- Direct address ("you") rate: ~{you_freq_median} per 100 words
- Readability (Flesch-Kincaid grade): ~{readability_median}
- Dominant title format: {title_format}
- Dominant hook type(s): {hook_types}
- Dominant close type(s): {close_types}

Produce:
1. A title in the profile's dominant title format
2. The rewritten script matching the profile's length/structure/diction targets above

Return exactly:
TITLE: <title>
SCRIPT:
<script>
"""


DATA_OUTPUT_PROMPT = """Rewrite the CONTENT below so it adopts the VOICE of the target
profile described, delivered in the STRUCTURAL FORM specified. Keep the underlying
subject/argument/facts from the content — change only how it's told.

CONTENT TO REWRITE:
---
{content}
---

TARGET VOICE — {profile_code} ({profile_label}):
{voice_traits}

OUTPUT FORM — {form_label}:
{form_spec}

Produce:
1. A title matching the output form's conventions
2. The rewritten script matching the form's structural targets above, in the target profile's voice

Return exactly:
TITLE: <title>
SCRIPT:
<script>
"""

# Structural targets derived from corpus-wide feature extraction across all
# ingested Instagram scripts (336) and book examples (398) — see the "form"
# a piece takes, independent of whose voice it's written in. A target profile's
# own fingerprint (_voice_traits) only ever covers attributes its media type
# was actually classified on, which is why voice and form are kept separate:
# a book-author profile has no hook_type/title_format of its own to draw on.
FORM_SPECS = {
    "insta_script": {
        "label": "Instagram script",
        "spec": (
            "- Target length: ~310 words\n"
            "- Target sentence length: ~22 words/sentence on average\n"
            "- Direct address (\"you\"): ~3 per 100 words — speak straight to the viewer\n"
            "- Include roughly 1-2 questions across the piece\n"
            "- Title format: name/source + concept (e.g. \"<Name>'s <Concept>\")\n"
            "- Hook: open with a bold/contrarian claim or a relatable scenario\n"
            "- Close: a punchy takeaway, mic-drop line, or light CTA — not a trailing-off summary\n"
            "- Voice: conversational, colloquial, built to be read aloud in ~60-90 seconds"
        ),
    },
    "book_example": {
        "label": "Book example / case study",
        "spec": (
            "- Target length: ~70 words (a single self-contained case study, not a full chapter)\n"
            "- Target sentence length: ~30 words/sentence on average\n"
            "- Third-person, narrative prose — avoid addressing the reader directly (\"you\")\n"
            "- No questions, no CTA, no direct address\n"
            "- Title format: a short descriptive label naming the example (e.g. \"<Subject>'s <Notable Detail>\")\n"
            "- Structure: set the scene, state what happened, let the point land implicitly through the narrative\n"
            "- Voice: expository, varied vocabulary, reads like a nonfiction case study excerpt"
        ),
    },
}


def _voice_traits(conn, profile_id, max_numeric=6, max_categorical=8):
    """Generic dump of whatever fingerprint attributes exist for this profile —
    works for both video and book-author profiles without hardcoding either
    attribute vocabulary, since the two media types are classified on
    completely different fields."""
    lines = []
    for r in conn.execute(
        "SELECT attribute, median_val FROM profile_fingerprint_numeric WHERE profile_id=? "
        "AND median_val IS NOT NULL ORDER BY attribute LIMIT ?",
        (profile_id, max_numeric),
    ).fetchall():
        lines.append(f"- {r['attribute']}: ~{round(r['median_val'], 2)}")
    for r in conn.execute(
        "SELECT DISTINCT attribute FROM profile_fingerprint_categorical WHERE profile_id=? "
        "ORDER BY attribute LIMIT ?",
        (profile_id, max_categorical),
    ).fetchall():
        lines.append(f"- {r['attribute']}: {_fp_top_categorical(conn, profile_id, r['attribute'], 2)}")
    if not lines:
        return "(profile not yet classified — infer voice from its subject matter)"
    return "\n".join(lines)


def build_data_output_prompt(test_id: int, profile_code: str, form: str) -> str:
    if form not in FORM_SPECS:
        raise ValueError(f"Unknown output form: {form}")

    conn = get_conn()
    test_row = conn.execute("SELECT * FROM test_inputs WHERE test_id=?", (test_id,)).fetchone()
    profile = conn.execute(
        """SELECT p.*, c.channel_name FROM style_profiles p
           JOIN channels c ON c.channel_id = p.channel_id WHERE p.profile_code=?""",
        (profile_code,),
    ).fetchone()
    if not test_row or not profile:
        conn.close()
        raise ValueError("test_id or profile_code not found")

    form_def = FORM_SPECS[form]
    prompt = DATA_OUTPUT_PROMPT.format(
        content=test_row["raw_text"],
        profile_code=profile_code,
        profile_label=profile["channel_name"],
        voice_traits=_voice_traits(conn, profile["profile_id"]),
        form_label=form_def["label"],
        form_spec=form_def["spec"],
    )
    conn.close()
    return prompt


def _fp_value(conn, profile_id, attr, field="median_val"):
    row = conn.execute(
        "SELECT * FROM profile_fingerprint_numeric WHERE profile_id=? AND attribute=?", (profile_id, attr)
    ).fetchone()
    if not row or row[field] is None:
        return None
    return round(row[field], 2)


def _fp_top_categorical(conn, profile_id, attr, top_n=2):
    rows = conn.execute(
        "SELECT value, share_pct FROM profile_fingerprint_categorical WHERE profile_id=? AND attribute=? "
        "ORDER BY share_pct DESC LIMIT ?",
        (profile_id, attr, top_n),
    ).fetchall()
    if not rows:
        return "(not yet classified)"
    return ", ".join(f"{r['value']} ({r['share_pct']}%)" for r in rows)


def build_transform_prompt(test_id: int, profile_code: str) -> str:
    conn = get_conn()
    test_row = conn.execute("SELECT * FROM test_inputs WHERE test_id=?", (test_id,)).fetchone()
    profile = conn.execute("SELECT * FROM style_profiles WHERE profile_code=?", (profile_code,)).fetchone()
    if not test_row or not profile:
        conn.close()
        raise ValueError("test_id or profile_code not found")

    pid = profile["profile_id"]
    prompt = TRANSFORM_PROMPT.format(
        content=test_row["raw_text"],
        profile_code=profile_code,
        word_count_median=_fp_value(conn, pid, "word_count"),
        word_count_min=_fp_value(conn, pid, "word_count", "min_val"),
        word_count_max=_fp_value(conn, pid, "word_count", "max_val"),
        beat_count_median=_fp_value(conn, pid, "beat_count"),
        sent_len_median=_fp_value(conn, pid, "avg_sentence_len"),
        you_freq_median=_fp_value(conn, pid, "you_freq_per_100w"),
        readability_median=_fp_value(conn, pid, "readability_score"),
        title_format=_fp_top_categorical(conn, pid, "title_format", 1),
        hook_types=_fp_top_categorical(conn, pid, "hook_type", 3),
        close_types=_fp_top_categorical(conn, pid, "close_type", 3),
    )
    conn.close()
    return prompt


def save_transformation(test_id: int, profile_code: str, generated_title: str, generated_text: str,
                         generated_by: str = "claude") -> int:
    conn = get_conn()
    profile = conn.execute("SELECT * FROM style_profiles WHERE profile_code=?", (profile_code,)).fetchone()
    if not profile:
        conn.close()
        raise ValueError(f"No such profile: {profile_code}")

    cur = conn.execute(
        "INSERT INTO transformations (test_id, target_profile_id, generated_title, generated_text, generated_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (test_id, profile["profile_id"], generated_title, generated_text, generated_by),
    )
    transformation_id = cur.lastrowid
    conn.commit()
    conn.close()
    return transformation_id


def score_transformation(transformation_id: int) -> dict:
    """POST-CREATION scoring: score the generated text against every
    confirmed profile (so you can see it moved toward the TARGET and,
    just as importantly, didn't accidentally drift toward a different
    profile), then compute the delta vs. the original Input X's
    pre-creation score against the same profiles."""
    conn = get_conn()
    tr = conn.execute(
        "SELECT * FROM transformations WHERE transformation_id=?", (transformation_id,)
    ).fetchone()
    if not tr:
        conn.close()
        raise ValueError("transformation_id not found")

    target_profile = conn.execute(
        "SELECT * FROM style_profiles WHERE profile_id=?", (tr["target_profile_id"],)
    ).fetchone()

    features = extract_auto_features(tr["generated_text"], tr["generated_title"] or "")
    post_scores = score_against_profiles(features, raw_text=tr["generated_text"])

    # pull the ORIGINAL input's pre-creation scores for delta comparison
    pre_scores = {
        r["profile_id"]: r["total_score"]
        for r in conn.execute("SELECT * FROM test_scores WHERE test_id=?", (tr["test_id"],))
    }

    results = []
    for ps in post_scores:
        pre = pre_scores.get(ps["profile_id"])
        delta = round(ps["total_score"] - pre, 1) if pre is not None else None
        is_target = ps["profile_id"] == tr["target_profile_id"]
        results.append({**ps, "is_target_profile": is_target, "pre_score_same_profile": pre, "score_delta": delta})

        conn.execute(
            "INSERT OR REPLACE INTO transform_scores (transformation_id, profile_id, total_score, rank, "
            "is_target_profile, pre_score_same_profile, score_delta, score_breakdown) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (transformation_id, ps["profile_id"], ps["total_score"], ps["rank"], int(is_target),
             pre, delta, json.dumps(ps["breakdown"])),
        )

    conn.commit()
    conn.close()
    results.sort(key=lambda r: r["total_score"], reverse=True)
    return {"transformation_id": transformation_id, "target_profile": target_profile["profile_code"],
            "results": results}


def print_transform_result(result: dict):
    print(f"\n=== Post-creation scoring: transformation #{result['transformation_id']} "
          f"(target = {result['target_profile']}) ===\n")
    for r in result["results"]:
        marker = " <-- TARGET" if r["is_target_profile"] else ""
        delta_str = f"  delta vs pre-creation: {r['score_delta']:+.1f}" if r["score_delta"] is not None else ""
        print(f"  #{r['rank']}  {r['profile_code']:6s}  post_score={r['total_score']:5.1f}{delta_str}{marker}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", action="store_true", help="print the transform prompt for a test_id + profile")
    ap.add_argument("--save", action="store_true", help="save + score a generated transformation")
    ap.add_argument("--test_id", type=int, required=True)
    ap.add_argument("--profile", required=True, help="target profile code, e.g. A.1")
    ap.add_argument("--title", default="")
    ap.add_argument("--file", default=None, help="path to .txt containing the generated script (for --save)")
    args = ap.parse_args()

    if args.prompt:
        print(build_transform_prompt(args.test_id, args.profile))
    elif args.save:
        with open(args.file) as f:
            text = f.read()
        tid = save_transformation(args.test_id, args.profile, args.title, text)
        result = score_transformation(tid)
        print_transform_result(result)
