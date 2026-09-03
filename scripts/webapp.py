"""
webapp.py — a small local browser interface for the PPE style engine, so you
can drive score_engine.py / profile_builder.py / transform.py from a page
instead of the CLI.

Usage:
    python3 webapp.py
    (then open http://127.0.0.1:5050 )
"""
import base64
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from urllib.parse import quote

from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, abort, jsonify

from db_init import (get_conn, BOOK_PAGES_DIR, BOOK_FILES_DIR, VIDEO_VISUALS_DIR,
                     PRODUCTION_SPEC_SHOTS_DIR, FORMAT_FRAMES_DIR)
from feature_extraction import extract_auto_features, _sentences
from profile_builder import build_profile
from score_engine import rate_input, score_intrinsic, score_against_profiles, score_book_against_profiles, CROSS_MEDIA_SHARED_FIELDS
from transform import build_transform_prompt, build_data_output_prompt, save_transformation, score_transformation, FORM_SPECS, SWIPE_FORM_CHOICES
from hybrid_profile_builder import create_hybrid_profile
from llm_client import generate_transform, generate_json, LLMConfigError
from ingest_book import ingest_book_text
from ingest_video import ingest_video_row
import gatekeeper
from ingest_production_spec import ingest_production_spec_row
from similarity import normalize_hash
from production_spec_profile_builder import build_profile as production_spec_build_profile

# Shared secret the Instagram Bulk Transcriber (a separate local app) sends
# as the X-Ingest-Key header when POSTing to /api/ingest/book, so a freshly
# imported book lands directly on the live site instead of needing a manual
# local-DB export/import round trip. Set in the Render dashboard (see
# render.yaml) and in the transcriber's own environment — never committed.
INGEST_API_KEY = os.environ.get("INGEST_API_KEY")

SUBJECTS = ["Economics", "Politics", "Philosophy", "Psychology", "Sustainability", "Science", "Technology"]
SUBJECT_ICONS = {
    "Economics": "📈", "Politics": "🏛️", "Philosophy": "🧠", "Psychology": "🧩",
    "Sustainability": "🌱", "Science": "🔬", "Technology": "💻", "Unassigned": "🏷️",
}

# Sentinel channel name the Instagram import tool falls back to when the
# "channel" field is left blank on transcription. It must never be treated
# as a real channel — see _unresolved_import_batch() / the channel-fix modal.
PLACEHOLDER_CHANNEL_NAME = "Instagram Import"

_STOPWORDS = set("""
    the a an and or but if of to in on for with is are was were be been being
    this that these those it its as at by from you your i we our they their
    he she his her not no yes do does did can could should would will just
    so than then there here what which who when where why how also into
    out up down about over under again more most some such only own same
    too very s t don now
""".split())


def _word_counter(text):
    words = re.findall(r"[a-z']+", (text or "").lower())
    return Counter(w for w in words if w not in _STOPWORDS and len(w) > 2)


def _cosine_sim(c1, c2):
    if not c1 or not c2:
        return 0.0
    common = set(c1) & set(c2)
    dot = sum(c1[w] * c2[w] for w in common)
    n1 = math.sqrt(sum(v * v for v in c1.values()))
    n2 = math.sqrt(sum(v * v for v in c2.values()))
    return dot / (n1 * n2) if n1 and n2 else 0.0


def _suggest_channel_for_scripts(scripts, conn):
    """Best-guess channel for a batch of orphaned scripts, by comparing their
    combined vocabulary against every real channel's existing corpus. This is
    what runs before we ever bother the user — only surfaced as a starting
    guess, never applied automatically."""
    target = Counter()
    for s in scripts:
        target.update(_word_counter(s))
    channels = conn.execute(
        "SELECT channel_id, channel_name FROM channels WHERE channel_name != ?", (PLACEHOLDER_CHANNEL_NAME,)
    ).fetchall()
    best_name, best_score = None, 0.0
    for ch in channels:
        rows = conn.execute("SELECT script FROM videos WHERE channel_id = ?", (ch["channel_id"],)).fetchall()
        corpus = Counter()
        for r in rows:
            corpus.update(_word_counter(r["script"]))
        sim = _cosine_sim(target, corpus)
        if sim > best_score:
            best_name, best_score = ch["channel_name"], sim
    if best_name is None:
        return None
    return {"channel_name": best_name, "confidence": round(best_score * 100)}


def _unresolved_import_batch():
    """Videos currently stuck under the placeholder channel, plus a best-guess
    suggestion for what they should actually be tagged as."""
    conn = get_conn()
    videos = conn.execute(
        "SELECT v.video_id, v.script FROM videos v JOIN channels c ON c.channel_id = v.channel_id WHERE c.channel_name = ?",
        (PLACEHOLDER_CHANNEL_NAME,),
    ).fetchall()
    suggestion = _suggest_channel_for_scripts([v["script"] for v in videos], conn) if videos else None
    conn.close()
    return {"count": len(videos), "suggestion": suggestion}


app = Flask(__name__)
app.secret_key = "ppe-engine-demo"


@app.context_processor
def inject_unresolved_imports():
    info = _unresolved_import_batch()
    return {"unresolved_count": info["count"], "unresolved_suggestion": info["suggestion"]}


@app.context_processor
def inject_nav_counts():
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM transformations").fetchone()[0]
    conn.close()
    return {"nav_creations_count": count}

SUBTITLE_EXTS = {"srt", "vtt"}


def _parse_subtitle_to_text(raw: str) -> str:
    """Strip cue numbers/timestamps/inline tags from an .srt/.vtt file,
    keeping one paragraph per subtitle block so beat/structure scoring
    still has something to work with."""
    blocks = re.split(r"\n\s*\n", raw.strip())
    paragraphs = []
    for block in blocks:
        lines = []
        for line in block.splitlines():
            line = line.strip()
            if not line or line.upper() == "WEBVTT" or "-->" in line or re.match(r"^\d+$", line):
                continue
            line = re.sub(r"<[^>]+>", "", line)
            line = re.sub(r"\{[^}]*\}", "", line)
            if line:
                lines.append(line)
        if lines:
            paragraphs.append(" ".join(lines))
    return "\n\n".join(paragraphs)


def _read_uploaded_file(file_storage):
    filename = file_storage.filename or ""
    raw = file_storage.stream.read().decode("utf-8", errors="ignore")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in SUBTITLE_EXTS:
        return _parse_subtitle_to_text(raw), filename
    return raw, filename


def _infer_platform(url, default="YouTube"):
    """Best-effort platform from a source URL. media_type decides which
    classification pipeline a video goes through (short-form vs the much
    larger long-form combined call), so getting it from the URL beats
    blindly defaulting — an unlabelled Instagram reel defaulting to
    "YouTube" is both wrong and more expensive to classify."""
    u = (url or "").lower()
    if "instagram.com" in u:
        return "Instagram"
    if "youtube.com" in u or "youtu.be" in u:
        return "YouTube"
    if "facebook.com" in u or "fb.watch" in u:
        return "Facebook"
    if "tiktok.com" in u:
        return "TikTok"
    return default


CHANNEL_MATCH_THRESHOLD = 0.82  # difflib ratio above which two channel names are "probably the same creator"


def _resolve_import_channel(name):
    """Classifies an incoming import's channel name against what already
    exists, so a new import can be held for confirmation instead of silently
    creating a near-duplicate channel.

    Returns (status, suggestion):
      ("exact", None)        -> this channel already exists; proceed normally
      ("similar", "<name>")  -> no exact match, but an existing channel looks
                                like the same creator (a typo/case/spacing
                                variant); suggest it
      ("new", None)          -> matches nothing; genuinely new creator

    Comparison is case- and separator-insensitive first (so "Kylascan",
    "kylascan" and "kyla scan" collapse together), then falls back to a
    fuzzy ratio for looser typos. Deliberately cheap and local — no LLM
    call, same spirit as gatekeeper's other pre-checks."""
    import difflib

    def norm(s):
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    target = norm(name)
    conn = get_conn()
    existing = [r["channel_name"] for r in conn.execute("SELECT channel_name FROM channels")]
    conn.close()

    for other in existing:
        if other == name:
            return "exact", None
    # normalised equality catches case/spacing/punctuation variants
    for other in existing:
        if norm(other) == target:
            return "similar", other

    best, best_ratio = None, 0.0
    for other in existing:
        ratio = difflib.SequenceMatcher(None, target, norm(other)).ratio()
        if ratio > best_ratio:
            best, best_ratio = other, ratio
    if best and best_ratio >= CHANNEL_MATCH_THRESHOLD:
        return "similar", best
    return "new", None


def _profiles():
    conn = get_conn()
    rows = conn.execute(
        """SELECT p.*, c.channel_name FROM style_profiles p
           JOIN channels c ON c.channel_id = p.channel_id
           ORDER BY p.profile_code"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.route("/usage")
def usage_summary():
    """Reads db/api_usage_log.jsonl (appended by gatekeeper.record_usage()
    on every real llm_client.py call) and breaks it down by call type, to
    answer "is the bill driven by input or output tokens" by looking at
    real numbers instead of guessing.

    call_site alone can't tell a 32k-budget long-form video classification
    apart from a small swipe-pitch generate_json() call — both log as
    "generate_json". So entries are also bucketed by output-token
    magnitude: only the long-form combined classification call
    (COMBINED_MAX_TOKENS=32000 in classify_video_combined.py) can
    realistically produce more than a few thousand output tokens, so a
    high-output-token generate_json() call is that classification call in
    practice, not any of the small ones."""
    LARGE_OUTPUT_THRESHOLD = 5000
    rows = []
    if gatekeeper.USAGE_LOG_PATH.exists():
        with open(gatekeeper.USAGE_LOG_PATH) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    def row_input_output_cost(r):
        """Per-row input/output cost split, respecting the batch discount
        (is_batch, added alongside generate_json_batch) — so a discounted
        row's split still sums to its own stored estimated_cost_usd instead
        of silently overstating it by 2x."""
        discount = gatekeeper.BATCH_DISCOUNT if r.get("is_batch") else 1.0
        input_cost = (r.get("input_tokens", 0) / 1_000_000) * gatekeeper.PRICE_PER_MTOK_INPUT * discount
        output_cost = (r.get("output_tokens", 0) / 1_000_000) * gatekeeper.PRICE_PER_MTOK_OUTPUT * discount
        return input_cost, output_cost

    buckets = {}
    for r in rows:
        site = r.get("call_site", "?")
        bucket_name = f"{site} — large output (likely long-form video classification)" \
            if r.get("output_tokens", 0) > LARGE_OUTPUT_THRESHOLD else site
        b = buckets.setdefault(bucket_name, {"n": 0, "input_tokens": 0, "output_tokens": 0, "cost": 0.0, "output_cost": 0.0})
        b["n"] += 1
        b["input_tokens"] += r.get("input_tokens", 0)
        b["output_tokens"] += r.get("output_tokens", 0)
        b["cost"] += r.get("estimated_cost_usd", 0.0)
        b["output_cost"] += row_input_output_cost(r)[1]

    summary = []
    for name, b in sorted(buckets.items(), key=lambda kv: -kv[1]["cost"]):
        summary.append({
            "call_site": name, "n": b["n"],
            "avg_input": round(b["input_tokens"] / b["n"]) if b["n"] else 0,
            "avg_output": round(b["output_tokens"] / b["n"]) if b["n"] else 0,
            "total_cost": round(b["cost"], 4),
            "output_pct_of_cost": round(b["output_cost"] / b["cost"] * 100, 1) if b["cost"] else 0,
        })

    total_cost = sum(r.get("estimated_cost_usd", 0.0) for r in rows)
    total_input_tokens = sum(r.get("input_tokens", 0) for r in rows)
    total_output_tokens = sum(r.get("output_tokens", 0) for r in rows)
    total_input_cost = sum(row_input_output_cost(r)[0] for r in rows)
    total_output_cost = sum(row_input_output_cost(r)[1] for r in rows)
    n_batch_calls = sum(1 for r in rows if r.get("is_batch"))

    return render_template(
        "usage_summary.html", active="usage", summary=summary, n_rows=len(rows),
        n_batch_calls=n_batch_calls,
        total_cost=round(total_cost, 2), total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_input_cost=round(total_input_cost, 2), total_output_cost=round(total_output_cost, 2),
        output_pct_of_total=round(total_output_cost / total_cost * 100, 1) if total_cost else 0,
    )


@app.route("/")
def dashboard():
    conn = get_conn()
    channels = conn.execute("SELECT * FROM channels").fetchall()
    n_inputs = (
        conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        + conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        + conn.execute("SELECT COUNT(*) FROM book_examples").fetchone()[0]
    )
    n_tests = conn.execute("SELECT COUNT(*) FROM test_inputs").fetchone()[0]
    n_creations = conn.execute("SELECT COUNT(*) FROM transformations").fetchone()[0]
    profiles = _profiles()
    n_subjects_covered = len({p["subject"] for p in profiles if p["subject"] in SUBJECTS})
    implemented_cards = [c for c in _attribute_cards(conn) if c["implemented"]]
    n_attribute_fields = sum(c["field_count"] for c in implemented_cards)
    n_media_types = len(implemented_cards)
    conn.close()
    return render_template(
        "dashboard.html", active="dashboard",
        channels=channels, profiles=profiles, n_inputs=n_inputs, n_tests=n_tests, n_creations=n_creations,
        n_subjects_covered=n_subjects_covered, n_attribute_fields=n_attribute_fields, n_media_types=n_media_types,
    )


WF_KEYS = ["wf_test_id", "wf_text", "wf_title", "wf_label", "wf_input_type", "wf_profile"]


@app.route("/workflow", methods=["GET"])
def workflow_start():
    if request.args.get("reset"):
        for k in WF_KEYS:
            session.pop(k, None)
    wf = {
        "text": session.get("wf_text", ""),
        "title": session.get("wf_title", ""),
        "label": session.get("wf_label", ""),
        "input_type": session.get("wf_input_type", ""),
    }
    return render_template("workflow_input.html", active="workflow", wf=wf)


@app.route("/workflow/input", methods=["POST"])
def workflow_input():
    text = request.form.get("text", "").strip()
    title = request.form.get("title", "")

    uploaded = request.files.get("file")
    if uploaded and uploaded.filename:
        content, filename = _read_uploaded_file(uploaded)
        content = content.strip()
        if content:
            text = content
            if not title:
                title = re.sub(r"[_\-]+", " ", filename.rsplit(".", 1)[0]).strip()

    if not text:
        flash("Paste some text or upload a media file first.")
        return redirect(url_for("workflow_start"))

    label = request.form.get("label", "")
    input_type = request.form.get("input_type", "video_script")

    result = rate_input(text, title=title, source_label=label, input_type=input_type)

    session["wf_test_id"] = result["test_id"]
    session["wf_text"] = text
    session["wf_title"] = title
    session["wf_label"] = label
    session["wf_input_type"] = input_type
    session.pop("wf_profile", None)

    return redirect(url_for("workflow_style"))


@app.route("/workflow/style", methods=["GET"])
def workflow_style():
    test_id = session.get("wf_test_id")
    if not test_id:
        flash("Start by providing an input first.")
        return redirect(url_for("workflow_start"))

    conn = get_conn()
    test = conn.execute("SELECT * FROM test_inputs WHERE test_id=?", (test_id,)).fetchone()
    if not test:
        conn.close()
        flash("That input no longer exists — start again.")
        return redirect(url_for("workflow_start"))

    scores = conn.execute(
        """SELECT s.total_score, s.rank, p.profile_code, p.length_band, p.status,
                  p.n_videos_analysed, c.channel_name
           FROM test_scores s
           JOIN style_profiles p ON p.profile_id = s.profile_id
           JOIN channels c ON c.channel_id = p.channel_id
           WHERE s.test_id = ? ORDER BY s.rank""",
        (test_id,),
    ).fetchall()
    conn.close()

    short_profiles = [dict(r) for r in scores if r["length_band"] == "A"]
    long_profiles = [dict(r) for r in scores if r["length_band"] == "B"]
    other_profiles = [dict(r) for r in scores if r["length_band"] not in ("A", "B")]

    intrinsic = score_intrinsic(extract_auto_features(test["raw_text"], session.get("wf_title", "")))["total_score"]
    preview = (test["raw_text"][:280] + "…") if len(test["raw_text"]) > 280 else test["raw_text"]

    return render_template(
        "workflow_style.html", active="workflow",
        test=test, preview=preview, intrinsic=intrinsic,
        short_profiles=short_profiles, long_profiles=long_profiles, other_profiles=other_profiles,
    )


@app.route("/workflow/generate", methods=["POST"])
def workflow_generate():
    test_id = request.form.get("test_id", type=int)
    profile = request.form.get("profile", "")
    if not test_id or not profile:
        flash("Choose a profile style first.")
        return redirect(url_for("workflow_style"))

    try:
        prompt_text = build_transform_prompt(test_id, profile)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("workflow_style"))

    session["wf_profile"] = profile
    return render_template(
        "workflow_output.html", active="workflow",
        test_id=test_id, profile=profile, prompt_text=prompt_text,
    )


@app.route("/workflow/save", methods=["POST"])
def workflow_save():
    test_id = request.form.get("test_id", type=int)
    profile = request.form.get("profile", "")
    gen_title = request.form.get("gen_title", "")
    gen_text = request.form.get("gen_text", "").strip()

    if not test_id or not profile or not gen_text:
        flash("Missing test_id, profile, or generated text.")
        return redirect(url_for("workflow_style"))

    try:
        transformation_id = save_transformation(test_id, profile, gen_title, gen_text)
        result = score_transformation(transformation_id)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("workflow_style"))

    return render_template(
        "workflow_result.html", active="workflow",
        result=result, generated_title=gen_title, generated_text=gen_text,
    )


# Video's NATIVE attribute taxonomy — its own micro-sections, each rolling
# up into one of the 5 macros in MACRO_GROUPS below. This is the
# medium-specific view: fields here (Title, Hook, Close, Engagement/CTA...)
# have no video-native equivalent, so this grouping is only meaningful for
# SAME-MEDIA comparison (one video against another, or a video against a
# video-creator profile) — see SHARED_ATTRIBUTE_SECTIONS further down for
# the cross-media-comparable taxonomy used when a video is being compared
# against a book.
ATTRIBUTE_SECTIONS = [
    ("Title", ["title_format", "title_names_source", "title_word_count"]),
    ("Length & pacing", ["word_count", "beat_count", "avg_sentence_len", "median_sentence_len",
                          "sentence_len_variance", "sentence_rhythm_cv", "time_to_payoff_pct", "reveal_placement",
                          "pacing"]),
    ("Structure", ["beat_sequence", "formula_explicit", "framework_marker_count",
                   "closing_paragraph_ratio", "references_external_media", "structure_archetype"]),
    ("Hook", ["hook_type", "hook_word_count", "hook_names_source", "hook_source_word_position",
              "hook_ends_on_pivot", "hook_self_demonstrating"]),
    ("Close", ["close_type", "ends_on_question", "callback_to_hook"]),
    ("Diction", ["you_freq_per_100w", "i_freq_per_100w", "we_freq_per_100w", "question_count", "emdash_count",
                 "quote_count", "jargon_density", "colloquialism_density", "readability_score",
                 "register_shift_at_cta", "filler_retention", "filler_count", "word_economy_ratio",
                 "number_count", "instruction_verb_count", "lexical_diversity", "punctuation_density",
                 "prose_rhythm", "noun_verb_ratio_style", "syntax_pattern",
                 "adjective_intensity", "punctuation_delivery"]),
    ("Rhetoric", ["citation_style", "analogy_count", "names_bias_or_law", "named_bias_or_law_term",
                  "dialectic_structure", "certainty_register", "rule_of_three_present", "rule_of_three_count",
                  "rhetorical_mode", "explanation_mechanism", "contrast_structure_count",
                  "narrative_density", "counter_argument_engagement", "rhetorical_appeal_balance",
                  "contrarian_positioning", "rhythmic_repetition"]),
    ("Content taxonomy", ["domain", "concept_type", "source_era", "framing", "named_entity_count",
                           "information_density", "niche_slang_usage"]),
    ("Delivery", ["script_polish", "emphasis_markers_present", "humor_marker_count",
                  "shareability_trigger", "product_placement", "core_value_reinforcement"]),
    ("Engagement / CTA", ["has_cta", "cta_type", "cta_placement", "cta_count"]),
    ("Tone & Voice", ["tone", "emotional_register", "narrative_voice", "polemical_tone", "narrative_presence",
                       "vulnerability_depth", "condescension_vs_empowerment"]),
    ("Audience Engagement", ["value_promise", "curiosity_loop", "relatability_factor",
                              "identity_framing", "status_signaling"]),
    # Only meaningful for long-form (YouTube, 20-40+ min) content — every
    # field here stays NULL forever on short-form Instagram rows, same as
    # any other media-specific section (Hook/Close are the mirror image:
    # short-form-only, NULL on long-form rows once those exist too).
    ("Long-form Structure", ["chapter_count", "has_chapters", "cold_open_present", "intro_length_sec",
                              "sponsor_segment_present", "sponsor_segment_position", "act_count",
                              "re_engagement_hook_count", "outro_cta_count", "outro_type", "pacing_arc",
                              "topic_shift_count"]),
    ("Meta", ["classified_by", "classified_at"]),
]

# The level above ATTRIBUTE_SECTIONS, native to video only — books have no
# equivalent extra rollup (their sections are already macro-sized).
MACRO_GROUPS = [
    ("Structure & Pacing", ["Title", "Length & pacing", "Structure", "Hook", "Close", "Long-form Structure"]),
    ("Voice & Diction", ["Diction", "Tone & Voice"]),
    ("Rhetoric & Persuasion", ["Rhetoric"]),
    ("Content & Delivery", ["Content taxonomy", "Delivery"]),
    ("Engagement & CTA", ["Engagement / CTA", "Audience Engagement"]),
]

SECTION_TO_MACRO = {section: macro for macro, sections in MACRO_GROUPS for section in sections}

# The cross-media-comparable taxonomy: 14 macro categories shared with
# SHARED_BOOK_ATTRIBUTE_SECTIONS further down, used ONLY where books and
# shorts are being compared to each other directly (the Attributes page's
# "Shared attribute categories" section, and the "Attribute category fit"
# rollup on book/creation detail pages) — never for same-media scoring or
# display, which uses the native ATTRIBUTE_SECTIONS/MACRO_GROUPS above
# instead. Video's native micro-sections (Title, Length & pacing, Structure,
# Hook, Close, Engagement/CTA) don't survive as separate groupings here —
# their fields moved into whichever of the 14 fits best (mostly Structure &
# Organization and Delivery). Style & Craft has no video Auto/Class field
# yet (an honest gap: video's rubric doesn't compute literary-craft fields
# the way the book rubric's LLM classification pass does).
SHARED_ATTRIBUTE_SECTIONS = [
    ("Diction", ["you_freq_per_100w", "emdash_count", "quote_count", "register_shift_at_cta",
                 "filler_retention", "filler_count", "instruction_verb_count", "lexical_diversity",
                 "punctuation_density", "we_freq_per_100w", "word_economy_ratio",
                 "syntax_pattern", "adjective_intensity", "punctuation_delivery"]),
    ("Rhetoric", ["analogy_count", "rule_of_three_present", "rule_of_three_count",
                  "explanation_mechanism", "contrast_structure_count",
                  "rhetorical_appeal_balance", "rhythmic_repetition"]),
    ("Content Taxonomy", ["domain", "concept_type", "framing", "named_entity_count",
                           "information_density", "niche_slang_usage"]),
    ("Delivery", ["script_polish", "emphasis_markers_present", "humor_marker_count",
                  "has_cta", "cta_type", "cta_placement", "cta_count",
                  "shareability_trigger", "product_placement", "core_value_reinforcement"]),
    ("Thesis & Purpose", ["rhetorical_mode", "value_promise"]),
    ("Evidence & Authority", ["citation_style", "number_count"]),
    ("Tone & Voice", ["certainty_register", "i_freq_per_100w", "colloquialism_density",
                       "tone", "emotional_register", "narrative_voice", "polemical_tone",
                       "narrative_presence", "vulnerability_depth", "condescension_vs_empowerment"]),
    ("Structure & Organization", ["title_format", "title_names_source", "title_word_count",
                                   "word_count", "beat_count", "time_to_payoff_pct", "reveal_placement",
                                   "beat_sequence", "formula_explicit", "framework_marker_count",
                                   "closing_paragraph_ratio", "references_external_media",
                                   "hook_type", "hook_word_count", "hook_names_source",
                                   "hook_source_word_position", "hook_ends_on_pivot",
                                   "hook_self_demonstrating", "close_type", "ends_on_question",
                                   "callback_to_hook", "curiosity_loop", "structure_archetype"]),
    ("Target Audience", ["jargon_density", "relatability_factor", "identity_framing", "status_signaling"]),
    ("Bias & Assumptions", ["names_bias_or_law", "named_bias_or_law_term",
                             "counter_argument_engagement", "contrarian_positioning"]),
    ("Argument & Reasoning", ["dialectic_structure", "question_count", "narrative_density"]),
    ("Context & Positioning", ["source_era"]),
    ("Style & Craft", ["prose_rhythm", "noun_verb_ratio_style", "pacing"]),
    ("Readability", ["avg_sentence_len", "median_sentence_len", "sentence_len_variance",
                      "sentence_rhythm_cv", "readability_score"]),
]

FIELD_TYPES = {
    # numeric
    "title_word_count": "numeric", "word_count": "numeric", "beat_count": "numeric",
    "avg_sentence_len": "numeric", "median_sentence_len": "numeric", "sentence_len_variance": "numeric",
    "time_to_payoff_pct": "numeric", "hook_word_count": "numeric", "hook_source_word_position": "numeric",
    "you_freq_per_100w": "numeric", "i_freq_per_100w": "numeric", "question_count": "numeric",
    "emdash_count": "numeric", "quote_count": "numeric", "jargon_density": "numeric",
    "colloquialism_density": "numeric", "readability_score": "numeric", "filler_count": "numeric",
    "analogy_count": "numeric", "rule_of_three_count": "numeric", "cta_count": "numeric",
    "number_count": "numeric", "instruction_verb_count": "numeric", "framework_marker_count": "numeric",
    "sentence_rhythm_cv": "numeric", "closing_paragraph_ratio": "numeric", "lexical_diversity": "numeric",
    "punctuation_density": "numeric", "contrast_structure_count": "numeric", "named_entity_count": "numeric",
    "humor_marker_count": "numeric",
    "we_freq_per_100w": "numeric", "word_economy_ratio": "numeric",
    "chapter_count": "numeric", "intro_length_sec": "numeric", "act_count": "numeric",
    "re_engagement_hook_count": "numeric", "outro_cta_count": "numeric", "topic_shift_count": "numeric",
    # boolean (0/1 flags)
    "title_names_source": "boolean", "formula_explicit": "boolean", "hook_names_source": "boolean",
    "hook_ends_on_pivot": "boolean", "hook_self_demonstrating": "boolean", "ends_on_question": "boolean",
    "callback_to_hook": "boolean", "register_shift_at_cta": "boolean", "filler_retention": "boolean",
    "names_bias_or_law": "boolean", "dialectic_structure": "boolean", "rule_of_three_present": "boolean",
    "emphasis_markers_present": "boolean", "has_cta": "boolean", "references_external_media": "boolean",
    "curiosity_loop": "boolean", "identity_framing": "boolean", "rhythmic_repetition": "boolean",
    "core_value_reinforcement": "boolean",
    "has_chapters": "boolean", "cold_open_present": "boolean", "sponsor_segment_present": "boolean",
    # categorical
    "title_format": "categorical", "reveal_placement": "categorical", "beat_sequence": "categorical",
    "hook_type": "categorical", "close_type": "categorical", "citation_style": "categorical",
    "certainty_register": "categorical", "domain": "categorical", "concept_type": "categorical",
    "source_era": "categorical", "framing": "categorical", "script_polish": "categorical",
    "cta_type": "categorical", "cta_placement": "categorical", "explanation_mechanism": "categorical",
    "rhetorical_mode": "categorical",
    # ported from books
    "tone": "categorical", "emotional_register": "categorical", "narrative_voice": "categorical",
    "narrative_density": "categorical", "counter_argument_engagement": "categorical",
    "rhetorical_appeal_balance": "categorical", "prose_rhythm": "categorical",
    "noun_verb_ratio_style": "categorical", "syntax_pattern": "categorical", "pacing": "categorical",
    "polemical_tone": "categorical", "narrative_presence": "categorical",
    # new shared fields
    "value_promise": "categorical", "information_density": "categorical",
    "relatability_factor": "categorical", "contrarian_positioning": "categorical",
    "adjective_intensity": "categorical", "punctuation_delivery": "categorical",
    "vulnerability_depth": "categorical", "condescension_vs_empowerment": "categorical",
    # new video-only fields
    "structure_archetype": "categorical", "shareability_trigger": "categorical",
    "product_placement": "categorical", "status_signaling": "categorical", "niche_slang_usage": "categorical",
    # long-form-only fields
    "sponsor_segment_position": "categorical", "outro_type": "categorical", "pacing_arc": "categorical",
    # free text
    "named_bias_or_law_term": "text",
}

FIELD_DESCRIPTIONS = {
    "title_format": 'Structural/rhetorical pattern of the title — 17-value taxonomy (e.g. "Curiosity Gap / Teaser", "Name/Source + Concept", "Question").',
    "title_names_source": "Whether the title names the person/text being discussed.",
    "title_word_count": "Word count of the title.",
    "word_count": "Total word count of the script.",
    "beat_count": "Number of distinct paragraphs/beats in the script.",
    "avg_sentence_len": "Average sentence length, in words.",
    "median_sentence_len": "Median sentence length, in words.",
    "sentence_len_variance": "Variance in sentence length — a proxy for rhythm/pacing consistency.",
    "sentence_rhythm_cv": "Coefficient of variation (std/mean) of sentence length — normalized rhythm, "
                           "comparable across profiles with different average sentence lengths "
                           "(unlike raw variance, which scales with the mean).",
    "time_to_payoff_pct": "How far into the script (%) the core payoff/reveal lands.",
    "reveal_placement": "Where the reveal/payoff sits: front, mid, or end.",
    "beat_sequence": 'The sequence of structural beats, e.g. "Hook-Definition-Example-Close".',
    "formula_explicit": "Whether the script explicitly names its own formula/structure.",
    "framework_marker_count": 'Count of explicit enumerated-framework markers '
                               '("step one", "number two", "the third thing"...) — '
                               "a proxy for how much the script organizes itself as a named, numbered list.",
    "closing_paragraph_ratio": "Last paragraph's word count relative to the script's mean paragraph "
                                'length — below 1 means it "ends smaller than it started."',
    "references_external_media": "Whether the script explicitly calls out another clip/video/footage "
                                   "— a text-level proxy only, not real video/audio analysis.",
    "hook_type": "Rhetorical device used to open the script, e.g. rhetorical question.",
    "hook_word_count": "Word count of the opening hook.",
    "hook_names_source": "Whether the hook names the source/person up front.",
    "hook_source_word_position": "Word position at which the source is first named.",
    "hook_ends_on_pivot": "Whether the hook ends on a pivot/twist into the body.",
    "hook_self_demonstrating": "Whether the hook demonstrates the concept it's introducing.",
    "close_type": "How the script wraps up, e.g. question, summary, call-to-action.",
    "ends_on_question": "Whether the final line is a question.",
    "callback_to_hook": "Whether the close references back to the opening hook.",
    "you_freq_per_100w": 'Rate of direct address ("you") per 100 words.',
    "i_freq_per_100w": 'Rate of first-person ("I") per 100 words.',
    "question_count": "Number of questions in the script.",
    "emdash_count": "Number of em-dashes used.",
    "quote_count": "Number of quoted/anchoring lines.",
    "jargon_density": "Share of words that are domain jargon.",
    "colloquialism_density": "Share of words that are informal/colloquial.",
    "readability_score": "Flesch-Kincaid grade level.",
    "register_shift_at_cta": "Whether tone noticeably shifts at the call-to-action.",
    "filler_retention": "Whether filler words were deliberately kept in (vs. cleaned up).",
    "filler_count": 'Count of filler words/phrases ("well,", "you know", etc).',
    "number_count": "Count of numeric figures/statistics mentioned (digits, percentages).",
    "instruction_verb_count": 'Count of instructional/demo verbs ("take", "try", "look", "hold"...) — '
                               "a proxy for how hands-on/demonstrative the script is.",
    "lexical_diversity": "Type-token ratio (unique words / total words) — vocabulary richness, "
                          "length-sensitive so most comparable between similarly-sized scripts.",
    "punctuation_density": "Punctuation marks per 100 words.",
    "citation_style": "How sources are cited/named in the script.",
    "analogy_count": "Number of analogies used.",
    "names_bias_or_law": 'Whether a named cognitive bias or "law" is invoked.',
    "named_bias_or_law_term": "The specific bias/law term named, if any.",
    "dialectic_structure": "Whether the script uses a thesis/antithesis structure.",
    "certainty_register": "How certain/hedged the claims are.",
    "rule_of_three_present": "Whether a rule-of-three list pattern appears.",
    "rule_of_three_count": "How many rule-of-three instances appear.",
    "rhetorical_mode": "The video's overall communicative mode: story, advice, opinion, or "
                        "exploratory question.",
    "explanation_mechanism": "The primary mechanism used to make the concept land, "
                              "e.g. physical demonstration, statistic-led, analogy-led.",
    "contrast_structure_count": 'Count of explicit contrast structures ("X instead of Y", '
                                 '"rather than", "not X but Y").',
    "domain": "Subject domain, e.g. philosophy, psychology.",
    "concept_type": "Kind of concept being explained.",
    "source_era": "Era of the source material/thinker.",
    "framing": "Overall framing/angle taken on the content.",
    "named_entity_count": "Count of distinct capitalized multi-word names mentioned "
                           '(e.g. "Steve Jobs") — a lightweight proxy for who/what the script is about.',
    "script_polish": "How polished/scripted vs. conversational the delivery reads.",
    "emphasis_markers_present": "Whether emphasis markers (caps, italics cues) are present.",
    "humor_marker_count": "Coarse proxy count of humor/wit markers (joke words + exclamation marks) — "
                           "not true humor detection, just a cheap textual signal.",
    "has_cta": "Whether the script contains a call-to-action.",
    "cta_type": "Type of call-to-action used.",
    "cta_placement": "Where the call-to-action sits in the script.",
    "cta_count": "Number of calls-to-action present.",
    "we_freq_per_100w": 'Rate of first-person-plural ("we") per 100 words.',
    "word_economy_ratio": "Ratio of filler words to high-value words (instruction verbs, "
                           "framework markers, numeric figures) — how diluted the substance is.",
    # ported from books (same meaning as the book rubric's field of the same name)
    "tone": "Overall persuasive stance — objective, advocacy, skeptical, urgent, wry, or reverent.",
    "emotional_register": "The script's overall emotional feel, distinct from tone.",
    "narrative_voice": "How the narrator/speaker sounds — distinct, generic, formal, conversational, or multi-voiced.",
    "narrative_density": "How story-led versus argument-led the script is.",
    "counter_argument_engagement": "How seriously the script engages views it disagrees with.",
    "rhetorical_appeal_balance": "Whether the script leans on logic, emotion, authority, or a blend.",
    "prose_rhythm": "Sentence cadence — staccato, flowing, balanced, or monotonous.",
    "noun_verb_ratio_style": "Whether the script leans on nouns/nominalizations or stays verb-driven.",
    "syntax_pattern": "Sentence length, variety, and grammar pattern, as a qualitative category.",
    "pacing": "How fast or slow the script moves, as a qualitative category (distinct from the "
              "numeric sentence-length proxies above).",
    "polemical_tone": "How adversarial the language gets toward opposing views.",
    "narrative_presence": "How much the speaker inserts themselves into the script.",
    # new shared fields (also on the book rubric)
    "value_promise": "What the script implicitly promises the viewer for sticking with it.",
    "information_density": "How fact-packed versus loose/lifestyle-focused the script is.",
    "curiosity_loop": "Whether the script opens a question/tease early that's only resolved later or at the end.",
    "relatability_factor": "The kind of relatable hook used — specific pain point, universal experience, "
                            "niche/insider experience, or none.",
    "identity_framing": "Whether the language invites the viewer to see themselves in it "
                         "(a \"that's so me\" framing).",
    "contrarian_positioning": "How much the script positions itself against consensus/mainstream opinion.",
    "adjective_intensity": "Whether modifiers are extreme/superlative or neutral/objective.",
    "punctuation_delivery": "The qualitative punctuation pattern — rhetorical questions, exclamation-heavy, "
                             "trail-off, or measured.",
    "rhythmic_repetition": "Whether the script uses anaphora (deliberately reusing a phrase starter for "
                            "rhythmic effect).",
    "vulnerability_depth": "How openly the speaker shares mistakes/failures versus staying purely authoritative.",
    "condescension_vs_empowerment": "Whether the script speaks down to the viewer or with them.",
    # new video-only fields
    "structure_archetype": "The short-form structural pattern used — problem/solution, listicle, story, "
                            "tutorial, myth-bust, comparison, or single-concept explainer.",
    "shareability_trigger": "What (if anything) makes the script save/share-worthy.",
    "product_placement": "Whether the script mentions a product/service, and how overtly.",
    "core_value_reinforcement": "Whether the script repeats/reinforces a consistent tagline or core message.",
    "status_signaling": "Whether watching/sharing the script is framed as implying sophistication, "
                         "belonging, or competence.",
    "niche_slang_usage": "How much in-group/community-specific slang the script uses.",
    # long-form-only fields (YouTube, 20-40+ min) — always NULL on short-form Instagram rows
    "chapter_count": "Number of chapters, from YouTube's own chapter markers (when present).",
    "has_chapters": "Whether the video has YouTube chapter markers at all.",
    "cold_open_present": "Whether the video opens with a teaser/preview before its main intro.",
    "intro_length_sec": "Seconds before the main content/thesis actually starts.",
    "sponsor_segment_present": "Whether the script contains a sponsor/ad-read segment.",
    "sponsor_segment_position": "Where the sponsor segment sits in the runtime: early, mid, late, or none.",
    "act_count": "Number of distinct large structural movements (not micro-beats) the video moves through.",
    "re_engagement_hook_count": 'Count of mid-video re-hooks ("but here\'s where it gets interesting"-style) '
                                 "used to fight viewer drop-off over a long runtime.",
    "outro_cta_count": "Count of calls-to-action in the closing portion of the script "
                        "(subscribe/bell, related video, membership, comment prompt).",
    "outro_type": "How the video wraps up — e.g. summary, CTA-stack, cliffhanger/teaser for next video.",
    "pacing_arc": "How pacing changes across the full runtime: steady, accelerating, or slows-then-quickens.",
    "topic_shift_count": "Number of distinct sub-topics the video covers.",
}

def _fmt_num(v):
    if v is None:
        return "—"
    v = float(v)
    return str(int(v)) if v.is_integer() else f"{v:.2f}"


def _format_duration(seconds):
    if seconds is None:
        return None
    total = int(round(seconds))
    m, s = divmod(total, 60)
    return f"{m}:{s:02d}"


def _trim(text, max_chars):
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "…"
    return text


def _auto_summary(script, max_chars=110):
    """Extractive summary — the script's own opening line plus its closing
    line (hook + payoff), not just a prefix, so it reads as an actual gist
    rather than duplicating the raw script preview shown alongside it. Not
    an abstractive/LLM summary: a cheap, deterministic stand-in so every
    video has *something* here without a manual pass over the whole corpus."""
    sents = _sentences(script)
    if not sents:
        return "—"
    if len(sents) == 1:
        return _trim(sents[0], max_chars * 2)
    opener, closer = _trim(sents[0], max_chars), _trim(sents[-1], max_chars)
    return f"{opener} […] {closer}"


def _profile_macro_coverage(conn, profile_id):
    """For a given profile, roll up all tracked attributes into the 5 native
    video macro categories (MACRO_GROUPS) and report what share of each
    macro's attributes actually made it into this profile's fingerprint
    (numeric or categorical) — a same-media view of a video-creator
    profile's own coverage. An attribute can be missing either because no
    video has been classified for it yet, or because profile_builder.py
    doesn't fingerprint that field at all (e.g. the boolean flags) — both
    show up the same way here: not yet part of the fingerprint."""
    present = {
        r["attribute"] for r in conn.execute(
            "SELECT DISTINCT attribute FROM profile_fingerprint_numeric WHERE profile_id=?", (profile_id,)
        )
    } | {
        r["attribute"] for r in conn.execute(
            "SELECT DISTINCT attribute FROM profile_fingerprint_categorical WHERE profile_id=?", (profile_id,)
        )
    }

    macros = []
    for macro_name, sections in MACRO_GROUPS:
        fields = [f for s in sections for f in dict(ATTRIBUTE_SECTIONS).get(s, [])]
        covered = sum(1 for f in fields if f in present)
        pct = round(covered / len(fields) * 100, 1) if fields else 0.0
        macros.append({"name": macro_name, "count": len(fields), "covered": covered, "pct": pct})
    return macros


def _book_profile_macro_coverage(conn, profile_id):
    """Book-profile counterpart to _profile_macro_coverage: each entry in
    BOOK_ATTRIBUTE_SECTIONS already is a macro-sized group (there's no extra
    section->macro rollup layer for books), so this just reports what share
    of each section's fields made it into this profile's fingerprint."""
    present = {
        r["attribute"] for r in conn.execute(
            "SELECT DISTINCT attribute FROM profile_fingerprint_numeric WHERE profile_id=?", (profile_id,)
        )
    } | {
        r["attribute"] for r in conn.execute(
            "SELECT DISTINCT attribute FROM profile_fingerprint_categorical WHERE profile_id=?", (profile_id,)
        )
    }

    macros = []
    for section_name, fields in BOOK_ATTRIBUTE_SECTIONS:
        covered = sum(1 for f in fields if f in present)
        pct = round(covered / len(fields) * 100, 1) if fields else 0.0
        macros.append({"name": section_name, "count": len(fields), "covered": covered, "pct": pct})
    return macros


ATTRIBUTE_MEDIA_TYPES = [
    {
        "slug": "books", "name": "Books", "icon": "📚", "implemented": True,
        "desc": "Full-length books, scored per book against a 42-field rubric covering thesis, evidence, "
                "tone, structure, audience, argument architecture, and literary style & craft.",
    },
    {
        "slug": "instagram", "name": "Short videos, Instagram", "icon": "📱", "implemented": True,
        "desc": "Short-form Instagram scripts, scored per video against the structural, rhetorical, and "
                "diction rubric — hook, close, pacing, CTA, and everything in between.",
    },
    {
        "slug": "youtube", "name": "Long videos, YouTube", "icon": "▶️", "implemented": True,
        "desc": "Long-form YouTube scripts, scored per video against the same structural rubric as "
                "Instagram, plus a Long-form Structure section (cold opens, sponsor segments, act count, "
                "pacing arc) and a chapter-by-chapter breakdown for videos long enough to need one.",
    },
    {
        "slug": "news", "name": "News Articles", "icon": "📰", "implemented": False,
        "desc": "News articles will get their own attribute schema once ingestion is built for this "
                "media type.",
    },
]

BOOK_FIELD_TYPES = {
    "uses_visual_aids": "boolean",
    "avg_sentence_len": "numeric", "avg_syllables_per_word": "numeric", "readability_score": "numeric",
}


def _video_attribute_field_rows(conn, media_type_filter):
    total = conn.execute(
        "SELECT COUNT(*) FROM video_attributes a JOIN videos v ON v.video_id = a.video_id "
        "WHERE v.media_type = ?", (media_type_filter,)
    ).fetchone()[0]

    field_rows = []
    for section_name, fields in ATTRIBUTE_SECTIONS:
        if section_name == "Meta":
            continue
        for field in fields:
            ftype = FIELD_TYPES.get(field, "categorical")
            populated = conn.execute(
                f"SELECT COUNT(a.{field}) FROM video_attributes a JOIN videos v ON v.video_id = a.video_id "
                f"WHERE v.media_type = ?", (media_type_filter,)
            ).fetchone()[0]
            pct = round(populated / total * 100, 1) if total else 0.0

            extra = None
            if populated:
                if ftype == "numeric":
                    mn, mx, avg = conn.execute(
                        f"SELECT MIN(a.{field}), MAX(a.{field}), AVG(a.{field}) FROM video_attributes a "
                        f"JOIN videos v ON v.video_id = a.video_id "
                        f"WHERE v.media_type = ? AND a.{field} IS NOT NULL", (media_type_filter,)
                    ).fetchone()
                    extra = f"range {_fmt_num(mn)}–{_fmt_num(mx)}, mean {_fmt_num(avg)}"
                elif ftype == "boolean":
                    true_count = conn.execute(
                        f"SELECT SUM(a.{field}) FROM video_attributes a JOIN videos v ON v.video_id = a.video_id "
                        f"WHERE v.media_type = ? AND a.{field} IS NOT NULL", (media_type_filter,)
                    ).fetchone()[0] or 0
                    extra = f"{true_count} true / {populated - true_count} false"
                else:
                    distinct = conn.execute(
                        f"SELECT COUNT(DISTINCT a.{field}) FROM video_attributes a "
                        f"JOIN videos v ON v.video_id = a.video_id "
                        f"WHERE v.media_type = ? AND a.{field} IS NOT NULL", (media_type_filter,)
                    ).fetchone()[0]
                    extra = f"{distinct} distinct value{'s' if distinct != 1 else ''}"

            field_rows.append({
                "name": field, "type": ftype, "section": section_name,
                "macro": SECTION_TO_MACRO.get(section_name, "Other"),
                "description": FIELD_DESCRIPTIONS.get(field, ""),
                "populated": populated, "total": total, "pct": pct, "extra": extra,
            })
    return total, field_rows


def _book_attribute_field_rows(conn):
    total = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]

    field_rows = []
    for section_name, fields in BOOK_ATTRIBUTE_SECTIONS:
        for field in fields:
            ftype = BOOK_FIELD_TYPES.get(field, "categorical")
            populated = conn.execute(
                f"SELECT COUNT({field}) FROM book_attributes WHERE {field} IS NOT NULL AND {field} != ''"
            ).fetchone()[0]
            pct = round(populated / total * 100, 1) if total else 0.0

            extra = None
            if populated:
                if ftype == "numeric":
                    mn, mx, avg = conn.execute(
                        f"SELECT MIN({field}), MAX({field}), AVG({field}) FROM book_attributes "
                        f"WHERE {field} IS NOT NULL"
                    ).fetchone()
                    extra = f"range {_fmt_num(mn)}–{_fmt_num(mx)}, mean {_fmt_num(avg)}"
                elif ftype == "boolean":
                    true_count = conn.execute(
                        f"SELECT SUM({field}) FROM book_attributes WHERE {field} IS NOT NULL"
                    ).fetchone()[0] or 0
                    extra = f"{true_count} true / {populated - true_count} false"
                else:
                    distinct = conn.execute(
                        f"SELECT COUNT(DISTINCT {field}) FROM book_attributes "
                        f"WHERE {field} IS NOT NULL AND {field} != ''"
                    ).fetchone()[0]
                    extra = f"{distinct} distinct value{'s' if distinct != 1 else ''}"

            field_rows.append({
                "name": field, "type": ftype, "section": section_name, "macro": section_name,
                "description": BOOK_FIELD_LABELS.get(field, ""),
                "populated": populated, "total": total, "pct": pct, "extra": extra,
            })
    return total, field_rows


def _macro_summary(field_rows, macro_names):
    macros = []
    for macro_name in macro_names:
        attrs = [r for r in field_rows if r["macro"] == macro_name]
        avg_pct = round(sum(r["pct"] for r in attrs) / len(attrs), 1) if attrs else 0.0
        macros.append({
            "name": macro_name,
            "count": len(attrs),
            "avg_pct": avg_pct,
            "fully_populated": sum(1 for r in attrs if r["pct"] >= 99.9),
            "empty": sum(1 for r in attrs if r["pct"] == 0),
        })
    return macros


def _attribute_cards(conn):
    cards = []
    for mt in ATTRIBUTE_MEDIA_TYPES:
        if not mt["implemented"]:
            cards.append({**mt, "item_count": 0, "field_count": 0, "avg_pct": 0.0})
            continue
        if mt["slug"] == "instagram":
            total, field_rows = _video_attribute_field_rows(conn, "Instagram")
        elif mt["slug"] == "youtube":
            total, field_rows = _video_attribute_field_rows(conn, "YouTube")
        elif mt["slug"] == "books":
            total, field_rows = _book_attribute_field_rows(conn)
        else:
            total, field_rows = 0, []
        avg_pct = round(sum(r["pct"] for r in field_rows) / len(field_rows), 1) if field_rows else 0.0
        cards.append({**mt, "item_count": total, "field_count": len(field_rows), "avg_pct": avg_pct})
    return cards


def _macro_slug(name):
    return name.lower().replace(" & ", "-").replace(" ", "-")


# One or two sentences per shared macro, framed around what the category
# actually captures across both media (not just a repeat of the field
# names) — shown at the top of each category's detail page.
SHARED_MACRO_BLURBS = {
    "Diction": "Word-choice and phrasing mechanics — direct address, filler words, punctuation rhythm, "
               "vocabulary richness, and (for books) formal diction and syntax register.",
    "Rhetoric": "Persuasive technique — analogies, rule-of-three patterns, contrast structures, and how "
                "hard an argument leans on logic versus emotion versus authority.",
    "Content Taxonomy": "What the piece is actually about — subject domain, concept type, framing, and "
                         "named entities referenced. Currently video-only; the book rubric doesn't tag "
                         "subject taxonomy at the attribute level.",
    "Delivery": "How the content is packaged and pushed toward action — production polish, emphasis "
                "markers, humor, and calls-to-action. Currently video-only; books have no CTA/delivery "
                "mechanic to capture.",
    "Thesis & Purpose": "The piece's central claim and what it's trying to accomplish — inform, "
                         "persuade, critique, or instruct.",
    "Evidence & Authority": "What backs up the piece's claims — citation style, evidence type, and how "
                             "much of the argument rests on cited sources versus bare assertion.",
    "Tone & Voice": "The emotional/persuasive stance and narrator voice — how certain, personal, or "
                    "polemical the piece reads.",
    "Structure & Organization": "How the piece is built and paced — opening hooks, structural framework, "
                                 "section/beat sequencing, and how it closes.",
    "Target Audience": "Who the piece is written for — vocabulary complexity and how accessible its "
                        "jargon is.",
    "Bias & Assumptions": "Hidden viewpoints and unstated beliefs, and how directly the piece engages "
                           "(or ignores) opposing views.",
    "Argument & Reasoning": "The logical architecture underneath the claims — how they're built, how "
                             "falsifiable they are, and how densely reasoned the piece is.",
    "Context & Positioning": "How the piece situates itself in time and against other work — era, "
                              "comparative references, and named frameworks it introduces.",
    "Style & Craft": "Literary technique — sensory language, figurative language, narrative distance, "
                      "and prose rhythm. Currently book-only; video's rubric doesn't reach this depth yet.",
    "Readability": "How easy the piece is to read — sentence length and Flesch-Kincaid grade level.",
}


def _shared_macro_category_detail(conn, macro_name):
    """Everything for one shared macro's cross-media detail page: its
    constituent fields (both media), the profiles most characteristic of
    each field (from the fingerprint tables, which already record "how
    often does profile X use value Y" for categorical fields and mean/
    extremes for numeric ones), and 1-2 real ingested items exemplifying
    each of those — so "most prominent profile styles" and "examples where
    it's applied most" are both read directly off real data, not curated
    by hand."""
    video_fields = dict(SHARED_ATTRIBUTE_SECTIONS).get(macro_name, [])
    book_fields = dict(SHARED_BOOK_ATTRIBUTE_SECTIONS).get(macro_name, [])

    fields = (
        [{"field": f, "medium": "Short videos, Instagram", "type": FIELD_TYPES.get(f, "categorical"),
          "description": FIELD_DESCRIPTIONS.get(f, "")} for f in video_fields]
        + [{"field": f, "medium": "Books", "type": BOOK_FIELD_TYPES.get(f, "categorical"),
            "description": BOOK_FIELD_LABELS.get(f, "")} for f in book_fields]
    )

    def _examples_for_value(field, value, medium):
        if medium == "Short videos, Instagram":
            rows = conn.execute(
                f"""SELECT v.video_id AS id, v.title, c.channel_name AS source FROM video_attributes a
                    JOIN videos v ON v.video_id = a.video_id JOIN channels c ON c.channel_id = v.channel_id
                    WHERE a.{field} = ? LIMIT 2""",
                (value,),
            ).fetchall()
            return [{"label": r["title"], "source": r["source"], "url": url_for("input_detail", video_id=r["id"])} for r in rows]
        rows = conn.execute(
            f"""SELECT b.book_id AS id, b.title, b.author AS source FROM book_attributes a
                JOIN books b ON b.book_id = a.book_id WHERE a.{field} = ? LIMIT 2""",
            (value,),
        ).fetchall()
        return [{"label": r["title"], "source": r["source"], "url": url_for("book_detail", book_id=r["id"])} for r in rows]

    def _examples_for_extreme(field, medium, direction="DESC"):
        if medium == "Short videos, Instagram":
            rows = conn.execute(
                f"""SELECT v.video_id AS id, v.title, c.channel_name AS source, a.{field} AS val FROM video_attributes a
                    JOIN videos v ON v.video_id = a.video_id JOIN channels c ON c.channel_id = v.channel_id
                    WHERE a.{field} IS NOT NULL ORDER BY a.{field} {direction} LIMIT 2""",
            ).fetchall()
            return [{"label": r["title"], "source": r["source"], "val": r["val"],
                      "url": url_for("input_detail", video_id=r["id"])} for r in rows]
        rows = conn.execute(
            f"""SELECT b.book_id AS id, b.title, b.author AS source, a.{field} AS val FROM book_attributes a
                JOIN books b ON b.book_id = a.book_id WHERE a.{field} IS NOT NULL ORDER BY a.{field} {direction} LIMIT 2""",
        ).fetchall()
        return [{"label": r["title"], "source": r["source"], "val": r["val"],
                  "url": url_for("book_detail", book_id=r["id"])} for r in rows]

    categorical_highlights = []
    numeric_highlights = []
    for finfo in fields:
        field, medium, ftype = finfo["field"], finfo["medium"], finfo["type"]
        # Some field NAMES are now shared between media (e.g. "tone" exists in
        # both video_attributes and book_attributes), so profile_fingerprint_*
        # can hold rows for the same attribute name from BOTH a video profile
        # and a book profile — filtering by style_profiles.media_type is
        # required here, or a video field's "top profile" could silently
        # surface a book profile's fingerprint row instead (and vice versa).
        db_media_type = "Instagram" if medium == "Short videos, Instagram" else "Book"
        if ftype in ("numeric",):
            top = conn.execute(
                """SELECT p.profile_code, fn.mean_val FROM profile_fingerprint_numeric fn
                   JOIN style_profiles p ON p.profile_id = fn.profile_id
                   WHERE fn.attribute = ? AND p.media_type = ? ORDER BY fn.mean_val DESC LIMIT 1""",
                (field, db_media_type),
            ).fetchone()
            if top:
                numeric_highlights.append({
                    "field": field, "description": finfo["description"], "medium": medium,
                    "top_profile": top["profile_code"], "top_mean": round(top["mean_val"], 2),
                    "examples": _examples_for_extreme(field, medium, "DESC"),
                })
        else:
            top = conn.execute(
                """SELECT p.profile_code, fc.value, fc.share_pct FROM profile_fingerprint_categorical fc
                   JOIN style_profiles p ON p.profile_id = fc.profile_id
                   WHERE fc.attribute = ? AND p.media_type = ? ORDER BY fc.share_pct DESC LIMIT 1""",
                (field, db_media_type),
            ).fetchone()
            if top:
                categorical_highlights.append({
                    "field": field, "description": finfo["description"], "medium": medium,
                    "top_profile": top["profile_code"], "top_value": top["value"], "top_share": top["share_pct"],
                    "examples": _examples_for_value(field, top["value"], medium),
                })

    categorical_highlights.sort(key=lambda h: h["top_share"], reverse=True)
    return {
        "name": macro_name,
        "blurb": SHARED_MACRO_BLURBS.get(macro_name, ""),
        "fields": fields,
        "categorical_highlights": categorical_highlights,
        "numeric_highlights": numeric_highlights,
    }


@app.route("/attributes/category/<slug>")
def attribute_category_detail(slug):
    shared_macro_names = [name for name, _ in SHARED_ATTRIBUTE_SECTIONS]
    macro_name = next((n for n in shared_macro_names if _macro_slug(n) == slug), None)
    if not macro_name:
        flash(f"No such shared attribute category: {slug}")
        return redirect(url_for("attributes_page"))

    conn = get_conn()
    detail = _shared_macro_category_detail(conn, macro_name)
    conn.close()
    return render_template("attribute_category_detail.html", active="attributes", detail=detail)


def _shared_macro_cards(conn):
    """The 14 cross-media macro categories (SHARED_ATTRIBUTE_SECTIONS /
    SHARED_BOOK_ATTRIBUTE_SECTIONS) — the tier where books and shorts can be
    compared macro-for-macro. field_rows from _video_attribute_field_rows()/
    _book_attribute_field_rows() are tagged with each medium's NATIVE macro
    (for same-media use elsewhere), so this remaps every field to its shared
    bucket independently before rolling up coverage — reusing that native
    tag here would silently misfile fields like the book rubric's diction/
    syntax_pattern (native macro: Style & Craft, shared macro: Diction). A
    macro with 0 fields on one side (e.g. Content Taxonomy/Delivery for
    books, Style & Craft for video) is an honest gap, not an error — that
    medium's rubric just doesn't cover that dimension yet."""
    shared_macro_names = [name for name, _ in SHARED_ATTRIBUTE_SECTIONS]
    field_to_shared_macro = {}
    for name, fields in SHARED_ATTRIBUTE_SECTIONS:
        for f in fields:
            field_to_shared_macro[f] = name
    for name, fields in SHARED_BOOK_ATTRIBUTE_SECTIONS:
        for f in fields:
            field_to_shared_macro.setdefault(f, name)

    _, book_rows = _book_attribute_field_rows(conn)
    book_rows_shared = [{**r, "macro": field_to_shared_macro.get(r["name"], "Other")} for r in book_rows]
    book_macros = {m["name"]: m for m in _macro_summary(book_rows_shared, shared_macro_names)}

    _, video_rows = _video_attribute_field_rows(conn, "Instagram")
    video_rows_shared = [{**r, "macro": field_to_shared_macro.get(r["name"], "Other")} for r in video_rows]
    video_macros = {m["name"]: m for m in _macro_summary(video_rows_shared, shared_macro_names)}

    return [
        {"name": name, "slug": _macro_slug(name), "book": book_macros.get(name), "video": video_macros.get(name)}
        for name in shared_macro_names
    ]


@app.route("/attributes")
def attributes_page():
    conn = get_conn()
    cards = _attribute_cards(conn)
    shared_macros = _shared_macro_cards(conn)
    conn.close()
    # Share of the total attribute FIELD CATALOG each implemented media type
    # contributes (45 book fields, 63 Instagram fields, etc) — reuses the same
    # {label: weight} -> pie-slices helper the Creations valuation pie uses,
    # just keyed by media type instead of scoring macro.
    media_field_weight = {c["name"]: c["field_count"] for c in cards if c["implemented"] and c["field_count"]}
    attribute_media_share = _valuation_slices(media_field_weight)
    return render_template(
        "attributes.html", active="attributes", cards=cards, shared_macros=shared_macros,
        attribute_media_share=attribute_media_share,
    )


@app.route("/attributes/<slug>")
def attribute_media_detail(slug):
    mt = next((m for m in ATTRIBUTE_MEDIA_TYPES if m["slug"] == slug), None)
    if not mt:
        flash(f"No such attribute category: {slug}")
        return redirect(url_for("attributes_page"))

    if not mt["implemented"]:
        return render_template(
            "attribute_detail.html", active="attributes", media=mt,
            implemented=False, macros=[], sections=[], total_items=0, total_fields=0,
        )

    conn = get_conn()
    if slug in ("instagram", "youtube"):
        total, field_rows = _video_attribute_field_rows(conn, "Instagram" if slug == "instagram" else "YouTube")
        macro_names = [name for name, _ in MACRO_GROUPS]
        macros = _macro_summary(field_rows, macro_names)
        sections_out = [
            (section_name, [r for r in field_rows if r["section"] == section_name])
            for section_name, fields in ATTRIBUTE_SECTIONS if section_name != "Meta" and fields
        ]
    else:  # books
        total, field_rows = _book_attribute_field_rows(conn)
        macro_names = [name for name, _ in BOOK_ATTRIBUTE_SECTIONS]
        macros = _macro_summary(field_rows, macro_names)
        sections_out = [
            (section_name, [r for r in field_rows if r["section"] == section_name])
            for section_name, fields in BOOK_ATTRIBUTE_SECTIONS if fields
        ]
    conn.close()

    return render_template(
        "attribute_detail.html", active="attributes", media=mt, implemented=True,
        macros=macros, sections=sections_out, total_items=total, total_fields=len(field_rows),
    )


@app.route("/tags")
def tags_page():
    """Every term/example occurrence across videos and books, one row per
    occurrence (not deduplicated) so each row can carry its own source
    channel/author, description, and link back to that specific analysis —
    the browsable index for the term/example tagging added to Inputs
    filtering. The Term/Example value itself still links to the Inputs
    filter that finds everything else sharing it."""
    conn = get_conn()
    term_rows = conn.execute(
        """SELECT vt.term AS value, vt.definition AS description,
                  c.channel_name AS source_name, p.profile_code AS profile_code,
                  v.video_id AS video_id, NULL AS book_id
           FROM video_terms vt
           JOIN videos v ON v.video_id = vt.video_id
           JOIN channels c ON c.channel_id = v.channel_id
           LEFT JOIN style_profiles p ON p.channel_id = c.channel_id
                AND (p.media_type IS NULL OR p.media_type != 'ProductionSpec')
           WHERE vt.term IS NOT NULL AND vt.term != ''
           UNION ALL
           SELECT bt.term AS value, bt.definition AS description,
                  b.author AS source_name, p.profile_code AS profile_code,
                  NULL AS video_id, b.book_id AS book_id
           FROM book_terms bt
           JOIN books b ON b.book_id = bt.book_id
           LEFT JOIN channels c ON c.channel_name = b.author AND c.platform = 'Book'
           LEFT JOIN style_profiles p ON p.channel_id = c.channel_id
                AND (p.media_type IS NULL OR p.media_type != 'ProductionSpec')
           WHERE bt.term IS NOT NULL AND bt.term != ''
           ORDER BY value COLLATE NOCASE, source_name COLLATE NOCASE"""
    ).fetchall()
    example_rows = conn.execute(
        """SELECT ve.example_title AS value, ve.example_text AS description,
                  c.channel_name AS source_name, p.profile_code AS profile_code,
                  v.video_id AS video_id, NULL AS book_id, ve.example_id AS example_id
           FROM video_examples ve
           JOIN videos v ON v.video_id = ve.video_id
           JOIN channels c ON c.channel_id = v.channel_id
           LEFT JOIN style_profiles p ON p.channel_id = c.channel_id
                AND (p.media_type IS NULL OR p.media_type != 'ProductionSpec')
           WHERE ve.example_title IS NOT NULL AND ve.example_title != ''
           UNION ALL
           SELECT be.example_title AS value, be.example_text AS description,
                  b.author AS source_name, p.profile_code AS profile_code,
                  NULL AS video_id, b.book_id AS book_id, be.example_id AS example_id
           FROM book_examples be
           JOIN books b ON b.book_id = be.book_id
           LEFT JOIN channels c ON c.channel_name = b.author AND c.platform = 'Book'
           LEFT JOIN style_profiles p ON p.channel_id = c.channel_id
                AND (p.media_type IS NULL OR p.media_type != 'ProductionSpec')
           WHERE be.example_title IS NOT NULL AND be.example_title != ''
           ORDER BY value COLLATE NOCASE, source_name COLLATE NOCASE"""
    ).fetchall()
    conn.close()

    def _detail_url(video_id, book_id, anchor=None):
        if video_id is not None:
            url = url_for("input_detail", video_id=video_id)
        else:
            url = url_for("book_detail", book_id=book_id)
        return f"{url}#example-{anchor}" if anchor is not None else url

    terms = [
        {
            "value": r["value"],
            "description": _trim(r["description"], 160) if r["description"] else None,
            "source_name": r["source_name"] or "—",
            "profile_code": r["profile_code"],
            "detail_url": _detail_url(r["video_id"], r["book_id"]),
        }
        for r in term_rows
    ]
    examples = [
        {
            "value": r["value"],
            "description": _trim(r["description"], 160) if r["description"] else None,
            "source_name": r["source_name"] or "—",
            "profile_code": r["profile_code"],
            "detail_url": _detail_url(r["video_id"], r["book_id"], anchor=r["example_id"]),
        }
        for r in example_rows
    ]
    return render_template(
        "tags.html", active="tags", terms=terms, examples=examples,
        n_terms=len(terms), n_examples=len(examples),
    )


INPUT_KINDS = [
    ("", "All types"),
    ("video", "Instagram videos"),
    ("youtube", "YouTube videos"),
    ("book", "Books"),
    # News articles will get their own kind here once ingestion exists for them.
]


@app.route("/inputs")
def inputs_list():
    kind = request.args.get("kind", "").strip()
    channel_id = request.args.get("channel_id", type=int)
    q = request.args.get("q", "").strip()
    title_format = request.args.get("title_format", "").strip()
    term = request.args.get("term", "").strip()
    example = request.args.get("example", "").strip()
    word_min = request.args.get("word_min", type=int)
    word_max = request.args.get("word_max", type=int)
    sort = request.args.get("sort", "ingested_at")
    direction = "asc" if request.args.get("dir") == "asc" else "desc"

    conn = get_conn()
    rows = []

    if kind != "book":
        where, params = [], []
        if kind == "video":
            where.append("v.media_type = 'Instagram'")
        elif kind == "youtube":
            where.append("v.media_type = 'YouTube'")
        if channel_id:
            where.append("v.channel_id = ?")
            params.append(channel_id)
        if q:
            where.append("v.title LIKE ?")
            params.append(f"%{q}%")
        if title_format:
            where.append("a.title_format = ?")
            params.append(title_format)
        if word_min is not None:
            where.append("a.word_count >= ?")
            params.append(word_min)
        if word_max is not None:
            where.append("a.word_count <= ?")
            params.append(word_max)
        if term:
            where.append("EXISTS (SELECT 1 FROM video_terms vt WHERE vt.video_id = v.video_id AND vt.term = ? COLLATE NOCASE)")
            params.append(term)
        if example:
            where.append("EXISTS (SELECT 1 FROM video_examples ve WHERE ve.video_id = v.video_id AND ve.example_title = ? COLLATE NOCASE)")
            params.append(example)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        video_rows = conn.execute(
            f"""SELECT v.video_id, v.title, v.url, v.media_type, v.ingested_at, v.duration_sec,
                       c.channel_name, c.channel_id, p.profile_code, a.word_count, a.title_format,
                       a.you_freq_per_100w, a.readability_score
                FROM videos v
                JOIN channels c ON c.channel_id = v.channel_id
                LEFT JOIN style_profiles p ON p.channel_id = c.channel_id
                     AND (p.media_type IS NULL OR p.media_type != 'ProductionSpec')
                LEFT JOIN video_attributes a ON a.video_id = v.video_id
                {where_sql}""",
            params,
        ).fetchall()
        for v in video_rows:
            rows.append({
                "kind": "video",
                # Identity for the right-click rename/delete menu. The row dicts
                # are display-shaped (title/labels/urls), so without this the
                # template has nothing to put in data-id.
                "entity_id": v["video_id"],
                "media_type": v["media_type"] or "Instagram",
                "channel_name": v["channel_name"],
                "channel_id": v["channel_id"],
                "profile_code": v["profile_code"],
                "unresolved_channel": v["channel_name"] == "Instagram Import",
                "title": v["title"],
                "title_format": v["title_format"],
                "word_count": v["word_count"],
                "duration_sec": v["duration_sec"],
                "duration_label": _format_duration(v["duration_sec"]),
                "you_freq_per_100w": v["you_freq_per_100w"],
                "readability_score": v["readability_score"],
                "source_url": v["url"],
                "source_label": "source ↗",
                "status_pill": None,
                "ingested_at": v["ingested_at"],
                "detail_url": url_for("input_detail", video_id=v["video_id"]),
                "quickview_url": url_for("input_quickview_json", video_id=v["video_id"]),
            })

    if kind not in ("video", "youtube") and not title_format:
        # title_format has no book equivalent, so it's still video-only; channel_id
        # now applies to books too, matched via the author->channel join below.
        where, params = [], []
        if channel_id:
            where.append("c.channel_id = ?")
            params.append(channel_id)
        if q:
            where.append("b.title LIKE ?")
            params.append(f"%{q}%")
        if word_min is not None:
            where.append("b.word_count >= ?")
            params.append(word_min)
        if word_max is not None:
            where.append("b.word_count <= ?")
            params.append(word_max)
        if term:
            where.append("EXISTS (SELECT 1 FROM book_terms bt WHERE bt.book_id = b.book_id AND bt.term = ? COLLATE NOCASE)")
            params.append(term)
        if example:
            where.append("EXISTS (SELECT 1 FROM book_examples be WHERE be.book_id = b.book_id AND be.example_title = ? COLLATE NOCASE)")
            params.append(example)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        book_rows = conn.execute(
            f"""SELECT b.book_id, b.title, b.author, b.word_count, b.page_count, b.ingested_at,
                       b.source_file_path, b.source_note,
                       COALESCE(a.classified_by, 'pending') AS classified_by, a.readability_score, p.profile_code
                FROM books b
                LEFT JOIN book_attributes a ON a.book_id = b.book_id
                LEFT JOIN channels c ON c.channel_name = b.author AND c.platform = 'Book'
                LEFT JOIN style_profiles p ON p.channel_id = c.channel_id
                     AND (p.media_type IS NULL OR p.media_type != 'ProductionSpec')
                {where_sql}""",
            params,
        ).fetchall()
        for b in book_rows:
            source_url = "file://" + quote(b["source_file_path"], safe="/") if b["source_file_path"] else None
            # books ingested via the live API (instagram_transcriber bulk upload)
            # never get a local file_path (there's no server-side file to point
            # at), only a source_note describing where it came from — show that
            # as plain text instead of silently falling back to "—".
            source_text = None if source_url else b["source_note"]
            book_detail_url = url_for("book_detail", book_id=b["book_id"])
            example_rows = conn.execute(
                """SELECT e.example_id, e.example_title, e.example_text, e.page_range, s.section_number
                   FROM book_examples e LEFT JOIN book_sections s ON s.section_id = e.section_id
                   WHERE e.book_id = ? ORDER BY e.example_id""",
                (b["book_id"],),
            ).fetchall()
            examples = [
                {
                    "number": i,
                    "label": ex["example_title"] or _trim(ex["example_text"], 90),
                    "example_title": ex["example_title"],
                    "detail_url": f"{book_detail_url}#example-{ex['example_id']}",
                    "location": (
                        f"Section {ex['section_number']}, page {ex['page_range']}"
                        if ex["section_number"] is not None and ex["page_range"] else None
                    ),
                }
                for i, ex in enumerate(example_rows, start=1)
            ]
            rows.append({
                "kind": "book",
                "entity_id": b["book_id"],
                "media_type": "Book",
                "channel_name": b["author"] or "—",
                "profile_code": b["profile_code"],
                "unresolved_channel": False,
                "title": b["title"],
                "title_format": None,
                "word_count": b["word_count"],
                "duration_sec": None,
                "duration_label": f"{b['page_count']} pages" if b["page_count"] else None,
                "you_freq_per_100w": None,
                "readability_score": b["readability_score"],
                "source_url": source_url,
                "source_label": "original file ↗",
                "source_text": source_text,
                "status_pill": (
                    "needs_review" if b["classified_by"] == "needs_review"
                    else "pending" if b["classified_by"] in (None, "pending")
                    else None
                ),
                "ingested_at": b["ingested_at"],
                "detail_url": book_detail_url,
                "quickview_url": url_for("book_quickview_json", book_id=b["book_id"]),
                "examples": examples,
            })

    sort_keys = {
        "title": lambda r: (r["title"] or "").lower(),
        "media_type": lambda r: (r["media_type"] or "").lower(),
        "channel_name": lambda r: (r["channel_name"] or "").lower(),
        "word_count": lambda r: r["word_count"] if r["word_count"] is not None else -1,
        "duration_sec": lambda r: r["duration_sec"] if r["duration_sec"] is not None else -1,
        "title_format": lambda r: (r["title_format"] or "").lower(),
        "you_freq_per_100w": lambda r: r["you_freq_per_100w"] if r["you_freq_per_100w"] is not None else -1,
        "readability_score": lambda r: r["readability_score"] if r["readability_score"] is not None else -1,
        "source_url": lambda r: (r["source_url"] or "").lower(),
        "ingested_at": lambda r: r["ingested_at"] or "",
    }
    rows.sort(key=sort_keys.get(sort, sort_keys["ingested_at"]), reverse=(direction == "desc"))

    channels = conn.execute("SELECT * FROM channels ORDER BY channel_name").fetchall()
    title_formats = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT title_format FROM video_attributes WHERE title_format IS NOT NULL ORDER BY title_format"
        ).fetchall()
    ]
    # "tags" for the term/example filters below: every distinct term and example
    # title across both videos and books, so a tag clicked on any detail page
    # filters this list down to everything else that shares the same concept.
    all_terms = sorted({
        r[0] for r in conn.execute("SELECT DISTINCT term FROM video_terms WHERE term IS NOT NULL").fetchall()
    } | {
        r[0] for r in conn.execute("SELECT DISTINCT term FROM book_terms WHERE term IS NOT NULL").fetchall()
    }, key=str.lower)
    all_examples = sorted({
        r[0] for r in conn.execute(
            "SELECT DISTINCT example_title FROM video_examples WHERE example_title IS NOT NULL"
        ).fetchall()
    } | {
        r[0] for r in conn.execute(
            "SELECT DISTINCT example_title FROM book_examples WHERE example_title IS NOT NULL"
        ).fetchall()
    }, key=str.lower)
    total = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] + conn.execute(
        "SELECT COUNT(*) FROM books"
    ).fetchone()[0]

    n_books_only = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    n_book_examples = conn.execute("SELECT COUNT(*) FROM book_examples").fetchone()[0]
    n_books = n_books_only + n_book_examples
    n_book_authors = conn.execute("SELECT COUNT(DISTINCT author) FROM books WHERE author IS NOT NULL").fetchone()[0]
    n_short_videos = conn.execute("SELECT COUNT(*) FROM videos WHERE media_type = 'Instagram'").fetchone()[0]
    n_short_video_accounts = conn.execute(
        "SELECT COUNT(DISTINCT channel_id) FROM videos WHERE media_type = 'Instagram'"
    ).fetchone()[0]
    n_long_videos_only = conn.execute("SELECT COUNT(*) FROM videos WHERE media_type = 'YouTube'").fetchone()[0]
    n_long_video_examples = conn.execute(
        "SELECT COUNT(*) FROM video_examples e JOIN videos v ON v.video_id = e.video_id WHERE v.media_type = 'YouTube'"
    ).fetchone()[0]
    n_long_videos = n_long_videos_only + n_long_video_examples
    n_long_video_channels = conn.execute(
        "SELECT COUNT(DISTINCT channel_id) FROM videos WHERE media_type = 'YouTube'"
    ).fetchone()[0]
    n_news = 0  # news article ingestion not built yet
    # Matches the Dashboard's "Inputs ingested" count exactly: videos + books + book_examples,
    # and (like books) long-form videos here already bundles in their examples.
    n_all_inputs = n_short_videos + n_long_videos + n_books + n_news
    conn.close()

    return render_template(
        "inputs_list.html", active="inputs",
        rows=rows, channels=channels, title_formats=title_formats, total=total, kinds=INPUT_KINDS,
        all_terms=all_terms, all_examples=all_examples,
        n_books=n_books, n_books_only=n_books_only, n_book_examples=n_book_examples, n_book_authors=n_book_authors,
        n_short_videos=n_short_videos, n_short_video_accounts=n_short_video_accounts,
        n_long_videos=n_long_videos, n_long_videos_only=n_long_videos_only,
        n_long_video_examples=n_long_video_examples, n_long_video_channels=n_long_video_channels,
        n_news=n_news, n_all_inputs=n_all_inputs,
        filters={
            "kind": kind, "channel_id": channel_id, "q": q, "title_format": title_format,
            "term": term, "example": example,
            "word_min": word_min, "word_max": word_max, "sort": sort, "dir": direction,
        },
    )


@app.route("/inputs/<int:video_id>")
def input_detail(video_id):
    conn = get_conn()
    video = conn.execute(
        """SELECT v.*, c.channel_name, p.profile_code FROM videos v
           JOIN channels c ON c.channel_id = v.channel_id
           LEFT JOIN style_profiles p ON p.channel_id = c.channel_id
                AND (p.media_type IS NULL OR p.media_type != 'ProductionSpec')
           WHERE v.video_id = ?""",
        (video_id,),
    ).fetchone()
    if not video:
        conn.close()
        flash(f"No such input: {video_id}")
        return redirect(url_for("inputs_list"))

    attrs_row = conn.execute("SELECT * FROM video_attributes WHERE video_id = ?", (video_id,)).fetchone()

    # Long-form (YouTube) videos get a chapter breakdown, same pattern as
    # book_detail(); short-form (Instagram) videos have no video_sections
    # rows at all, so `chapters` stays empty and the template falls back to
    # the flat points/terms/examples lists below (unchanged from before).
    section_rows = conn.execute(
        "SELECT * FROM video_sections WHERE video_id = ? ORDER BY section_number, section_id", (video_id,)
    ).fetchall()
    chapters = []
    for s in section_rows:
        sec_points = conn.execute(
            "SELECT point_text FROM video_points WHERE section_id = ? ORDER BY point_id", (s["section_id"],)
        ).fetchall()
        sec_terms = conn.execute(
            "SELECT term_id, term, definition FROM video_terms WHERE section_id = ? ORDER BY term_id", (s["section_id"],)
        ).fetchall()
        sec_examples = conn.execute(
            "SELECT example_id, example_title, example_text, reinforces_point FROM video_examples "
            "WHERE section_id = ? ORDER BY example_id", (s["section_id"],)
        ).fetchall()
        chapters.append({
            "section": s,
            "topics": [t.strip() for t in s["topics"].split(",")] if s["topics"] else [],
            "points": sec_points, "terms": sec_terms, "examples": sec_examples,
        })

    points = conn.execute(
        "SELECT point_text FROM video_points WHERE video_id = ? AND section_id IS NULL ORDER BY point_id", (video_id,)
    ).fetchall()
    terms = conn.execute(
        "SELECT term, definition FROM video_terms WHERE video_id = ? AND section_id IS NULL ORDER BY term_id", (video_id,)
    ).fetchall()
    examples = conn.execute(
        "SELECT example_id, example_title, example_text, reinforces_point FROM video_examples "
        "WHERE video_id = ? AND section_id IS NULL ORDER BY example_id", (video_id,)
    ).fetchall()
    has_breakdown = bool(points or terms or examples or chapters)

    visuals = conn.execute(
        "SELECT visual_id, timestamp_sec, caption, recreated_svg, screenshot_captured FROM video_visuals "
        "WHERE video_id = ? ORDER BY timestamp_sec", (video_id,)
    ).fetchall()
    conn.close()

    attrs = dict(attrs_row) if attrs_row else {}
    sections = []
    for name, fields in ATTRIBUTE_SECTIONS:
        sections.append((name, [(f, attrs.get(f)) for f in fields]))

    return render_template(
        "input_detail.html", active="inputs", video=video, sections=sections, chapters=chapters,
        points=points, terms=terms, examples=examples, has_breakdown=has_breakdown, visuals=visuals,
        needs_review=attrs.get("classified_by") == "needs_review",
        classification_error=attrs.get("classification_error"),
    )


@app.route("/inputs/<int:video_id>/quickview.json")
def input_quickview_json(video_id):
    """Powers the Library page's row quick-view slide-over — the same
    attributes/chapters/terms/examples shown on input_detail.html, but
    flattened and fetched lazily (only when a row is actually opened)
    rather than embedded in every row of the table."""
    conn = get_conn()
    video = conn.execute("SELECT video_id FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    if not video:
        conn.close()
        return jsonify({"ok": False, "error": "no such video"}), 404

    attrs_row = conn.execute("SELECT * FROM video_attributes WHERE video_id = ?", (video_id,)).fetchone()
    attrs = dict(attrs_row) if attrs_row else {}
    attributes = []
    for name, fields in ATTRIBUTE_SECTIONS:
        section_fields = [{"label": f, "value": attrs[f]} for f in fields if attrs.get(f) not in (None, "")]
        if section_fields:
            attributes.append({"section": name, "fields": section_fields})

    section_rows = conn.execute(
        "SELECT * FROM video_sections WHERE video_id = ? ORDER BY section_number, section_id", (video_id,)
    ).fetchall()
    chapters, all_terms, all_examples = [], [], []
    for s in section_rows:
        sec_points = conn.execute(
            "SELECT point_text FROM video_points WHERE section_id = ? ORDER BY point_id", (s["section_id"],)
        ).fetchall()
        sec_terms = conn.execute(
            "SELECT term, definition FROM video_terms WHERE section_id = ? ORDER BY term_id", (s["section_id"],)
        ).fetchall()
        sec_examples = conn.execute(
            "SELECT example_title, example_text, reinforces_point FROM video_examples "
            "WHERE section_id = ? ORDER BY example_id", (s["section_id"],)
        ).fetchall()
        chapters.append({
            "title": f'{s["section_number"]}. {s["section_title"]}' if s["section_number"] is not None else s["section_title"],
            "summary": s["summary"],
            "topics": [t.strip() for t in s["topics"].split(",")] if s["topics"] else [],
            "points": [p["point_text"] for p in sec_points],
        })
        all_terms.extend({"term": t["term"], "definition": t["definition"]} for t in sec_terms)
        all_examples.extend(
            {"title": e["example_title"], "text": e["example_text"], "reinforces": e["reinforces_point"]}
            for e in sec_examples
        )

    flat_terms = conn.execute(
        "SELECT term, definition FROM video_terms WHERE video_id = ? AND section_id IS NULL ORDER BY term_id",
        (video_id,),
    ).fetchall()
    flat_examples = conn.execute(
        "SELECT example_title, example_text, reinforces_point FROM video_examples "
        "WHERE video_id = ? AND section_id IS NULL ORDER BY example_id", (video_id,)
    ).fetchall()
    conn.close()

    all_terms.extend({"term": t["term"], "definition": t["definition"]} for t in flat_terms)
    all_examples.extend(
        {"title": e["example_title"], "text": e["example_text"], "reinforces": e["reinforces_point"]}
        for e in flat_examples
    )

    return jsonify({"ok": True, "attributes": attributes, "chapters": chapters, "terms": all_terms, "examples": all_examples})


@app.route("/inputs/<int:video_id>/delete", methods=["POST"])
def input_delete(video_id):
    conn = get_conn()
    video = conn.execute(
        """SELECT v.*, c.channel_name FROM videos v
           JOIN channels c ON c.channel_id = v.channel_id
           WHERE v.video_id = ?""",
        (video_id,),
    ).fetchone()
    if not video:
        conn.close()
        flash(f"No such input: {video_id}")
        return redirect(url_for("inputs_list"))

    title, channel_id, channel_name = video["title"], video["channel_id"], video["channel_name"]

    # Avoid FK violations on tables that reference this video, then delete it.
    # The schema has no ON DELETE CASCADE anywhere and get_conn() sets
    # PRAGMA foreign_keys = ON, so EVERY referencing row must be cleared by
    # hand first. Previously only test_scores and video_attributes were, which
    # meant any video that had been through classification (and so had
    # breakdown children) raised IntegrityError -> unhandled 500, leaving the
    # connection open mid-transaction. Order matters: video_visuals/points/
    # terms/examples reference video_sections as well as videos, so they go
    # before video_sections.
    conn.execute("UPDATE test_scores SET match_video_id = NULL WHERE match_video_id = ?", (video_id,))
    conn.execute("UPDATE swipe_candidates SET source_video_id = NULL WHERE source_video_id = ?", (video_id,))
    conn.execute(
        "UPDATE production_spec_creations SET source_video_id = NULL WHERE source_video_id = ?", (video_id,)
    )
    for table in ("video_visuals", "video_points", "video_terms", "video_examples", "video_sections"):
        conn.execute(f"DELETE FROM {table} WHERE video_id = ?", (video_id,))
    conn.execute("DELETE FROM video_attributes WHERE video_id = ?", (video_id,))
    conn.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
    conn.commit()

    # rebuild that channel's profile fingerprint, if it has one, so this
    # video's numbers stop pulling on the averages
    profile = conn.execute(
        "SELECT profile_code, length_band, n_videos_analysed FROM style_profiles WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    conn.close()

    if profile:
        min_n = 10  # matches the threshold used everywhere else profiles are built in this project
        build_profile(channel_name, profile["profile_code"], profile["length_band"], min_n=min_n)
        flash(f'Deleted "{title}" and rebuilt {profile["profile_code"]}\'s fingerprint.', "success")
    else:
        flash(f'Deleted "{title}".', "success")

    return redirect(url_for("inputs_list"))


@app.route("/inputs/fix-channel", methods=["POST"])
def fix_unresolved_channel():
    new_name = request.form.get("channel_name", "").strip()
    if not new_name:
        flash("Enter a channel name.")
        return redirect(request.referrer or url_for("inputs_list"))
    if new_name == PLACEHOLDER_CHANNEL_NAME:
        flash(f'"{PLACEHOLDER_CHANNEL_NAME}" is the placeholder itself, not a real channel — enter the actual channel name.')
        return redirect(request.referrer or url_for("inputs_list"))

    conn = get_conn()
    bad = conn.execute("SELECT channel_id FROM channels WHERE channel_name = ?", (PLACEHOLDER_CHANNEL_NAME,)).fetchone()
    if not bad:
        conn.close()
        flash("No unresolved imports to fix.")
        return redirect(request.referrer or url_for("inputs_list"))
    bad_id = bad["channel_id"]

    target = conn.execute("SELECT channel_id FROM channels WHERE channel_name = ?", (new_name,)).fetchone()
    if target:
        target_id = target["channel_id"]
    else:
        cur = conn.execute("INSERT INTO channels (channel_name, platform) VALUES (?, 'Instagram')", (new_name,))
        target_id = cur.lastrowid

    n = conn.execute("SELECT COUNT(*) FROM videos WHERE channel_id = ?", (bad_id,)).fetchone()[0]
    conn.execute("UPDATE videos SET channel_id = ? WHERE channel_id = ?", (target_id, bad_id))

    # The placeholder channel can have its own auto-built style_profiles row
    # by now (auto_process_shortform_video.py profiles every channel it
    # classifies for, including an unresolved "Instagram Import" one) — that
    # profile is junk built from mislabeled data, never worth keeping, but
    # its foreign keys must be cleared before the channel row can be deleted.
    bad_profile = conn.execute("SELECT profile_id FROM style_profiles WHERE channel_id = ?", (bad_id,)).fetchone()
    if bad_profile:
        bad_profile_id = bad_profile["profile_id"]
        # score_transformation() writes one transform_scores row per confirmed
        # profile for EVERY transformation, so deleting only the rows pointing
        # at this profile leaves ~20 sibling rows still referencing the
        # transformations we're about to delete. That made the transformations
        # DELETE raise IntegrityError, and because the commit below was never
        # reached, SQLite rolled back the UPDATE videos SET channel_id above
        # too — the user got a 500 and the reassignment silently didn't happen.
        # So: clear transform_scores for the doomed transformations FIRST
        # (whichever profile they point at), then the transformations.
        conn.execute(
            "DELETE FROM transform_scores WHERE transformation_id IN "
            "(SELECT transformation_id FROM transformations WHERE target_profile_id = ?)",
            (bad_profile_id,),
        )
        # video_creations and production_spec_creations reference style_profiles
        # independently and would each block the DELETE FROM style_profiles on
        # their own; these are tracking rows, so null the link rather than
        # destroying the user's record of a planned output.
        conn.execute(
            "UPDATE video_creations SET target_profile_id = NULL WHERE target_profile_id = ?", (bad_profile_id,)
        )
        conn.execute(
            "UPDATE production_spec_creations SET style_profile_id = NULL WHERE style_profile_id = ?",
            (bad_profile_id,),
        )
        conn.execute(
            "UPDATE production_spec_creations SET production_profile_id = NULL WHERE production_profile_id = ?",
            (bad_profile_id,),
        )
        for table, column in [
            ("profile_fingerprint_numeric", "profile_id"),
            ("profile_fingerprint_categorical", "profile_id"),
            ("profile_style_card", "profile_id"),
            ("profile_fingerprint_snapshots", "profile_id"),
            ("test_scores", "profile_id"),
            ("transform_scores", "profile_id"),
            ("transformations", "target_profile_id"),
        ]:
            conn.execute(f"DELETE FROM {table} WHERE {column} = ?", (bad_profile_id,))
        conn.execute(
            "DELETE FROM style_profile_hybrid_sources WHERE profile_id = ? OR source_profile_id = ?",
            (bad_profile_id, bad_profile_id),
        )
        conn.execute("DELETE FROM style_profiles WHERE profile_id = ?", (bad_profile_id,))

    conn.execute("DELETE FROM channels WHERE channel_id = ?", (bad_id,))
    conn.commit()

    profile = conn.execute(
        "SELECT profile_code, length_band FROM style_profiles WHERE channel_id = ?", (target_id,)
    ).fetchone()
    conn.close()

    if profile:
        build_profile(new_name, profile["profile_code"], profile["length_band"], min_n=10)
        flash(f'Reassigned {n} video(s) to "{new_name}" and rebuilt {profile["profile_code"]}\'s fingerprint.', "success")
    else:
        flash(f'Reassigned {n} video(s) to "{new_name}". No style profile exists for this channel yet.', "success")

    return redirect(request.referrer or url_for("inputs_list"))


CREATIONS_SORT_COLUMNS = {
    "generated_at": "t.generated_at",
    "delta": "ts.score_delta",
    "post_score": "ts.total_score",
    "title": "t.generated_title",
}


@app.route("/creations")
def creations_list():
    profile_code = request.args.get("profile", "").strip()
    q = request.args.get("q", "").strip()
    generated_by = request.args.get("generated_by", "").strip()
    delta_min = request.args.get("delta_min", type=float)
    sort = request.args.get("sort", "generated_at")
    direction = "asc" if request.args.get("dir") == "asc" else "desc"

    where, params = [], []
    if profile_code:
        where.append("p.profile_code = ?")
        params.append(profile_code)
    if q:
        where.append("(t.generated_title LIKE ? OR ti.source_label LIKE ?)")
        params.append(f"%{q}%")
        params.append(f"%{q}%")
    if generated_by:
        where.append("t.generated_by = ?")
        params.append(generated_by)
    if delta_min is not None:
        where.append("ts.score_delta >= ?")
        params.append(delta_min)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    sort_col = CREATIONS_SORT_COLUMNS.get(sort, "t.generated_at")

    conn = get_conn()
    rows = conn.execute(
        f"""SELECT t.transformation_id, t.generated_title, t.generated_by, t.generated_at,
                   p.profile_code, ti.test_id, ti.source_label,
                   ts.total_score, ts.pre_score_same_profile, ts.score_delta
            FROM transformations t
            JOIN style_profiles p ON p.profile_id = t.target_profile_id
            JOIN test_inputs ti ON ti.test_id = t.test_id
            LEFT JOIN transform_scores ts ON ts.transformation_id = t.transformation_id AND ts.is_target_profile = 1
            {where_sql}
            ORDER BY {sort_col} {'ASC' if direction == 'asc' else 'DESC'}""",
        params,
    ).fetchall()

    generated_by_values = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT generated_by FROM transformations WHERE generated_by IS NOT NULL ORDER BY generated_by"
        ).fetchall()
    ]
    total = conn.execute("SELECT COUNT(*) FROM transformations").fetchone()[0]
    conn.close()

    overall_valuation, n_valued, n_total_scored = _all_creations_valuation()

    return render_template(
        "creations_list.html", active="creations",
        rows=rows, profiles=_profiles(), generated_by_values=generated_by_values, total=total,
        overall_valuation=overall_valuation, n_valued=n_valued, n_total_scored=n_total_scored,
        filters={
            "profile": profile_code, "q": q, "generated_by": generated_by,
            "delta_min": delta_min, "sort": sort, "dir": direction,
        },
    )


VALUATION_COLORS = [
    "#6ea8fe", "#4ade80", "#facc15", "#f87171", "#c084fc",
    "#2dd4bf", "#fb923c", "#f472b6", "#a3e635",
]


def _score_field_to_macro():
    """Maps every scoreable attribute name to its CROSS-MEDIA macro category
    (SHARED_ATTRIBUTE_SECTIONS / SHARED_BOOK_ATTRIBUTE_SECTIONS) — used by
    the valuation pie and "Attribute category fit" rollups, both of which
    are meant to be eyeballed against the equivalent view on a book's page
    or a video's page. This intentionally does NOT use the native
    ATTRIBUTE_SECTIONS/BOOK_ATTRIBUTE_SECTIONS (same-media only)."""
    field_to_macro = {}
    for section_name, fields in SHARED_ATTRIBUTE_SECTIONS:
        for f in fields:
            field_to_macro[f] = section_name
    for section_name, fields in SHARED_BOOK_ATTRIBUTE_SECTIONS:
        for f in fields:
            field_to_macro.setdefault(f, section_name)
    return field_to_macro


def _cross_media_shared_fields():
    """The field NAMES scored the same way on a Short and on a book (same
    controlled vocabulary / numeric meaning), so a value on one side is
    directly comparable to a value on the other — score_engine's
    CROSS_MEDIA_SHARED_FIELDS is the single source of truth (it's also what
    score_against_profiles() actually scores a video creation against a
    book profile on), reused here so the pie can never drift from what was
    actually scored. Strictly smaller than _score_field_to_macro()'s
    domain, which also covers each medium's native-only fields."""
    return set(CROSS_MEDIA_SHARED_FIELDS)


def _macro_weights_for_breakdown(breakdown, weights, field_to_macro):
    """Returns {macro: weight_sum} for one creation's score_breakdown dict
    ({attribute: subscore}) — each attribute present contributes its
    scoring_weights weight to whichever macro it belongs to. Returns None
    for the content-match special case (no per-attribute entries to value)."""
    if not breakdown or "content_match" in breakdown or "cross_media" in breakdown:
        return None
    macro_weight = {}
    for attr in breakdown:
        macro = field_to_macro.get(attr, "Other")
        w = weights.get(attr, 1.0)
        macro_weight[macro] = macro_weight.get(macro, 0.0) + w
    return macro_weight


def _valuation_slices(macro_weight):
    """Turns a {macro: weight_sum} dict into sorted pie slices (pct, color,
    conic-gradient start/end) — a valuation, not a performance read: each
    slice is that macro's share of the total scoring weight actually
    available, regardless of how well any attribute in it scored."""
    total_weight = sum(macro_weight.values()) if macro_weight else 0
    if not total_weight:
        return None
    slices = sorted(
        ({"macro": m, "pct": round(w / total_weight * 100, 1)} for m, w in macro_weight.items()),
        key=lambda s: s["pct"], reverse=True,
    )
    cumulative = 0.0
    for i, s in enumerate(slices):
        s["color"] = VALUATION_COLORS[i % len(VALUATION_COLORS)]
        s["start_pct"] = round(cumulative, 2)
        cumulative += s["pct"]
        s["end_pct"] = round(cumulative, 2)
    return slices


def _macro_subscore_from_breakdown(breakdown, field_to_macro):
    """Like _macro_weights_for_breakdown, but averages the SUBSCORE (0-100,
    'how well does this fit the profile') per macro instead of summing
    scoring weight ('how much of the rubric is represented'). This is the
    number that's actually comparable across media: a book's macro fields
    and a video's macro fields mean completely different things field-by-
    field, but 'average fit score for this macro' is a 0-100 number either
    way. Works on any breakdown shaped {attribute: subscore}, whether it
    came from score_against_profiles() (video) or
    score_book_against_profiles() (book)."""
    if not breakdown or "content_match" in breakdown or "cross_media" in breakdown:
        return None
    macro_scores = {}
    for attr, sub in breakdown.items():
        if not isinstance(sub, (int, float)):
            continue
        macro = field_to_macro.get(attr, "Other")
        macro_scores.setdefault(macro, []).append(sub)
    if not macro_scores:
        return None
    return {m: round(sum(vals) / len(vals), 1) for m, vals in macro_scores.items()}


def _shared_field_macro_counts():
    """Fixed field-COUNT share of the full CROSS_MEDIA_SHARED_FIELDS list per
    macro category — e.g. Tone & Voice has 7 of the 25 shared fields, so it's
    always 28% of this pie, regardless of how many of those 7 happen to be
    populated for any one creation. This is what makes the pie represent
    "share of influence from the [shared-attribute] list", not just whatever
    thin subset a particular creation happened to get classified with."""
    field_to_macro = _score_field_to_macro()
    macro_counts = {}
    for f in CROSS_MEDIA_SHARED_FIELDS:
        macro = field_to_macro.get(f, "Other")
        macro_counts[macro] = macro_counts.get(macro, 0) + 1
    return macro_counts


def _creation_shared_valuation(breakdown):
    """Pie slices sized by the FIXED share of the 25-field cross-media
    taxonomy each macro occupies (_shared_field_macro_counts), annotated
    with how many of that macro's shared fields are actually populated for
    THIS creation — so "only 2 fields happened to get scored" shows up as
    "2 of 2 scored" inside a slice that's still correctly sized at its true
    8% share of the full list, instead of ballooning to look like 100% of
    the comparison. Returns (slices_or_None, n_shared, n_shared_total)."""
    macro_field_totals = _shared_field_macro_counts()
    slices = _valuation_slices(macro_field_totals)

    has_data = breakdown and "content_match" not in breakdown and "cross_media" not in breakdown
    shared_fields = _cross_media_shared_fields()
    scored_shared = {k for k in breakdown if k in shared_fields} if has_data else set()
    n_shared = len(scored_shared)
    n_shared_total = len(CROSS_MEDIA_SHARED_FIELDS)

    if slices:
        field_to_macro = _score_field_to_macro()
        macro_scored_counts = {}
        for f in scored_shared:
            m = field_to_macro.get(f, "Other")
            macro_scored_counts[m] = macro_scored_counts.get(m, 0) + 1
        for s in slices:
            s["field_total"] = macro_field_totals.get(s["macro"], 0)
            s["field_scored"] = macro_scored_counts.get(s["macro"], 0)

    return (slices if n_shared else None), n_shared, n_shared_total


def _all_creations_valuation():
    """Aggregates every creation's target-profile score_breakdown into one
    combined valuation — the same macro-weight-share logic as a single
    creation, but summed across every creation that has a valuable
    breakdown, so a macro that shows up in more creations naturally carries
    more combined weight. Returns (slices_or_None, n_valued, n_total)."""
    conn = get_conn()
    weights = {r["attribute"]: r["weight"] for r in conn.execute("SELECT attribute, weight FROM scoring_weights")}
    breakdown_rows = conn.execute(
        """SELECT ts.score_breakdown FROM transform_scores ts
           JOIN transformations t ON t.transformation_id = ts.transformation_id
           WHERE ts.profile_id = t.target_profile_id AND ts.score_breakdown IS NOT NULL"""
    ).fetchall()
    conn.close()

    field_to_macro = _score_field_to_macro()
    combined = {}
    n_valued = 0
    for row in breakdown_rows:
        breakdown = json.loads(row["score_breakdown"])
        macro_weight = _macro_weights_for_breakdown(breakdown, weights, field_to_macro)
        if macro_weight is None:
            continue
        n_valued += 1
        for m, w in macro_weight.items():
            combined[m] = combined.get(m, 0.0) + w

    return _valuation_slices(combined), n_valued, len(breakdown_rows)


@app.route("/creations/<int:transformation_id>")
def creation_detail(transformation_id):
    conn = get_conn()
    t = conn.execute(
        """SELECT t.*, p.profile_code, ti.source_label, ti.raw_text, ti.test_id
           FROM transformations t
           JOIN style_profiles p ON p.profile_id = t.target_profile_id
           JOIN test_inputs ti ON ti.test_id = t.test_id
           WHERE t.transformation_id = ?""",
        (transformation_id,),
    ).fetchone()
    if not t:
        conn.close()
        flash(f"No such creation: {transformation_id}")
        return redirect(url_for("creations_list"))

    scores = conn.execute(
        """SELECT ts.*, p.profile_code FROM transform_scores ts
           JOIN style_profiles p ON p.profile_id = ts.profile_id
           WHERE ts.transformation_id = ? ORDER BY ts.rank""",
        (transformation_id,),
    ).fetchall()
    conn.close()

    target_score = next((s for s in scores if s["profile_id"] == t["target_profile_id"]), None)
    breakdown = json.loads(target_score["score_breakdown"]) if target_score and target_score["score_breakdown"] else {}
    macro_fit = _macro_subscore_from_breakdown(breakdown, _score_field_to_macro())
    shared_valuation, n_shared, n_shared_total = _creation_shared_valuation(breakdown)

    return render_template(
        "creation_detail.html", active="creations",
        t=t, scores=scores, macro_fit=macro_fit,
        shared_valuation=shared_valuation, n_shared=n_shared, n_shared_total=n_shared_total,
    )


@app.route("/score", methods=["GET"])
def score_form():
    return render_template("score_form.html", active="score")


@app.route("/score", methods=["POST"])
def score_submit():
    text = request.form.get("text", "").strip()
    if not text:
        flash("Paste some text to score first.")
        return redirect(url_for("score_form"))

    result = rate_input(
        text,
        title=request.form.get("title", ""),
        source_label=request.form.get("label", ""),
        input_type=request.form.get("input_type", "video_script"),
    )
    return render_template("score_result.html", active="score", result=result)


PROFILES_SORT_KEYS = {
    "media_type": lambda p: (p.get("media_type") or "").lower(),
    "profile_code": lambda p: (p.get("profile_code") or "").lower(),
    "channel_name": lambda p: (p.get("channel_name") or "").lower(),
    "subject": lambda p: (p.get("subject") or "").lower(),
    "length_band": lambda p: (p.get("length_band") or "").lower(),
    "n_videos_analysed": lambda p: p.get("n_videos_analysed") or 0,
    "status": lambda p: (p.get("status") or "").lower(),
    "concepts_added": lambda p: p.get("concepts_added") or False,
}

# Book profiles whose author has a book in this set get the "concepts/examples
# added" pilot breakdown (see profile_detail's book_examples/book_terms section);
# every Instagram profile has it via the per-video breakdown instead.
BOOK_BREAKDOWN_PILOT_BOOK_IDS = {5}


@app.route("/insights")
def insights_overview():
    conn = get_conn()
    profiles = _profiles()
    n_profiles = len(profiles)
    n_subjects_covered = len({p["subject"] for p in profiles if p["subject"] in SUBJECTS})
    n_tests = conn.execute("SELECT COUNT(*) FROM test_inputs").fetchone()[0]
    n_creations = conn.execute("SELECT COUNT(*) FROM transformations").fetchone()[0]

    subject_groups = [g for g in _subject_groups(profiles) if g["count"] > 0]
    attribute_cards = _attribute_cards(conn)
    n_attribute_cats = len([c for c in attribute_cards if c["implemented"]])

    n_terms = conn.execute(
        """SELECT COUNT(DISTINCT term COLLATE NOCASE) FROM (
               SELECT term FROM video_terms WHERE term IS NOT NULL AND term != ''
               UNION ALL SELECT term FROM book_terms WHERE term IS NOT NULL AND term != ''
           )"""
    ).fetchone()[0]
    n_examples = conn.execute(
        """SELECT COUNT(DISTINCT example_title COLLATE NOCASE) FROM (
               SELECT example_title FROM video_examples WHERE example_title IS NOT NULL AND example_title != ''
               UNION ALL SELECT example_title FROM book_examples WHERE example_title IS NOT NULL AND example_title != ''
           )"""
    ).fetchone()[0]

    media_counts = Counter((p["media_type"] or "Unknown") for p in profiles)
    media_breakdown = []
    for key, label, endpoint in [
        ("Book", "Book", "books_list"),
        ("Instagram", "Short Videos, Instagram", "channels_list"),
        ("YouTube", "Long Videos, Youtube", None),
        ("News", "News", None),
    ]:
        n = media_counts.get(key, 0)
        media_breakdown.append({
            "label": label,
            "count": n,
            "pct": round(n / n_profiles * 100) if n_profiles else 0,
            "url": url_for(endpoint) if endpoint else None,
        })
    status_counts = Counter(p["status"] for p in profiles)
    n_confirmed = status_counts.get("confirmed", 0)
    n_draft = status_counts.get("draft", 0)

    recent_test_rows = conn.execute(
        "SELECT * FROM test_inputs ORDER BY test_id DESC LIMIT 5"
    ).fetchall()
    recent_tests = []
    for t in recent_test_rows:
        best = conn.execute(
            """SELECT s.total_score, p.profile_code FROM test_scores s
               JOIN style_profiles p ON p.profile_id = s.profile_id
               WHERE s.test_id = ? ORDER BY s.rank ASC LIMIT 1""",
            (t["test_id"],),
        ).fetchone()
        d = dict(t)
        d["best_score"] = best["total_score"] if best else None
        d["best_profile"] = best["profile_code"] if best else None
        recent_tests.append(d)
    conn.close()

    return render_template(
        "insights_overview.html", active="insights",
        n_profiles=n_profiles, n_subjects_covered=n_subjects_covered, n_attribute_cats=n_attribute_cats,
        n_terms=n_terms, n_examples=n_examples,
        n_tests=n_tests, n_creations=n_creations, n_confirmed=n_confirmed, n_draft=n_draft,
        media_breakdown=media_breakdown, subject_groups=subject_groups, attribute_cards=attribute_cards,
        recent_tests=recent_tests,
    )


@app.route("/profiles")
def profiles_list():
    sort = request.args.get("sort", "profile_code")
    direction = "desc" if request.args.get("dir") == "desc" else "asc"
    profiles = _profiles()

    conn = get_conn()
    placeholders = ",".join("?" * len(BOOK_BREAKDOWN_PILOT_BOOK_IDS))
    pilot_authors = {
        r["author"] for r in conn.execute(
            f"SELECT DISTINCT author FROM books WHERE book_id IN ({placeholders})",
            list(BOOK_BREAKDOWN_PILOT_BOOK_IDS),
        ).fetchall()
    }
    conn.close()
    for p in profiles:
        p["concepts_added"] = p["media_type"] == "Instagram" or p["channel_name"] in pilot_authors

    profiles.sort(key=PROFILES_SORT_KEYS.get(sort, PROFILES_SORT_KEYS["profile_code"]), reverse=(direction == "desc"))
    return render_template(
        "profiles_list.html", active="profiles", profiles=profiles,
        filters={"sort": sort, "dir": direction},
    )


def _subject_groups(profiles):
    groups = []
    for subject in SUBJECTS:
        groups.append({
            "name": subject,
            "icon": SUBJECT_ICONS.get(subject, "🏷️"),
            "count": len([p for p in profiles if p["subject"] == subject]),
        })
    unassigned = [p for p in profiles if not p["subject"] or p["subject"] not in SUBJECTS]
    if unassigned:
        groups.append({"name": "Unassigned", "icon": SUBJECT_ICONS["Unassigned"], "count": len(unassigned)})
    return groups


@app.route("/subjects")
def subjects_page():
    profiles = _profiles()
    groups = _subject_groups(profiles)
    return render_template("subjects.html", active="subjects", groups=groups)


@app.route("/subjects/<subject>")
def subject_detail(subject):
    profiles = _profiles()
    if subject == "Unassigned":
        matched = [p for p in profiles if not p["subject"] or p["subject"] not in SUBJECTS]
    else:
        matched = [p for p in profiles if p["subject"] == subject]
    return render_template("subject_detail.html", active="subjects", subject=subject, profiles=matched)


# Books' NATIVE attribute taxonomy — its own macro-sized sections (no extra
# rollup layer needed, unlike video's ATTRIBUTE_SECTIONS -> MACRO_GROUPS).
# This is the medium-specific view, used for same-media comparison and the
# book's own attribute-catalog display. See SHARED_BOOK_ATTRIBUTE_SECTIONS
# below for the cross-media-comparable taxonomy.
BOOK_ATTRIBUTE_SECTIONS = [
    ("Thesis & Purpose", ["thesis_statement", "primary_goal", "value_promise"]),
    ("Evidence & Authority", ["primary_evidence_type", "secondary_evidence_types", "citation_density"]),
    ("Tone & Voice", ["tone", "rhetorical_appeal_balance", "emotional_register", "narrative_voice", "polemical_tone",
                       "narrative_presence", "vulnerability_depth", "condescension_vs_empowerment"]),
    ("Structure & Organization", ["structure_style", "uses_visual_aids", "subheading_density", "thesis_consistency",
                                   "curiosity_loop"]),
    ("Target Audience", ["target_audience", "vocabulary_complexity", "jargon_accessibility", "relatability_factor",
                          "identity_framing"]),
    ("Bias & Assumptions", ["bias_assumptions", "counter_argument_engagement", "ideological_positioning",
                             "contrarian_positioning"]),
    ("Argument & Reasoning", ["argument_architecture", "prescriptiveness", "claim_falsifiability", "narrative_density", "argumentative_density", "abstraction_concreteness_balance", "hedging_vs_assertion", "rhetorical_questioning", "information_density"]),
    ("Context & Positioning", ["temporal_orientation", "interdisciplinary_fields", "named_frameworks_coined", "comparative_positioning"]),
    ("Style & Craft", ["diction", "syntax_pattern", "pacing", "sensory_language_density", "narrative_distance", "figurative_language_density", "prose_rhythm", "noun_verb_ratio_style", "cognitive_metaphor_domain", "adjective_intensity", "punctuation_delivery", "rhythmic_repetition"]),
    ("Readability", ["avg_sentence_len", "avg_syllables_per_word", "readability_score"]),
]

# Same 14 shared macro names as SHARED_ATTRIBUTE_SECTIONS above. Books' own
# rubric already matched 10 of these almost verbatim; this just splits
# "diction" and "syntax_pattern" out of the native Style & Craft grouping
# into their own Diction bucket, and pulls rhetorical_appeal_balance into
# Rhetoric — moves that make books line up with video's Diction/Rhetoric
# sections directly. Content Taxonomy and Delivery have no book-rubric
# equivalent (books have no subject-taxonomy or CTA/delivery-mechanic
# fields), so they're empty for books — an honest gap, not a bug: those two
# buckets stay video-only. Used ONLY for cross-media comparison, never for
# same-media display (see BOOK_ATTRIBUTE_SECTIONS above for that).
SHARED_BOOK_ATTRIBUTE_SECTIONS = [
    ("Diction", ["diction", "syntax_pattern", "adjective_intensity", "punctuation_delivery"]),
    ("Rhetoric", ["rhetorical_appeal_balance", "rhythmic_repetition"]),
    ("Content Taxonomy", ["information_density"]),
    ("Delivery", []),
    ("Thesis & Purpose", ["thesis_statement", "primary_goal", "value_promise"]),
    ("Evidence & Authority", ["primary_evidence_type", "secondary_evidence_types", "citation_density"]),
    ("Tone & Voice", ["tone", "emotional_register", "narrative_voice", "polemical_tone", "narrative_presence",
                       "vulnerability_depth", "condescension_vs_empowerment"]),
    ("Structure & Organization", ["structure_style", "uses_visual_aids", "subheading_density", "thesis_consistency",
                                   "curiosity_loop"]),
    ("Target Audience", ["target_audience", "vocabulary_complexity", "jargon_accessibility", "relatability_factor",
                          "identity_framing"]),
    ("Bias & Assumptions", ["bias_assumptions", "counter_argument_engagement", "ideological_positioning",
                             "contrarian_positioning"]),
    ("Argument & Reasoning", ["argument_architecture", "prescriptiveness", "claim_falsifiability", "narrative_density", "argumentative_density", "abstraction_concreteness_balance", "hedging_vs_assertion", "rhetorical_questioning"]),
    ("Context & Positioning", ["temporal_orientation", "interdisciplinary_fields", "named_frameworks_coined", "comparative_positioning"]),
    ("Style & Craft", ["pacing", "sensory_language_density", "narrative_distance", "figurative_language_density", "prose_rhythm", "noun_verb_ratio_style", "cognitive_metaphor_domain"]),
    ("Readability", ["avg_sentence_len", "avg_syllables_per_word", "readability_score"]),
]

BOOK_FIELD_LABELS = {
    "thesis_statement": "Thesis statement", "primary_goal": "Primary goal",
    "primary_evidence_type": "Primary evidence type", "secondary_evidence_types": "Secondary evidence types",
    "citation_density": "Citation density", "tone": "Tone", "rhetorical_appeal_balance": "Rhetorical appeal balance",
    "emotional_register": "Emotional register", "narrative_voice": "Narrative voice",
    "polemical_tone": "Polemical tone", "narrative_presence": "Narrative presence (the \"I\")",
    "structure_style": "Structure style", "uses_visual_aids": "Uses visual aids",
    "subheading_density": "Subheading density", "thesis_consistency": "Thesis consistency",
    "target_audience": "Target audience", "vocabulary_complexity": "Vocabulary complexity",
    "jargon_accessibility": "Jargon density & accessibility",
    "bias_assumptions": "Bias & assumptions", "counter_argument_engagement": "Counter-argument engagement",
    "ideological_positioning": "Ideological positioning", "argument_architecture": "Argument architecture",
    "prescriptiveness": "Prescriptiveness", "claim_falsifiability": "Claim falsifiability",
    "narrative_density": "Narrative density", "argumentative_density": "Argumentative density",
    "abstraction_concreteness_balance": "Abstraction vs. concreteness",
    "hedging_vs_assertion": "Hedging vs. assertion", "rhetorical_questioning": "Rhetorical questioning",
    "temporal_orientation": "Temporal orientation",
    "interdisciplinary_fields": "Interdisciplinary fields", "named_frameworks_coined": "Named frameworks coined",
    "comparative_positioning": "Comparative positioning",
    "diction": "Diction", "syntax_pattern": "Syntax", "pacing": "Pacing",
    "sensory_language_density": "Sensory language", "narrative_distance": "Narrative distance",
    "figurative_language_density": "Figurative language", "prose_rhythm": "Rhythm",
    "noun_verb_ratio_style": "Noun-to-verb ratio", "cognitive_metaphor_domain": "Cognitive metaphors",
    "avg_sentence_len": "Average sentence length", "avg_syllables_per_word": "Average syllables per word",
    "readability_score": "Readability score (Flesch-Kincaid grade)",
    "value_promise": "What the book implicitly promises the reader for sticking with it",
    "information_density": "How fact-packed versus loose/lifestyle-focused the book is",
    "curiosity_loop": "Opens a question/tease resolved only later",
    "relatability_factor": "Kind of relatable hook used",
    "identity_framing": "Invites the reader to see themselves in it",
    "contrarian_positioning": "How much it positions against consensus",
    "adjective_intensity": "Extreme vs. neutral modifiers",
    "punctuation_delivery": "Qualitative punctuation pattern",
    "rhythmic_repetition": "Uses anaphora (repeated phrase starter)",
    "vulnerability_depth": "How openly mistakes/failures are shared",
    "condescension_vs_empowerment": "Speaks down to vs. with the reader",
}


@app.route("/channels")
def channels_list():
    platform = request.args.get("platform", "Instagram")
    conn = get_conn()
    rows = conn.execute(
        # Two separate pipelines share the channels table: the LIBRARY
        # (writing style — videos/books, profile codes A.*/BK.*) and
        # PRODUCTION SPEC (shot pacing and framing, PS.*). A creator can
        # appear in both, so joining style_profiles on channel_id alone
        # pulled a channel's PS.* profile into this library page — showing
        # e.g. "sovra.money · PS.2 · 0 videos · Not analysed", which reads
        # as a broken library channel when it is really a perfectly healthy
        # production-spec one that simply has no library videos. PS profiles
        # belong on /production/profiles; excluded by name rather than
        # whitelisting library types, so a future library media_type isn't
        # silently dropped from this page.
        #
        # The EXISTS also drops channels with no library videos at all —
        # this page's own description is "every creator with ingested
        # videos", and a production-only creator has none.
        """SELECT c.channel_id, c.channel_name, p.profile_code, p.subject, p.status, p.n_videos_analysed,
                  (SELECT COUNT(*) FROM videos v WHERE v.channel_id = c.channel_id) AS n_videos
           FROM channels c
           LEFT JOIN style_profiles p ON p.channel_id = c.channel_id
                AND (p.media_type IS NULL OR p.media_type != 'ProductionSpec')
           WHERE c.platform = ?
             AND EXISTS (SELECT 1 FROM videos v WHERE v.channel_id = c.channel_id)
           ORDER BY c.channel_name""",
        (platform,),
    ).fetchall()
    channels = []
    for c in rows:
        video_rows = [
            {**dict(v), "duration_label": _format_duration(v["duration_sec"])}
            for v in conn.execute(
                """SELECT v.video_id, v.title, v.ingested_at, v.duration_sec, v.url, a.word_count
                   FROM videos v LEFT JOIN video_attributes a ON a.video_id = v.video_id
                   WHERE v.channel_id = ? ORDER BY v.ingested_at DESC""",
                (c["channel_id"],),
            ).fetchall()
        ]
        n_analysed = conn.execute(
            """SELECT COUNT(DISTINCT v.video_id) FROM videos v
               WHERE v.channel_id = ? AND v.video_id IN (
                   SELECT video_id FROM video_points
                   UNION SELECT video_id FROM video_terms
                   UNION SELECT video_id FROM video_examples
               )""",
            (c["channel_id"],),
        ).fetchone()[0]
        is_analysed = c["n_videos"] > 0 and n_analysed == c["n_videos"]
        analysed_pct = round(n_analysed / c["n_videos"] * 100) if c["n_videos"] else 0
        channels.append({
            **dict(c), "videos": video_rows, "n_analysed": n_analysed,
            "is_analysed": is_analysed, "analysed_pct": analysed_pct,
        })
    conn.close()
    active = "channels" if platform == "Instagram" else "channels-youtube"
    return render_template("channels_list.html", active=active, channels=channels, platform=platform)


@app.route("/api/ingest/book", methods=["POST"])
def api_ingest_book():
    """Lets the Instagram Bulk Transcriber (running locally, on a different
    machine from this live service) hand a book straight to the live engine —
    no local ingest + manual DB export/import round trip. Ingestion itself is
    synchronous (just an INSERT), but classification is kicked off as a
    detached subprocess (NOT an in-process thread) since reading a full book
    via the Anthropic API can take several minutes: gunicorn's gthread
    workers get recycled by the arbiter if they look unresponsive, and a
    long GIL-bound background thread inside a worker can trigger exactly
    that — which silently kills an in-process thread along with the worker,
    with no error ever recorded. A separate process has its own PID, so it
    survives that worker being recycled; only a full container restart
    would interrupt it. The caller gets book_id back immediately and the
    book shows as 'pending' until that subprocess finishes."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    title = (data.get("title") or "").strip()
    if not text or not title:
        return jsonify({"ok": False, "error": "Both 'text' and 'title' are required."}), 400

    book_id = ingest_book_text(
        full_text=text,
        title=title,
        author=data.get("author") or None,
        subject=data.get("subject") or None,
        year=data.get("year") or None,
        source_note=data.get("source_note") or None,
    )

    # stdout/stderr deliberately inherited (not DEVNULL'd) so classification
    # errors/tracebacks still show up in Render's log stream even though the
    # process is detached from this request.
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_process_book.py")
    subprocess.Popen([sys.executable, script_path, "--book_id", str(book_id)], start_new_session=True)

    return jsonify({"ok": True, "book_id": book_id, "status": "ingested, classifying in the background"})


@app.route("/api/ingest/video", methods=["POST"])
def api_ingest_video():
    """Video counterpart to /api/ingest/book — lets the Instagram Bulk
    Transcriber hand one video (long-form YouTube or short-form Instagram)
    straight to the live engine. Ingestion is synchronous (an INSERT +
    local feature extraction), but classification (and, for YouTube,
    breakdown/visual-detection) runs in a detached subprocess, same
    rationale as api_ingest_book: a multi-minute LLM call inside an
    in-process thread risks gunicorn's arbiter recycling the worker
    mid-call, silently killing the thread with it.

    Which classification script runs depends on platform: YouTube's
    auto_process_video.py does one combined call covering the Class
    rubric + chapter breakdown + visual-chart detection (long-form only
    has all three); everything else runs
    auto_process_shortform_video.py's single Class-rubric call — a Reel
    is too short to have chapters or on-screen data visuals, and the
    long-form combined prompt is worded specifically for a long-form
    YouTube video, so it isn't reused here."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    script = (data.get("script") or "").strip()
    channel = (data.get("channel") or "").strip()
    if not title or not script or not channel:
        return jsonify({"ok": False, "error": "'title', 'script', and 'channel' are required."}), 400

    # Infer the platform from the URL when the caller doesn't state one.
    # This used to default to "YouTube" outright, so any import that omitted
    # platform became a YouTube video — which then routed a 60-second
    # Instagram reel into the LONG-FORM classification pipeline
    # (auto_process_video.py's single 32k-token combined call) instead of the
    # cheaper short-form one, and grouped it under the wrong length band.
    # Nine reels are currently mislabelled this way.
    platform = (data.get("platform") or "").strip() or _infer_platform(data.get("url"))
    duration_sec = data.get("duration_sec") or None

    # Resolve the channel BEFORE ingesting. get_or_create_channel matches on
    # an exact string and silently INSERTs anything else, so "kylascan" /
    # "Kylascan" / "kyla scan" each become a separate channel with its own
    # profile and its own fingerprint, and nobody is told. That is how a
    # philedwardsinc video ended up filed under kylascan — and because the
    # channel decides the profile, and the profile carries both the style
    # code AND the subject, one wrong channel makes both wrong at once.
    channel_status, channel_suggestion = _resolve_import_channel(channel)

    video_id = ingest_video_row(
        title=title,
        script=script,
        channel=channel,
        platform=platform,
        url=data.get("url") or None,
        duration_sec=duration_sec,
        posted_at=data.get("posted_at") or None,
        chapter_count=data.get("chapter_count"),
        timed_transcript=data.get("timed_transcript") or None,
        length_band=data.get("length_band") or "C",
    )

    # --- gatekeeper: cheap, no-LLM checks before an expensive Claude call fires.
    # See gatekeeper.py's module docstring for why this exists (Aug 2026 cost
    # investigation) and what each check does.
    conn = get_conn()
    row = conn.execute("SELECT classified_by FROM video_attributes WHERE video_id = ?", (video_id,)).fetchone()
    conn.close()
    if bool(row) and row["classified_by"] == "claude":
        # content_hash-deduped to an existing, ALREADY-classified video —
        # ingest_video_row() already guaranteed no duplicate DB row; this
        # stops a re-submitted job (e.g. Bulk Transcriber retrying after a
        # partial failure) from also re-running the expensive classification
        # subprocess on a video that's already done.
        return jsonify({"ok": True, "video_id": video_id, "status": "already classified, skipped re-processing"})

    # Unconfirmed channel -> hold BEFORE classifying. Classifying into the
    # wrong profile costs a real Claude call AND pollutes that profile's
    # averages, so the fix afterwards is "reassign + reclassify + rebuild two
    # profiles" instead of "answer one question up front". A caller that
    # knows the name is right passes confirm_channel: true and skips this.
    if channel_status != "exact" and not data.get("confirm_channel"):
        if channel_suggestion:
            reason = (
                f'Channel "{channel}" is new and closely matches the existing channel '
                f'"{channel_suggestion}" — did you mean that? Confirm the channel before classifying '
                f"(POST /api/videos/{video_id}/channel to move it, or re-ingest with confirm_channel: true)."
            )
        else:
            reason = (
                f'Channel "{channel}" is new and matches no existing channel. Confirm it is a genuinely '
                f"new creator (and set its profile subject) before classifying, or re-ingest with "
                f"confirm_channel: true."
            )
        gatekeeper.mark_needs_review(video_id, reason)
        return jsonify({
            "ok": True, "video_id": video_id, "status": "held for channel confirmation",
            "channel": channel, "suggestion": channel_suggestion or None, "reason": reason,
        })

    # The topic gate is for catching an obviously-wrong bulk import. On a
    # channel whose videos are already curated and classified, that question
    # is already settled, so skip it there (see gatekeeper.check_topic).
    conn = get_conn()
    chan_row = conn.execute("SELECT channel_id FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    conn.close()
    trusted = gatekeeper.channel_is_trusted(chan_row["channel_id"]) if chan_row else False

    hold_reason = (
        gatekeeper.check_length(duration_sec, platform)
        or gatekeeper.check_topic(title, script, channel_trusted=trusted)
        or gatekeeper.check_daily_budget()
    )
    if hold_reason:
        gatekeeper.mark_needs_review(video_id, hold_reason)
        return jsonify({"ok": True, "video_id": video_id, "status": "held for review", "reason": hold_reason})

    script_name = "auto_process_video.py" if platform == "YouTube" else "auto_process_shortform_video.py"
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script_name)
    subprocess.Popen([sys.executable, script_path, "--video_id", str(video_id)], start_new_session=True)

    return jsonify({"ok": True, "video_id": video_id, "status": "ingested, classifying in the background"})


def _norm_handle(x):
    """Account names for comparison only: case, @, spacing and punctuation
    all ignored, so "@casuallyfinance", "casuallyfinance" and "Casually
    Finance" are one account rather than three profiles."""
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def _next_format_profile_code(conn):
    """Next free PVS.N. Same convention as A.*/BK.*/C.*/PS.* — the code is
    an allocated identifier, not something a human should have to choose
    and keep straight in their head."""
    max_n = 0
    for r in conn.execute("SELECT profile_code FROM format_profiles WHERE profile_code LIKE 'PVS.%'"):
        suffix = r["profile_code"].split(".", 1)[1]
        if suffix.isdigit():
            max_n = max(max_n, int(suffix))
    return f"PVS.{max_n + 1}"


def get_or_create_format_profile(conn, channel):
    """Resolves an account name to its PVS profile, creating one if this
    account has never been analysed before. Returns (row, created).

    Matching is loose (see _norm_handle) for one reason: the failure that
    matters here is no longer a wrong code but a TYPO — "casually finance"
    silently forking a second profile that then splits an account's
    readings across two pages, each too thin to say anything. Anything a
    person would read as the same account resolves to the same profile."""
    target = _norm_handle(channel)
    if not target:
        return None, False
    for r in conn.execute(
        "SELECT * FROM format_profiles ORDER BY format_profile_id"
    ):
        if target in (_norm_handle(r["handle"]), _norm_handle(r["display_name"])):
            return r, False

    # Single-token names get the @ the other profiles carry, so the pages
    # stay visually consistent; a name with spaces is stored as typed.
    given = channel.strip()
    handle = given if (given.startswith("@") or " " in given) else f"@{given}"
    code = _next_format_profile_code(conn)
    conn.execute(
        "INSERT INTO format_profiles (profile_code, handle, status, n_inputs_analysed) "
        "VALUES (?, ?, 'preliminary', 0)",
        (code, handle),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM format_profiles WHERE profile_code = ?", (code,)
    ).fetchone()
    return row, True


@app.route("/api/format/profiles", methods=["GET"])
def api_format_profiles():
    """Lists the PVS profiles and the account handle each one belongs to.

    Exists so a caller can offer the accounts already being tracked as
    suggestions. Codes are allocated automatically now, so the mistake worth
    preventing is a typo forking a second profile for an account that
    already has one — picking from this list avoids it."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    conn = get_conn()
    rows = conn.execute(
        "SELECT profile_code, handle FROM format_profiles ORDER BY profile_code"
    ).fetchall()
    conn.close()
    return jsonify({"ok": True, "profiles": [
        {"profile_code": r["profile_code"], "handle": r["handle"]} for r in rows
    ]})


@app.route("/api/ingest/format", methods=["POST"])
def api_ingest_format():
    """Receives one video for Production Inputs (P+S) analysis: its transcript
    (possibly empty — a silent format is the point, not a failure) plus the
    frame timestamps the transcriber sampled. Frames themselves follow via
    /api/format/inputs/<id>/frames/<frame_id>, mirroring the production-spec
    pipeline.

    The account name is what a caller supplies; the PVS code is allocated
    here, the way every other profile code in this project is, and a code
    already in use for that account is reused. An explicit profile_code is
    still accepted for a caller that has one.

    Body: {channel, title, url, platform, duration_sec, transcript,
           has_audio, frames: [{frame_number, at_sec}, ...]}"""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    data = request.get_json(silent=True) or {}
    code = (data.get("profile_code") or "").strip()
    channel = (data.get("channel") or "").strip()
    frames = data.get("frames") or []
    if not channel and not code:
        return jsonify({"ok": False, "error": "'channel' is required (the account these videos came from)"}), 400
    if not frames:
        return jsonify({"ok": False, "error": "'frames' is required"}), 400

    conn = get_conn()
    created = False
    if code:
        # An explicit code still works, for a caller that has one. When a
        # channel comes with it, the two must agree: pasting one account's
        # videos against another's code would see them downloaded, classified,
        # paid for, and folded into the wrong creator's readings — far harder
        # to unpick afterwards than to refuse now.
        prof = conn.execute(
            "SELECT * FROM format_profiles WHERE profile_code = ?", (code,)
        ).fetchone()
        if not prof:
            conn.close()
            return jsonify({"ok": False, "error": f"no such format profile: {code}"}), 404
        if (channel and prof["handle"]
                and _norm_handle(channel) != _norm_handle(prof["handle"])
                and not data.get("confirm_channel")):
            conn.close()
            return jsonify({
                "ok": False,
                "error": (f'Channel "{channel}" doesn\'t match {code}, which is '
                          f'"{prof["handle"]}". If these videos really belong to {code}, resubmit '
                          f"with confirm_channel: true — otherwise check the profile code."),
                "profile_code": code, "profile_handle": prof["handle"], "given_channel": channel,
            }), 400
    else:
        # The normal path: the account name is the only thing asked for, and
        # the PVS code is allocated the way every other profile code is.
        prof, created = get_or_create_format_profile(conn, channel)
        if not prof:
            conn.close()
            return jsonify({"ok": False, "error": "'channel' is required (the account these videos came from)"}), 400
        code = prof["profile_code"]

    url = data.get("url")
    chash = normalize_hash(url) if url else None
    if chash:
        existing = conn.execute(
            "SELECT format_input_id FROM format_inputs WHERE content_hash = ?", (chash,)
        ).fetchone()
        if existing:
            # Re-submitting the same URL reuses the row rather than making a
            # second one, so a retried job can't double-count in the aggregate.
            fid = existing["format_input_id"]
            rows = conn.execute(
                "SELECT frame_id, frame_number FROM format_input_frames WHERE format_input_id = ? "
                "ORDER BY frame_number", (fid,)
            ).fetchall()
            conn.close()
            return jsonify({"ok": True, "format_input_id": fid, "duplicate": True,
                            "profile_code": code, "profile_handle": prof["handle"],
                            "profile_created": created,
                            "frames": [{"frame_number": r["frame_number"], "frame_id": r["frame_id"]} for r in rows]})

    cur = conn.execute(
        """INSERT INTO format_inputs
           (format_profile_id, title, url, platform, duration_sec, content_hash, transcript,
            has_audio, n_frames, channel_name, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ingested')""",
        (prof["format_profile_id"], data.get("title"), url, data.get("platform"),
         data.get("duration_sec"), chash, data.get("transcript") or None,
         1 if data.get("has_audio") else 0, len(frames), channel or None),
    )
    fid = cur.lastrowid
    out = []
    for f in frames:
        c2 = conn.execute(
            "INSERT INTO format_input_frames (format_input_id, frame_number, at_sec) VALUES (?, ?, ?)",
            (fid, f.get("frame_number"), f.get("at_sec")),
        )
        out.append({"frame_number": f.get("frame_number"), "frame_id": c2.lastrowid})
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "format_input_id": fid, "duplicate": False,
                    "profile_code": code, "profile_handle": prof["handle"],
                    "profile_created": created, "frames": out})


@app.route("/api/format/inputs/<int:input_id>/frames/<int:frame_id>", methods=["POST"])
def api_set_format_frame(input_id, frame_id):
    """Stores one downscaled frame for a P+S input."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    image_b64 = (request.get_json(silent=True) or {}).get("image_base64")
    if not image_b64:
        return jsonify({"ok": False, "error": "'image_base64' is required"}), 400

    conn = get_conn()
    frame = conn.execute(
        "SELECT frame_id FROM format_input_frames WHERE frame_id = ? AND format_input_id = ?",
        (frame_id, input_id),
    ).fetchone()
    if not frame:
        conn.close()
        return jsonify({"ok": False, "error": "no such frame on this input"}), 404

    out_dir = FORMAT_FRAMES_DIR / str(input_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"frame_{frame_id}.jpg").write_bytes(base64.b64decode(image_b64))
    conn.execute("UPDATE format_input_frames SET captured = 1 WHERE frame_id = ?", (frame_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "frame_id": frame_id})


@app.route("/api/format/inputs/<int:input_id>/classify", methods=["POST"])
def api_classify_format_input(input_id):
    """Kicks off the joint visual+script classification as a detached
    subprocess, same rationale as the other classify routes."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    conn = get_conn()
    row = conn.execute("SELECT format_input_id FROM format_inputs WHERE format_input_id = ?", (input_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "no such format input"}), 404
    conn.execute(
        "UPDATE format_inputs SET status = 'classifying', classification_error = NULL WHERE format_input_id = ?",
        (input_id,),
    )
    conn.commit()
    conn.close()

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "classify_format_input.py")
    subprocess.Popen([sys.executable, script_path, "--format_input_id", str(input_id)], start_new_session=True)
    return jsonify({"ok": True, "format_input_id": input_id, "status": "classifying"})


@app.route("/api/format/inputs/<int:input_id>/status")
def api_format_input_status(input_id):
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    conn = get_conn()
    row = conn.execute(
        "SELECT status, classification_error, on_screen_text FROM format_inputs WHERE format_input_id = ?",
        (input_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"ok": False, "error": "no such format input"}), 404
    return jsonify({"ok": True, "format_input_id": input_id, "status": row["status"],
                    "done": row["status"] in ("classified", "needs_review"),
                    "classification_error": row["classification_error"],
                    "on_screen_text": row["on_screen_text"]})


@app.route("/api/channels/suggest")
def api_suggest_channels():
    """Given a proposed channel name, return the existing channels it might
    actually be — so a chat front end (Hermes/Telegram) can offer them as
    tappable options BEFORE ingesting, instead of the name being taken
    literally and silently creating a near-duplicate channel.

    GET /api/channels/suggest?name=kylscan  ->
      {"exact": false,
       "candidates": [{"channel_name": "kylascan", "profile_code": "A.10",
                       "subject": null, "n_videos": 10, "confidence": 0.94}, ...],
       "recommended_action": "confirm_existing" | "proceed" | "confirm_new"}

    Ranked best-first and capped, so the caller can render a short list of
    buttons plus a "no, it's a new creator" fallback. Each candidate carries
    its profile code AND subject because those are two different things the
    user needs to see to decide (style vs topic)."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    import difflib

    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "'name' is required"}), 400
    limit = min(int(request.args.get("limit", 5)), 20)

    def norm(s):
        return re.sub(r"[^a-z0-9]", "", (s or "").lower())

    target = norm(name)
    conn = get_conn()
    rows = conn.execute(
        """SELECT c.channel_id, c.channel_name, c.platform,
                  p.profile_code, p.subject, p.n_videos_analysed,
                  (SELECT COUNT(*) FROM videos v WHERE v.channel_id = c.channel_id) AS n_videos
           FROM channels c LEFT JOIN style_profiles p ON p.channel_id = c.channel_id"""
    ).fetchall()
    conn.close()

    exact = any(r["channel_name"] == name for r in rows)
    scored = []
    for r in rows:
        ratio = 1.0 if norm(r["channel_name"]) == target else difflib.SequenceMatcher(
            None, target, norm(r["channel_name"])).ratio()
        scored.append((ratio, r))
    scored.sort(key=lambda t: -t[0])

    candidates = [
        {
            "channel_name": r["channel_name"], "platform": r["platform"],
            "profile_code": r["profile_code"], "subject": r["subject"],
            "n_videos": r["n_videos"], "n_analysed": r["n_videos_analysed"],
            "confidence": round(ratio, 3),
        }
        for ratio, r in scored[:limit] if ratio >= 0.5
    ]

    if exact:
        action = "proceed"
    elif candidates and candidates[0]["confidence"] >= CHANNEL_MATCH_THRESHOLD:
        action = "confirm_existing"
    else:
        action = "confirm_new"

    return jsonify({"ok": True, "name": name, "exact": exact,
                    "candidates": candidates, "recommended_action": action})


@app.route("/api/videos/<int:video_id>/channel", methods=["POST"])
def api_reassign_video_channel(video_id):
    """Moves ONE already-ingested video to a different channel, creating the
    channel if it doesn't exist yet. The only existing reassignment path
    (fix_unresolved_channel) moves an entire placeholder channel's batch at
    once and is hardcoded to the "Instagram Import" case, so a single
    misfiled video on a real channel had no route at all short of editing
    the database by hand.

    Body: {"channel_name": "...", "subject": "..."(optional),
           "platform": "..."(optional, only used when creating the channel)}

    NOTE ON SUBJECT: subject is a property of the style PROFILE, not of a
    video — there is no videos.subject column. Passing "subject" sets it on
    the destination channel's profile (if that channel has one), which is
    the closest real equivalent and what a caller asking to "set the
    video's subject" actually means.

    Both the old and new channel's profiles are rebuilt afterwards, since
    the video's numbers have to stop pulling on one average and start
    pulling on the other. Rebuilt at min_n=1 to match what the ingest
    pipelines use, NOT the min_n=10 the other webapp rebuild sites use —
    those two disagree, and using 10 here would silently demote a small
    channel's profile to 'draft' and drop it out of scoring as a side
    effect of moving one video."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)

    payload = request.get_json(silent=True) or {}
    new_name = (payload.get("channel_name") or "").strip()
    subject = (payload.get("subject") or "").strip()
    platform = (payload.get("platform") or "").strip()
    if not new_name:
        return jsonify({"ok": False, "error": "'channel_name' is required"}), 400

    conn = get_conn()
    video = conn.execute(
        """SELECT v.video_id, v.title, v.channel_id, v.media_type, c.channel_name
           FROM videos v JOIN channels c ON c.channel_id = v.channel_id
           WHERE v.video_id = ?""",
        (video_id,),
    ).fetchone()
    if not video:
        conn.close()
        return jsonify({"ok": False, "error": "no such video"}), 404

    old_channel_id, old_channel_name = video["channel_id"], video["channel_name"]

    target = conn.execute("SELECT channel_id FROM channels WHERE channel_name = ?", (new_name,)).fetchone()
    if target:
        target_id, created = target["channel_id"], False
    else:
        cur = conn.execute(
            "INSERT INTO channels (channel_name, platform) VALUES (?, ?)",
            (new_name, platform or video["media_type"] or "Instagram"),
        )
        target_id, created = cur.lastrowid, True

    if target_id == old_channel_id:
        conn.close()
        return jsonify({"ok": True, "video_id": video_id, "status": "already on that channel",
                        "channel_name": new_name, "moved": False})

    conn.execute("UPDATE videos SET channel_id = ? WHERE video_id = ?", (target_id, video_id))

    subject_set_on = None
    if subject:
        prof = conn.execute(
            "SELECT profile_id, profile_code FROM style_profiles WHERE channel_id = ?", (target_id,)
        ).fetchone()
        if prof:
            conn.execute("UPDATE style_profiles SET subject = ? WHERE profile_id = ?", (subject, prof["profile_id"]))
            subject_set_on = prof["profile_code"]
    conn.commit()

    # Rebuild both sides: the origin loses this video from its averages, the
    # destination gains it. Either may legitimately have no profile yet.
    rebuilt = []
    for chan_id, chan_name in ((old_channel_id, old_channel_name), (target_id, new_name)):
        prof = conn.execute(
            "SELECT profile_code, length_band FROM style_profiles WHERE channel_id = ?", (chan_id,)
        ).fetchone()
        if not prof:
            continue
        try:
            build_profile(chan_name, prof["profile_code"], prof["length_band"], min_n=1)
            rebuilt.append(prof["profile_code"])
        except Exception as e:  # noqa: BLE001 - a rebuild failure must not undo the move
            print(f"[api_reassign_video_channel] rebuild of {prof['profile_code']} failed (non-fatal): {e}")
    conn.close()

    return jsonify({
        "ok": True, "video_id": video_id, "moved": True,
        "from_channel": old_channel_name, "to_channel": new_name,
        "channel_created": created, "profiles_rebuilt": rebuilt,
        "subject_set_on_profile": subject_set_on,
        "note": ("subject is stored on the style profile, not the video"
                 if subject and not subject_set_on else None),
    })


@app.route("/api/videos/fix-media-type", methods=["POST"])
def api_fix_media_type():
    """Repairs videos whose media_type disagrees with their source URL —
    the fallout of /api/ingest/video having defaulted an unstated platform
    to "YouTube". media_type picks the classification pipeline, so a reel
    labelled YouTube is sent through the long-form combined call: wrong
    shape for a 60-second video, and several times the cost.

    Body: {"dry_run": true} (default) to report only,
          {"dry_run": false} to apply.
    Only ever corrects rows where the URL is unambiguous."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    payload = request.get_json(silent=True) or {}
    dry_run = payload.get("dry_run", True)

    conn = get_conn()
    rows = conn.execute("SELECT video_id, url, media_type, title FROM videos WHERE url IS NOT NULL").fetchall()
    mismatched = []
    for r in rows:
        inferred = _infer_platform(r["url"], default=None)
        if inferred and r["media_type"] and inferred != r["media_type"]:
            mismatched.append({"video_id": r["video_id"], "title": (r["title"] or "")[:60],
                               "from": r["media_type"], "to": inferred})
    if not dry_run:
        for m in mismatched:
            conn.execute("UPDATE videos SET media_type = ? WHERE video_id = ?", (m["to"], m["video_id"]))
        conn.commit()
    conn.close()

    return jsonify({"ok": True, "dry_run": dry_run, "n_mismatched": len(mismatched),
                    "changes": mismatched[:50]})


@app.route("/api/videos/<int:video_id>/classify", methods=["POST"])
def api_classify_video(video_id):
    """Runs classification on a video that is ALREADY ingested — the
    counterpart to api_classify_production_spec, which the production-spec
    pipeline has always had and this one didn't. Until now classification
    could only ever happen as a side effect of /api/ingest/video, so a
    channel ingested before classification existed (or held for review, or
    interrupted) had no way to be classified short of re-ingesting it.

    Goes through the same gatekeeper checks and the same detached
    subprocess as the ingest path, so cost controls and the
    long-form/short-form script split apply identically. Skips videos that
    are already classified unless force=true is passed, so it is safe to
    fire at a whole channel repeatedly."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)

    conn = get_conn()
    row = conn.execute(
        """SELECT v.video_id, v.title, v.script, v.duration_sec, v.media_type, v.channel_id, a.classified_by
           FROM videos v LEFT JOIN video_attributes a ON a.video_id = v.video_id
           WHERE v.video_id = ?""",
        (video_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"ok": False, "error": "no such video"}), 404

    payload = request.get_json(silent=True) or {}
    already = row["classified_by"] not in (None, "auto", "pending", "needs_review")
    if already and not payload.get("force"):
        return jsonify({"ok": True, "video_id": video_id, "status": "already classified", "skipped": True})

    # skip_topic_check lets a caller force past the keyword gate for a video
    # they have eyeballed themselves — the manual counterpart to the
    # channel_trusted skip, for the first video on a brand-new channel that
    # has nothing classified to vouch for it yet.
    hold_reason = (
        gatekeeper.check_length(row["duration_sec"], row["media_type"])
        or gatekeeper.check_topic(
            row["title"], row["script"],
            channel_trusted=bool(payload.get("skip_topic_check")) or gatekeeper.channel_is_trusted(row["channel_id"]),
        )
        or gatekeeper.check_daily_budget()
    )
    if hold_reason:
        gatekeeper.mark_needs_review(video_id, hold_reason)
        return jsonify({"ok": True, "video_id": video_id, "status": "held for review", "reason": hold_reason})

    # Clear the previous failure reason before re-running. classification_error
    # is only ever written on failure and never cleared, so a stale message
    # from an earlier attempt survives a successful retry and — worse — is
    # indistinguishable from a fresh one. That actively misleads: a video
    # re-run past the topic gate still displayed the old "no PPE subject
    # keyword" text, making a retry look like it had been rejected again.
    conn = get_conn()
    conn.execute("UPDATE video_attributes SET classification_error = NULL WHERE video_id = ?", (video_id,))
    conn.commit()
    conn.close()

    script_name = "auto_process_video.py" if row["media_type"] == "YouTube" else "auto_process_shortform_video.py"
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), script_name)
    subprocess.Popen([sys.executable, script_path, "--video_id", str(video_id)], start_new_session=True)

    return jsonify({"ok": True, "video_id": video_id, "status": "classifying"})


@app.route("/api/videos/<int:video_id>/status")
def api_video_status(video_id):
    """Lets the transcriber poll for classification completion after
    /api/ingest/video, the same way _auto_finish_book_import polls
    /api/books — 'claude' or 'needs_review' are both terminal states."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    conn = get_conn()
    row = conn.execute(
        """SELECT a.classified_by, a.classification_error, p.profile_code
           FROM videos v LEFT JOIN video_attributes a ON a.video_id = v.video_id
           LEFT JOIN channels c ON c.channel_id = v.channel_id
           LEFT JOIN style_profiles p ON p.channel_id = c.channel_id
                AND (p.media_type IS NULL OR p.media_type != 'ProductionSpec')
           WHERE v.video_id = ?""",
        (video_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"ok": False, "error": "no such video"}), 404
    done = row["classified_by"] in ("claude", "needs_review")
    return jsonify({
        "ok": True, "video_id": video_id, "done": done,
        "classified_by": row["classified_by"], "classification_error": row["classification_error"],
        "profile_code": row["profile_code"],
    })


@app.route("/api/profiles/<code>/summary")
def api_profile_summary(code):
    """Read-only JSON summary of one style profile, for chat-based tools
    (Hermes/Telegram) to check on things without scraping the HTML page.
    Same X-Ingest-Key convention as the other /api/* routes."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    conn = get_conn()
    p = conn.execute(
        """SELECT p.profile_code, p.subject, p.status, p.media_type, p.n_videos_analysed,
                  c.channel_name, c.channel_id, c.platform
           FROM style_profiles p JOIN channels c ON c.channel_id = p.channel_id
           WHERE p.profile_code = ?""",
        (code,),
    ).fetchone()
    if not p:
        conn.close()
        return jsonify({"ok": False, "error": f"no such profile: {code}"}), 404
    n_videos = conn.execute(
        "SELECT COUNT(*) FROM videos WHERE channel_id = ?", (p["channel_id"],)
    ).fetchone()[0]
    recent = conn.execute(
        """SELECT title, ingested_at FROM videos WHERE channel_id = ?
           ORDER BY ingested_at DESC LIMIT 5""",
        (p["channel_id"],),
    ).fetchall()
    conn.close()
    return jsonify({
        "ok": True,
        "profile_code": p["profile_code"],
        "channel_name": p["channel_name"],
        "platform": p["platform"],
        "subject": p["subject"],
        "status": p["status"],
        "media_type": p["media_type"],
        "n_videos": n_videos,
        "n_videos_analysed": p["n_videos_analysed"],
        "recent_videos": [{"title": r["title"], "ingested_at": r["ingested_at"]} for r in recent],
    })


@app.route("/api/channels/<name>/summary")
def api_channel_summary(name):
    """Read-only JSON summary of one channel by name, mirrors
    api_profile_summary but keyed by channel rather than profile code
    (useful when a channel doesn't have a profile yet)."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    conn = get_conn()
    c = conn.execute(
        "SELECT channel_id, channel_name, platform FROM channels WHERE channel_name = ?",
        (name,),
    ).fetchone()
    if not c:
        conn.close()
        return jsonify({"ok": False, "error": f"no such channel: {name}"}), 404
    p = conn.execute(
        """SELECT profile_code, subject, status, media_type, n_videos_analysed
           FROM style_profiles WHERE channel_id = ?""",
        (c["channel_id"],),
    ).fetchone()
    n_videos = conn.execute(
        "SELECT COUNT(*) FROM videos WHERE channel_id = ?", (c["channel_id"],)
    ).fetchone()[0]
    recent = conn.execute(
        """SELECT title, ingested_at FROM videos WHERE channel_id = ?
           ORDER BY ingested_at DESC LIMIT 5""",
        (c["channel_id"],),
    ).fetchall()
    conn.close()
    return jsonify({
        "ok": True,
        "channel_name": c["channel_name"],
        "platform": c["platform"],
        "has_profile": p is not None,
        "profile_code": p["profile_code"] if p else None,
        "subject": p["subject"] if p else None,
        "status": p["status"] if p else None,
        "media_type": p["media_type"] if p else None,
        "n_videos": n_videos,
        "n_videos_analysed": p["n_videos_analysed"] if p else 0,
        "recent_videos": [{"title": r["title"], "ingested_at": r["ingested_at"]} for r in recent],
    })


@app.route("/api/videos/<int:video_id>/visuals")
def api_video_visuals(video_id):
    """Lets the transcriber (which has access to the actual video) fetch
    the list of LLM-flagged chart/graph/table moments needing a real
    screenshot, so it knows which timestamps to grab a frame at."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    conn = get_conn()
    rows = conn.execute(
        "SELECT visual_id, timestamp_sec, caption, screenshot_captured FROM video_visuals "
        "WHERE video_id = ? ORDER BY timestamp_sec", (video_id,)
    ).fetchall()
    conn.close()
    return jsonify({"ok": True, "video_id": video_id, "visuals": [dict(r) for r in rows]})


@app.route("/api/videos/<int:video_id>/visuals/<int:visual_id>/screenshot", methods=["POST"])
def api_set_visual_screenshot(video_id, visual_id):
    """Receives one real video-frame screenshot grabbed locally by the
    transcriber and stores it on the persistent disk, mirroring
    api_set_example_screenshot for book pages."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    data = request.get_json(silent=True) or {}
    image_b64 = data.get("image_base64")
    if not image_b64:
        return jsonify({"ok": False, "error": "'image_base64' is required"}), 400

    conn = get_conn()
    visual = conn.execute(
        "SELECT visual_id FROM video_visuals WHERE visual_id = ? AND video_id = ?", (visual_id, video_id)
    ).fetchone()
    if not visual:
        conn.close()
        return jsonify({"ok": False, "error": "no such visual on this video"}), 404

    video_dir = VIDEO_VISUALS_DIR / str(video_id)
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / f"visual_{visual_id}.png").write_bytes(base64.b64decode(image_b64))

    conn.execute("UPDATE video_visuals SET screenshot_captured = 1 WHERE visual_id = ?", (visual_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "video_id": video_id, "visual_id": visual_id})


@app.route("/videos/<int:video_id>/visual/<int:visual_id>.png")
def video_visual_image(video_id, visual_id):
    image_path = VIDEO_VISUALS_DIR / str(video_id) / f"visual_{visual_id}.png"
    if not image_path.is_file():
        abort(404)
    return send_file(image_path, mimetype="image/png")


@app.route("/api/ingest/production-spec", methods=["POST"])
def api_ingest_production_spec():
    """Production Spec counterpart to /api/ingest/video — the Instagram Bulk
    Transcriber's shot-analysis job POSTs here after running ffmpeg
    scene-detection locally, handing over the shot boundary list (no frame
    images yet, keeping this payload small the same way /api/ingest/video
    keeps its payload to just text). Ingestion is synchronous; no subprocess
    is launched here since classification can't start until every shot's
    frame has been uploaded via api_set_shot_frame below."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    channel = (data.get("channel") or "").strip()
    shots = data.get("shots") or []
    if not title or not channel or not shots:
        return jsonify({"ok": False, "error": "'title', 'channel', and a non-empty 'shots' list are required."}), 400

    input_id, shot_rows = ingest_production_spec_row(
        title=title,
        channel=channel,
        platform=data.get("platform") or "Instagram",
        url=data.get("url") or None,
        duration_sec=data.get("duration_sec") or None,
        posted_at=data.get("posted_at") or None,
        scene_threshold=data.get("scene_threshold") or 0.25,
        shots=shots,
    )
    return jsonify({"ok": True, "input_id": input_id, "shots": shot_rows})


@app.route("/api/production-spec/inputs/<int:input_id>/shots/<int:shot_id>/frame", methods=["POST"])
def api_set_shot_frame(input_id, shot_id):
    """Receives one shot's representative frame grabbed locally by the
    transcriber, mirrors api_set_visual_screenshot for video_visuals."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    data = request.get_json(silent=True) or {}
    image_b64 = data.get("image_base64")
    if not image_b64:
        return jsonify({"ok": False, "error": "'image_base64' is required"}), 400

    conn = get_conn()
    shot = conn.execute(
        "SELECT shot_id FROM production_spec_shots WHERE shot_id = ? AND input_id = ?", (shot_id, input_id)
    ).fetchone()
    if not shot:
        conn.close()
        return jsonify({"ok": False, "error": "no such shot on this input"}), 404

    shot_dir = PRODUCTION_SPEC_SHOTS_DIR / str(input_id)
    shot_dir.mkdir(parents=True, exist_ok=True)
    (shot_dir / f"shot_{shot_id}.png").write_bytes(base64.b64decode(image_b64))

    conn.execute("UPDATE production_spec_shots SET frame_captured = 1 WHERE shot_id = ?", (shot_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "input_id": input_id, "shot_id": shot_id})


@app.route("/api/production-spec/inputs/<int:input_id>/classify", methods=["POST"])
def api_classify_production_spec(input_id):
    """Kicks off shot-content classification once every frame has been
    uploaded — a detached subprocess, same rationale as api_ingest_video:
    the vision calls can take long enough that an in-process thread risks
    gunicorn's arbiter recycling the worker mid-call.

    Optional JSON body {"batch_size": N} overrides the default 15
    shots-per-vision-call for this one run — useful for retrying an input
    that keeps failing at the default size (e.g. an empty/malformed reply
    from Claude on a particular batch). Optional {"skip_shots": [16, ...]}
    leaves specific shot_numbers out of every vision call entirely (e.g. a
    specific frame that reliably triggers an empty reply from Claude) —
    those shots end up content_category='other', classified_by='skipped'
    instead of unclassified."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    conn = get_conn()
    row = conn.execute("SELECT input_id FROM production_spec_inputs WHERE input_id = ?", (input_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "no such input"}), 404
    conn.execute("UPDATE production_spec_inputs SET status = 'classifying' WHERE input_id = ?", (input_id,))
    conn.commit()
    conn.close()

    payload = request.get_json(silent=True) or {}
    try:
        batch_size = int(payload["batch_size"]) if payload.get("batch_size") else None
    except (TypeError, ValueError):
        batch_size = None
    try:
        skip_shots = [int(n) for n in payload.get("skip_shots") or []]
    except (TypeError, ValueError):
        skip_shots = []

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "classify_production_spec_shots.py")
    cmd = [sys.executable, script_path, "--input_id", str(input_id)]
    if batch_size:
        cmd += ["--batch_size", str(batch_size)]
    if skip_shots:
        cmd += ["--skip_shots", ",".join(str(n) for n in skip_shots)]
    subprocess.Popen(cmd, start_new_session=True)

    return jsonify({"ok": True, "input_id": input_id, "status": "classifying", "batch_size": batch_size, "skip_shots": skip_shots})


@app.route("/api/production-spec/inputs/<int:input_id>/status")
def api_production_spec_status(input_id):
    """Lets the transcriber poll for classification completion, mirrors
    api_video_status — 'classified' or 'needs_review' are terminal states."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    conn = get_conn()
    row = conn.execute(
        """SELECT i.status, i.classification_error, p.profile_code
           FROM production_spec_inputs i
           LEFT JOIN channels c ON c.channel_id = i.channel_id
           LEFT JOIN style_profiles p ON p.channel_id = c.channel_id AND p.media_type = 'ProductionSpec'
           WHERE i.input_id = ?""",
        (input_id,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"ok": False, "error": "no such input"}), 404
    done = row["status"] in ("classified", "needs_review")
    return jsonify({
        "ok": True, "input_id": input_id, "done": done,
        "status": row["status"], "classification_error": row["classification_error"],
        "profile_code": row["profile_code"],
    })


@app.route("/production/inputs/<int:input_id>/shot/<int:shot_id>.png")
def production_spec_shot_image(input_id, shot_id):
    image_path = PRODUCTION_SPEC_SHOTS_DIR / str(input_id) / f"shot_{shot_id}.png"
    if not image_path.is_file():
        abort(404)
    return send_file(image_path, mimetype="image/png")


@app.route("/production/inputs")
def production_inputs_list():
    channel_filter = request.args.get("channel_id", type=int)
    status_filter = request.args.get("status", "")
    q = request.args.get("q", "").strip()

    query = """SELECT i.input_id, i.title, i.platform, i.url, i.status, i.ingested_at,
                      c.channel_id, c.channel_name,
                      a.total_shots, a.avg_shot_length_sec, a.dominant_shot_category
               FROM production_spec_inputs i
               JOIN channels c ON c.channel_id = i.channel_id
               LEFT JOIN production_spec_attributes a ON a.input_id = i.input_id
               WHERE 1=1"""
    params = []
    if channel_filter:
        query += " AND c.channel_id = ?"
        params.append(channel_filter)
    if status_filter:
        query += " AND i.status = ?"
        params.append(status_filter)
    if q:
        query += " AND i.title LIKE ?"
        params.append(f"%{q}%")
    query += " ORDER BY i.ingested_at DESC"

    conn = get_conn()
    rows = conn.execute(query, params).fetchall()
    channels = conn.execute(
        "SELECT DISTINCT c.channel_id, c.channel_name FROM channels c "
        "JOIN production_spec_inputs i ON i.channel_id = c.channel_id ORDER BY c.channel_name"
    ).fetchall()
    conn.close()
    return render_template(
        "production_inputs_list.html", active="production-inputs", rows=rows, channels=channels,
        filters={"channel_id": channel_filter, "status": status_filter, "q": q},
    )


@app.route("/production/inputs/<int:input_id>")
def production_input_detail(input_id):
    conn = get_conn()
    row = conn.execute(
        """SELECT i.*, c.channel_name, p.profile_code
           FROM production_spec_inputs i JOIN channels c ON c.channel_id = i.channel_id
           LEFT JOIN style_profiles p ON p.channel_id = c.channel_id AND p.media_type = 'ProductionSpec'
           WHERE i.input_id = ?""",
        (input_id,),
    ).fetchone()
    if not row:
        conn.close()
        flash(f"No such Production Spec input: {input_id}")
        return redirect(url_for("production_inputs_list"))
    attrs = conn.execute("SELECT * FROM production_spec_attributes WHERE input_id = ?", (input_id,)).fetchone()
    shots = conn.execute(
        "SELECT shot_id, shot_number, start_sec, end_sec, duration_sec, content_category, frame_captured "
        "FROM production_spec_shots WHERE input_id = ? ORDER BY shot_number", (input_id,)
    ).fetchall()
    conn.close()
    return render_template(
        "production_input_detail.html", active="production-inputs",
        input=row, attrs=attrs, shots=shots,
    )


@app.route("/production/inputs/<int:input_id>/delete", methods=["POST"])
def production_input_delete(input_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT channel_id FROM production_spec_inputs WHERE input_id = ?", (input_id,)
    ).fetchone()
    if not row:
        conn.close()
        flash(f"No such Production Spec input: {input_id}")
        return redirect(url_for("production_inputs_list"))
    channel_id = row["channel_id"]
    channel = conn.execute("SELECT channel_name FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()

    conn.execute("DELETE FROM production_spec_shots WHERE input_id = ?", (input_id,))
    conn.execute("DELETE FROM production_spec_attributes WHERE input_id = ?", (input_id,))
    conn.execute("DELETE FROM production_spec_inputs WHERE input_id = ?", (input_id,))
    conn.commit()
    conn.close()

    shot_dir = PRODUCTION_SPEC_SHOTS_DIR / str(input_id)
    if shot_dir.is_dir():
        shutil.rmtree(shot_dir, ignore_errors=True)

    if channel:
        try:
            conn = get_conn()
            code = conn.execute(
                "SELECT profile_code FROM style_profiles WHERE channel_id = ? AND media_type = 'ProductionSpec'",
                (channel_id,),
            ).fetchone()
            conn.close()
            if code:
                production_spec_build_profile(channel["channel_name"], code["profile_code"], min_n=1)
        except Exception as e:
            flash(f"Input deleted, but rebuilding the Production Spec profile failed: {e}")

    flash(f"Deleted Production Spec input {input_id}.")
    return redirect(url_for("production_inputs_list"))


# The format axes, mirrored from migrate_add_format_profiles.FORMAT_AXES so
# the page can show a value against the vocabulary it was chosen from — a
# value means little without the alternatives it was picked over.
FORMAT_AXES = [
    ("verbal_channel", "Where the words live",
     ["Spoken monologue", "On-screen text", "Borrowed audio", "Caption only", "Mixed"]),
    ("verbal_authorship", "Who wrote the words",
     ["Original", "Borrowed", "Hybrid"]),
    ("visual_role", "What the visuals do",
     ["Illustrate the script", "Carry meaning alone", "Decorative", "Are the content"]),
    ("audio_role", "Role of sound",
     ["Primary", "Borrowed hook", "Ambient", "Silent"]),
    ("coupling", "Script-visual coupling",
     ["Beat-synced", "Loose", "Independent"]),
]


def _format_profile_rows(conn):
    return conn.execute(
        """SELECT f.*, c.channel_name,
                  sp.profile_code AS style_code, pp.profile_code AS production_code
           FROM format_profiles f
           LEFT JOIN channels c ON c.channel_id = f.channel_id
           LEFT JOIN style_profiles sp ON sp.profile_id = f.style_profile_id
           LEFT JOIN style_profiles pp ON pp.profile_id = f.production_profile_id
           ORDER BY CAST(SUBSTR(f.profile_code, 5) AS INTEGER)"""
    ).fetchall()


@app.route("/production/formats/inputs")
def format_inputs_list():
    """Production Inputs (P+S) — the analysed VIDEOS, one row each.

    Split from the PVS profile list for the same reason the Library splits
    inputs from profiles: a profile is a claim about an account, and the
    inputs are the evidence for it. Keeping them on one page hides how thin
    (or how mixed) the evidence behind a reading actually is."""
    profile_filter = request.args.get("profile", "").strip()
    status_filter = request.args.get("status", "").strip()
    q = request.args.get("q", "").strip()

    query = """SELECT i.*, f.profile_code, f.handle
               FROM format_inputs i
               LEFT JOIN format_profiles f ON f.format_profile_id = i.format_profile_id
               WHERE 1=1"""
    params = []
    if profile_filter:
        query += " AND f.profile_code = ?"
        params.append(profile_filter)
    if status_filter:
        query += " AND i.status = ?"
        params.append(status_filter)
    if q:
        query += " AND (i.title LIKE ? OR i.channel_name LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    query += " ORDER BY i.ingested_at DESC, i.format_input_id DESC"

    conn = get_conn()
    rows = [dict(r) for r in conn.execute(query, params)]
    profiles = conn.execute(
        "SELECT profile_code, handle FROM format_profiles "
        "ORDER BY CAST(SUBSTR(profile_code, 5) AS INTEGER)"
    ).fetchall()
    conn.close()
    return render_template("format_inputs_list.html", active="production-format-inputs",
                           rows=rows, profiles=profiles,
                           filters={"profile": profile_filter, "status": status_filter, "q": q})


@app.route("/production/formats/inputs/<int:input_id>")
def format_input_detail(input_id):
    """One analysed video: what the joint pass read, and what it read it from."""
    conn = get_conn()
    row = conn.execute(
        """SELECT i.*, f.profile_code, f.handle
           FROM format_inputs i
           LEFT JOIN format_profiles f ON f.format_profile_id = i.format_profile_id
           WHERE i.format_input_id = ?""",
        (input_id,),
    ).fetchone()
    if not row:
        conn.close()
        flash(f"No such P+S input: {input_id}")
        return redirect(url_for("format_inputs_list"))
    readings = {
        r["axis"]: {"value": r["value"], "note": r["note"]}
        for r in conn.execute(
            "SELECT axis, value, note FROM format_input_readings WHERE format_input_id = ?", (input_id,)
        )
    }
    frames = conn.execute(
        "SELECT frame_id, frame_number, at_sec, captured FROM format_input_frames "
        "WHERE format_input_id = ? ORDER BY frame_number", (input_id,)
    ).fetchall()
    conn.close()
    return render_template("format_input_detail.html", active="production-format-inputs",
                           i=dict(row), readings=readings, frames=frames, axes=FORMAT_AXES)


@app.route("/production/formats/inputs/<int:input_id>/frame/<int:frame_id>.jpg")
def format_input_frame_image(input_id, frame_id):
    image_path = FORMAT_FRAMES_DIR / str(input_id) / f"frame_{frame_id}.jpg"
    if not image_path.is_file():
        abort(404)
    return send_file(image_path, mimetype="image/jpeg")


@app.route("/production/formats")
def format_profiles_list():
    """Production Inputs (P+S) — format profiles (PVS.*), which describe how
    a creator's VISUALS and SCRIPT relate. Distinct from both A.* (what a
    video says) and PS.* (how it is cut), because the attributes here are
    relational and invisible to either pipeline alone."""
    conn = get_conn()
    rows = _format_profile_rows(conn)
    profiles = []
    for r in rows:
        attrs = {
            a["axis"]: {"value": a["value"], "note": a["note"], "source": a["source"]}
            for a in conn.execute(
                "SELECT axis, value, note, source FROM format_profile_attributes WHERE format_profile_id = ?",
                (r["format_profile_id"],),
            )
        }
        profiles.append({**dict(r), "attrs": attrs})
    conn.close()
    return render_template("format_profiles_list.html", active="production-formats",
                           profiles=profiles, axes=FORMAT_AXES)


@app.route("/production/formats/<code>")
def format_profile_detail(code):
    conn = get_conn()
    row = conn.execute(
        """SELECT f.*, c.channel_name,
                  sp.profile_code AS style_code, pp.profile_code AS production_code
           FROM format_profiles f
           LEFT JOIN channels c ON c.channel_id = f.channel_id
           LEFT JOIN style_profiles sp ON sp.profile_id = f.style_profile_id
           LEFT JOIN style_profiles pp ON pp.profile_id = f.production_profile_id
           WHERE f.profile_code = ?""",
        (code,),
    ).fetchone()
    if not row:
        conn.close()
        return "Format profile not found", 404
    attrs = {
        a["axis"]: {"value": a["value"], "note": a["note"], "source": a["source"]}
        for a in conn.execute(
            "SELECT axis, value, note, source FROM format_profile_attributes WHERE format_profile_id = ?",
            (row["format_profile_id"],),
        )
    }
    others = [dict(r) for r in _format_profile_rows(conn) if r["profile_code"] != code]
    conn.close()
    return render_template("format_profile_detail.html", active="production-formats",
                           p=dict(row), attrs=attrs, axes=FORMAT_AXES, others=others)


# --- generic rename/delete, for the right-click menu -------------------
# One spec per entity the context menu can act on. Kept as data rather than
# a route each, so adding an entity is one entry instead of two endpoints.
#
#   table/pk/name_column — what to rename
#   children             — rows to remove BEFORE the parent, in order. The
#                          schema has no ON DELETE CASCADE anywhere and
#                          get_conn() sets PRAGMA foreign_keys = ON, so an
#                          unlisted child means an IntegrityError and a 500.
#   detach               — (table, column) pairs set to NULL instead of
#                          deleted: tracking rows the user authored, which
#                          shouldn't vanish because their source did.
#
# Channels are deliberately absent from delete: removing one would have to
# take every video with it, which is a genuinely destructive action that
# shouldn't sit one right-click away. Rename only.
ENTITY_SPECS = {
    "video": {
        "label": "video", "table": "videos", "pk": "video_id", "name_column": "title",
        "children": ["video_visuals", "video_points", "video_terms", "video_examples",
                     "video_sections", "video_attributes"],
        "detach": [("test_scores", "match_video_id"), ("swipe_candidates", "source_video_id"),
                   ("production_spec_creations", "source_video_id")],
        "rebuild_profile_for_channel": True,
    },
    "book": {
        "label": "book", "table": "books", "pk": "book_id", "name_column": "title",
        "children": ["book_examples", "book_terms", "book_points", "book_sections", "book_attributes"],
        "detach": [("swipe_candidates", "source_book_id"), ("production_spec_creations", "source_book_id")],
    },
    "production_input": {
        "label": "production input", "table": "production_spec_inputs", "pk": "input_id",
        "name_column": "title",
        "children": ["production_spec_shots", "production_spec_attributes"],
        "detach": [("video_creations", "source_input_id")],
    },
    "format_input": {
        "label": "P+S input", "table": "format_inputs", "pk": "format_input_id",
        "name_column": "title",
        "children": ["format_input_readings", "format_input_frames"],
    },
    "format_profile": {
        "label": "format profile", "table": "format_profiles", "pk": "format_profile_id",
        # handle, not display_name: the @handle is what both pages show and
        # what identifies the account. display_name is rendered nowhere, so
        # renaming it appeared to do nothing at all — the write succeeded
        # every time, into a field with no way to see it.
        "name_column": "handle",
        "children": ["format_profile_attributes"], "detach": [],
    },
    "channel": {
        "label": "channel", "table": "channels", "pk": "channel_id", "name_column": "channel_name",
        "no_delete": "Deleting a channel would take every one of its videos with it — "
                     "delete the videos individually first.",
    },
}


@app.route("/api/entity/rename", methods=["POST"])
def api_entity_rename():
    """Rename one row, for the right-click menu. Browser-facing (this app
    has no login by design), so it validates the entity type against
    ENTITY_SPECS rather than accepting a table name from the client."""
    payload = request.get_json(silent=True) or {}
    kind, entity_id, new_name = payload.get("kind"), payload.get("id"), (payload.get("name") or "").strip()
    spec = ENTITY_SPECS.get(kind)
    if not spec:
        return jsonify({"ok": False, "error": f"unknown entity type: {kind}"}), 400
    if not new_name:
        return jsonify({"ok": False, "error": "a name is required"}), 400
    try:
        entity_id = int(entity_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid id"}), 400

    conn = get_conn()
    row = conn.execute(
        f"SELECT {spec['name_column']} AS name FROM {spec['table']} WHERE {spec['pk']} = ?", (entity_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": f"no such {spec['label']}"}), 404
    old = row["name"]
    conn.execute(
        f"UPDATE {spec['table']} SET {spec['name_column']} = ? WHERE {spec['pk']} = ?", (new_name, entity_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "kind": kind, "id": entity_id, "from": old, "to": new_name})


@app.route("/api/entity/delete", methods=["POST"])
def api_entity_delete():
    """Delete one row and everything that references it, for the right-click
    menu. Children are removed in the order listed in ENTITY_SPECS; rows
    that merely point at it are detached rather than destroyed."""
    payload = request.get_json(silent=True) or {}
    kind, entity_id = payload.get("kind"), payload.get("id")
    spec = ENTITY_SPECS.get(kind)
    if not spec:
        return jsonify({"ok": False, "error": f"unknown entity type: {kind}"}), 400
    if spec.get("no_delete"):
        return jsonify({"ok": False, "error": spec["no_delete"]}), 400
    try:
        entity_id = int(entity_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid id"}), 400

    conn = get_conn()
    row = conn.execute(
        f"SELECT {spec['name_column']} AS name FROM {spec['table']} WHERE {spec['pk']} = ?", (entity_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": f"no such {spec['label']}"}), 404
    name = row["name"]

    channel_id = None
    if spec.get("rebuild_profile_for_channel"):
        c = conn.execute(
            f"SELECT channel_id FROM {spec['table']} WHERE {spec['pk']} = ?", (entity_id,)
        ).fetchone()
        channel_id = c["channel_id"] if c else None

    try:
        for table, column in spec.get("detach", []):
            conn.execute(f"UPDATE {table} SET {column} = NULL WHERE {column} = ?", (entity_id,))
        for child in spec.get("children", []):
            conn.execute(f"DELETE FROM {child} WHERE {spec['pk']} = ?", (entity_id,))
        conn.execute(f"DELETE FROM {spec['table']} WHERE {spec['pk']} = ?", (entity_id,))
        conn.commit()
    except Exception as e:  # noqa: BLE001 - report the real reason rather than a bare 500
        conn.rollback()
        conn.close()
        return jsonify({"ok": False, "error": f"delete failed: {e}"}), 500

    # A deleted video's numbers must stop pulling on its channel's averages.
    rebuilt = None
    if channel_id:
        prof = conn.execute(
            "SELECT profile_code, length_band, channel_id FROM style_profiles "
            "WHERE channel_id = ? AND media_type != 'ProductionSpec'", (channel_id,)
        ).fetchone()
        chan = conn.execute("SELECT channel_name FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
        if prof and chan:
            try:
                build_profile(chan["channel_name"], prof["profile_code"], prof["length_band"], min_n=1)
                rebuilt = prof["profile_code"]
            except Exception as e:  # noqa: BLE001 - a rebuild failure must not undo the delete
                print(f"[api_entity_delete] profile rebuild failed (non-fatal): {e}")
    conn.close()
    return jsonify({"ok": True, "kind": kind, "id": entity_id, "deleted": name, "profile_rebuilt": rebuilt})


@app.route("/production/profiles")
def production_profiles_list():
    conn = get_conn()
    rows = conn.execute(
        """SELECT p.profile_id, p.profile_code, p.channel_id, p.overview, p.n_videos_analysed, p.status,
                  c.channel_name
           FROM style_profiles p JOIN channels c ON c.channel_id = p.channel_id
           WHERE p.media_type = 'ProductionSpec' ORDER BY p.profile_code"""
    ).fetchall()
    conn.close()
    return render_template("production_profiles_list.html", active="production-profiles", profiles=rows)


@app.route("/production/profiles/<code>")
def production_profile_detail(code):
    conn = get_conn()
    profile = conn.execute(
        """SELECT p.*, c.channel_name FROM style_profiles p JOIN channels c ON c.channel_id = p.channel_id
           WHERE p.profile_code = ? AND p.media_type = 'ProductionSpec'""",
        (code,),
    ).fetchone()
    if not profile:
        conn.close()
        flash(f"No such Production Spec profile: {code}")
        return redirect(url_for("production_profiles_list"))
    numeric = conn.execute(
        "SELECT attribute, mean_val, std_val, min_val, max_val, median_val FROM profile_fingerprint_numeric "
        "WHERE profile_id = ? ORDER BY attribute", (profile["profile_id"],)
    ).fetchall()
    categorical_rows = conn.execute(
        "SELECT attribute, value, share_pct FROM profile_fingerprint_categorical "
        "WHERE profile_id = ? ORDER BY attribute, share_pct DESC", (profile["profile_id"],)
    ).fetchall()
    categorical = {}
    for r in categorical_rows:
        categorical.setdefault(r["attribute"], []).append(r)
    inputs = conn.execute(
        """SELECT i.input_id, i.title, i.url, i.ingested_at, a.total_shots, a.avg_shot_length_sec
           FROM production_spec_inputs i LEFT JOIN production_spec_attributes a ON a.input_id = i.input_id
           WHERE i.channel_id = ? ORDER BY i.ingested_at DESC""",
        (profile["channel_id"],),
    ).fetchall()
    conn.close()
    return render_template(
        "production_profile_detail.html", active="production-profiles",
        profile=profile, numeric=numeric, categorical=categorical, inputs=inputs,
    )


@app.route("/production/creations")
def video_creations_list():
    status_filter = request.args.get("status", "")
    profile_filter = request.args.get("profile", "")

    query = """SELECT vc.*, p.profile_code, i.title AS source_title
               FROM video_creations vc
               LEFT JOIN style_profiles p ON p.profile_id = vc.target_profile_id
               LEFT JOIN production_spec_inputs i ON i.input_id = vc.source_input_id
               WHERE 1=1"""
    params = []
    if status_filter:
        query += " AND vc.status = ?"
        params.append(status_filter)
    if profile_filter:
        query += " AND p.profile_code = ?"
        params.append(profile_filter)
    query += " ORDER BY vc.created_at DESC"

    conn = get_conn()
    rows = conn.execute(query, params).fetchall()
    profiles = conn.execute(
        """SELECT p.profile_code, c.channel_name FROM style_profiles p JOIN channels c ON c.channel_id = p.channel_id
           WHERE p.media_type = 'ProductionSpec' ORDER BY p.profile_code"""
    ).fetchall()
    conn.close()
    return render_template(
        "video_creations_list.html", active="production-creations", rows=rows, profiles=profiles,
        filters={"status": status_filter, "profile": profile_filter},
    )


@app.route("/production/creations/new", methods=["GET", "POST"])
def video_creation_create():
    conn = get_conn()
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        brief = (request.form.get("brief") or "").strip()
        source_input_id = request.form.get("source_input_id") or None
        target_profile_id = request.form.get("target_profile_id") or None
        status = request.form.get("status") or "planned"
        if not title:
            flash("Title is required.")
        else:
            cur = conn.execute(
                "INSERT INTO video_creations (source_input_id, target_profile_id, title, brief, status) "
                "VALUES (?, ?, ?, ?, ?)",
                (source_input_id, target_profile_id, title, brief, status),
            )
            conn.commit()
            creation_id = cur.lastrowid
            conn.close()
            return redirect(url_for("video_creation_detail", creation_id=creation_id))

    inputs = conn.execute("SELECT input_id, title FROM production_spec_inputs ORDER BY ingested_at DESC").fetchall()
    profiles = conn.execute(
        """SELECT p.profile_id, p.profile_code, c.channel_name FROM style_profiles p
           JOIN channels c ON c.channel_id = p.channel_id
           WHERE p.media_type = 'ProductionSpec' ORDER BY p.profile_code"""
    ).fetchall()
    conn.close()
    return render_template(
        "video_creation_form.html", active="production-creations", inputs=inputs, profiles=profiles,
    )


@app.route("/production/creations/<int:creation_id>")
def video_creation_detail(creation_id):
    conn = get_conn()
    row = conn.execute(
        """SELECT vc.*, p.profile_code, c.channel_name AS profile_channel_name, i.title AS source_title
           FROM video_creations vc
           LEFT JOIN style_profiles p ON p.profile_id = vc.target_profile_id
           LEFT JOIN channels c ON c.channel_id = p.channel_id
           LEFT JOIN production_spec_inputs i ON i.input_id = vc.source_input_id
           WHERE vc.creation_id = ?""",
        (creation_id,),
    ).fetchone()
    conn.close()
    if not row:
        flash(f"No such video creation: {creation_id}")
        return redirect(url_for("video_creations_list"))
    return render_template("video_creation_detail.html", active="production-creations", creation=row)


@app.route("/production/creations/<int:creation_id>/update", methods=["POST"])
def video_creation_update(creation_id):
    conn = get_conn()
    row = conn.execute("SELECT creation_id FROM video_creations WHERE creation_id = ?", (creation_id,)).fetchone()
    if not row:
        conn.close()
        flash(f"No such video creation: {creation_id}")
        return redirect(url_for("video_creations_list"))
    conn.execute(
        """UPDATE video_creations SET status = ?, generation_tool = ?, output_url = ?, output_file_path = ?,
           updated_at = datetime('now') WHERE creation_id = ?""",
        (
            request.form.get("status") or "planned",
            request.form.get("generation_tool") or None,
            request.form.get("output_url") or None,
            request.form.get("output_file_path") or None,
            creation_id,
        ),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("video_creation_detail", creation_id=creation_id))


@app.route("/production/creations/<int:creation_id>/delete", methods=["POST"])
def video_creation_delete(creation_id):
    conn = get_conn()
    conn.execute("DELETE FROM video_creations WHERE creation_id = ?", (creation_id,))
    conn.commit()
    conn.close()
    flash(f"Deleted video creation {creation_id}.")
    return redirect(url_for("video_creations_list"))


PRODUCTION_CREATION_SHOT_MIX_FIELDS = [
    ("pct_illustration_panel", "illustration panel"),
    ("pct_step_card", "step card"),
    ("pct_narrator_reaction", "narrator reaction"),
    ("pct_map_data_graphic", "map/data graphic"),
    ("pct_cta", "cta"),
    ("pct_other", "other"),
]

PRODUCTION_CREATION_PROMPT = """You are building a shot-pacing production spec: a plan for recutting a long-form video into a shorter illustrated recut, broken into numbered beats.

SOURCE VIDEO CONTENT — use these real facts/points as the beats' content, don't invent new ones:
{content_block}

PACING TARGET — measured from {production_channel_name}'s real shot-by-shot analysis (n={n_analysed} video(s) classified so far, so treat this as directional, not gospel):
- Average shot length: {avg_shot_length:.2f}s
- Shot-type mix: {shot_mix_summary}

VOICE/STYLE TARGET: {style_channel_name}{style_overview_suffix}
{format_block}
Return ONLY a JSON object shaped exactly like this, no other text:
{{
  "title": "a punchy title for this recut, in the source's own vocabulary",
  "dek": "one sentence describing what this recut is and the pacing template it's built on",
  "beats": [
    {{
      "step": 1,
      "title": "short beat title",
      "duration_sec_min": 10, "duration_sec_max": 14,
      "shot_count_min": 4, "shot_count_max": 5,
      "content_points": ["fact drawn from the source content above, verbatim or close to it"],
      "illustration_captions": ["one visual direction per shot or shot-group"],
      "punch_tags": ["2-4 short punch words for on-screen captions"]
    }}
  ],
  "production_notes": [
    {{"heading": "short heading", "text": "one craft note, 1-2 sentences"}}
  ]
}}

Split the source content into 5-7 beats that cover its real structure end to end — don't pad, and don't invent facts not present in the source content above."""


# The P+S block is appended only when a format profile is chosen. It is
# written as constraints rather than description because these axes change
# what the beats must CONTAIN: a format whose visuals carry meaning alone
# cannot be specified with illustration captions that merely restate the
# script, and a borrowed-audio format has no narration to write at all.
FORMAT_PROFILE_PROMPT_BLOCK = """
FORMAT TARGET — how words and pictures relate for {handle} ({profile_code}, read from {n_inputs} analysed video(s){preliminary_note}):
{axis_lines}

Honour these as constraints on the beats themselves, not as notes appended at the end:
- If the words live on screen rather than in speech, write the on-screen text as the content, and don't write narration that isn't there.
- If the visuals carry meaning alone, each illustration caption must advance the point rather than restate the words.
- If the audio is borrowed, don't script a voiceover — script what is shown and what is written over it.
"""


def _video_full_breakdown_block(conn, video_id, limit=6000):
    """Whole-video content block for a production creation prompt: every
    chapter's title/summary/points if the video has a section breakdown,
    else a truncated raw script — same fallback video_detail() uses."""
    video = conn.execute("SELECT title, script FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    if not video:
        return ""
    section_rows = conn.execute(
        "SELECT * FROM video_sections WHERE video_id = ? ORDER BY section_number, section_id", (video_id,)
    ).fetchall()
    if not section_rows:
        return f"Title: {video['title']}\n\n{(video['script'] or '')[:limit]}"
    parts = [f"Title: {video['title']}"]
    for s in section_rows:
        pts = conn.execute(
            "SELECT point_text FROM video_points WHERE section_id = ? ORDER BY point_id", (s["section_id"],)
        ).fetchall()
        points_text = "\n".join(f"- {p['point_text']}" for p in pts)
        parts.append(f"Section {s['section_number']}: {s['section_title']}\nSummary: {s['summary']}\nPoints:\n{points_text}")
    return "\n\n".join(parts)[:limit]


def _production_creation_source_label(conn, kind, video_id, book_id, section_id, transformation_id):
    """Short human label for any of the 5 source kinds — used on the list
    page and in the masthead's 'Source:' line. Returns None if the
    referenced row no longer exists (e.g. deleted since this creation was
    made)."""
    kind = kind or "video"
    if kind == "video_section":
        r = conn.execute(
            "SELECT vs.section_title, v.title AS parent_title FROM video_sections vs "
            "JOIN videos v ON v.video_id = vs.video_id WHERE vs.section_id = ?", (section_id,),
        ).fetchone()
        return f"{r['parent_title']} — {r['section_title']}" if r else None
    if kind == "book_section":
        r = conn.execute(
            "SELECT bs.section_title, b.title AS parent_title FROM book_sections bs "
            "JOIN books b ON b.book_id = bs.book_id WHERE bs.section_id = ?", (section_id,),
        ).fetchone()
        return f"{r['parent_title']} — {r['section_title']}" if r else None
    if kind == "book":
        r = conn.execute("SELECT title FROM books WHERE book_id = ?", (book_id,)).fetchone()
        return r["title"] if r else None
    if kind == "creation":
        r = conn.execute("SELECT generated_title FROM transformations WHERE transformation_id = ?", (transformation_id,)).fetchone()
        return (r["generated_title"] or f"Creation #{transformation_id}") if r else None
    r = conn.execute("SELECT title FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    return r["title"] if r else None


def _production_creation_source_content(conn, kind, video_id, book_id, section_id, transformation_id, limit=6000):
    """Returns (label, content_block) for any of the 5 supported source
    kinds — the single-video/single-book cases reuse the existing
    breakdown fetchers; chapter and Studio-Creation cases are built here.
    Returns (None, None) if the source row no longer exists."""
    kind = kind or "video"
    label = _production_creation_source_label(conn, kind, video_id, book_id, section_id, transformation_id)
    if label is None:
        return None, None

    if kind == "video":
        return label, _video_full_breakdown_block(conn, video_id, limit)

    if kind == "video_section":
        s = conn.execute(
            "SELECT vs.*, v.title AS parent_title FROM video_sections vs "
            "JOIN videos v ON v.video_id = vs.video_id WHERE vs.section_id = ?", (section_id,),
        ).fetchone()
        pts = conn.execute(
            "SELECT point_text FROM video_points WHERE section_id = ? ORDER BY point_id", (section_id,)
        ).fetchall()
        points_text = "\n".join(f"- {p['point_text']}" for p in pts)
        content = f"Title: {s['parent_title']}\n\nChapter: {s['section_title']}\nSummary: {s['summary']}\nPoints:\n{points_text}"
        return label, content[:limit]

    if kind == "book":
        b = conn.execute("SELECT title, summary, full_text FROM books WHERE book_id = ?", (book_id,)).fetchone()
        content = f"Title: {b['title']}\n\n{(b['summary'] or '')}\n\n{(b['full_text'] or '')}".strip()
        return label, content[:limit]

    if kind == "book_section":
        s = conn.execute(
            "SELECT bs.*, b.title AS parent_title FROM book_sections bs "
            "JOIN books b ON b.book_id = bs.book_id WHERE bs.section_id = ?", (section_id,),
        ).fetchone()
        pts = conn.execute(
            "SELECT point_text FROM book_points WHERE section_id = ? ORDER BY point_id", (section_id,)
        ).fetchall()
        points_text = "\n".join(f"- {p['point_text']}" for p in pts)
        content = f"Title: {s['parent_title']}\n\nChapter: {s['section_title']}\nSummary: {s['summary']}\nPoints:\n{points_text}"
        return label, content[:limit]

    # 'creation' — an existing Studio Creation's already-generated text
    t = conn.execute(
        "SELECT generated_title, generated_text FROM transformations WHERE transformation_id = ?", (transformation_id,)
    ).fetchone()
    content = f"Title: {label}\n\n{t['generated_text']}"
    return label, content[:limit]


def _production_profile_fingerprint(conn, profile_id):
    """Returns {numeric: {attr: row}, categorical: {attr: [rows]}, n_videos_analysed}
    for a ProductionSpec profile — same tables production_profile_detail() reads."""
    profile = conn.execute("SELECT n_videos_analysed FROM style_profiles WHERE profile_id = ?", (profile_id,)).fetchone()
    numeric_rows = conn.execute(
        "SELECT attribute, mean_val, std_val, min_val, max_val, median_val FROM profile_fingerprint_numeric WHERE profile_id = ?",
        (profile_id,),
    ).fetchall()
    categorical_rows = conn.execute(
        "SELECT attribute, value, share_pct FROM profile_fingerprint_categorical WHERE profile_id = ? ORDER BY share_pct DESC",
        (profile_id,),
    ).fetchall()
    categorical = {}
    for r in categorical_rows:
        categorical.setdefault(r["attribute"], []).append({"value": r["value"], "share_pct": r["share_pct"]})
    return {
        "numeric": {r["attribute"]: dict(r) for r in numeric_rows},
        "categorical": categorical,
        "n_videos_analysed": profile["n_videos_analysed"] if profile else 0,
    }


def _format_profile_prompt_block(conn, format_profile_id):
    """The P+S axes for a chosen format profile, as prompt constraints.
    Returns "" when no profile is chosen — the spec is still valid without
    one, it just leaves the format to whoever executes it."""
    if not format_profile_id:
        return ""
    prof = conn.execute(
        "SELECT profile_code, handle, status, n_inputs_analysed FROM format_profiles "
        "WHERE format_profile_id = ?", (format_profile_id,),
    ).fetchone()
    if not prof:
        return ""
    labels = {axis: label for axis, label, _ in FORMAT_AXES}
    rows = conn.execute(
        "SELECT axis, value, note, source FROM format_profile_attributes WHERE format_profile_id = ?",
        (format_profile_id,),
    ).fetchall()
    by_axis = {r["axis"]: r for r in rows}
    lines = []
    for axis, label, _ in FORMAT_AXES:
        r = by_axis.get(axis)
        if not r:
            continue
        # Say which readings are measured and which are still asserted, so the
        # model is not asked to treat a guess as a specification.
        mark = "" if r["source"] == "classified" else " [asserted, not yet measured]"
        lines.append(f"- {label}: {r['value']}{mark}")
    if not lines:
        return ""
    note = "" if prof["status"] == "confirmed" else ", still provisional"
    return FORMAT_PROFILE_PROMPT_BLOCK.format(
        handle=prof["handle"] or prof["profile_code"], profile_code=prof["profile_code"],
        n_inputs=prof["n_inputs_analysed"] or 0, preliminary_note=note,
        axis_lines="\n".join(lines),
    )


def _build_production_creation_prompt(conn, content_block, style_profile_id, production_profile_id,
                                      format_profile_id=None):
    prod_fp = _production_profile_fingerprint(conn, production_profile_id)
    style_row = conn.execute(
        """SELECT p.overview, c.channel_name FROM style_profiles p JOIN channels c ON c.channel_id = p.channel_id
           WHERE p.profile_id = ?""", (style_profile_id,),
    ).fetchone()
    prod_row = conn.execute(
        """SELECT c.channel_name FROM style_profiles p JOIN channels c ON c.channel_id = p.channel_id
           WHERE p.profile_id = ?""", (production_profile_id,),
    ).fetchone()

    avg_shot_length = (prod_fp["numeric"].get("avg_shot_length_sec") or {}).get("mean_val") or 2.5
    shot_mix_summary = ", ".join(
        f"{label} {prod_fp['numeric'][attr]['mean_val']:.0f}%"
        for attr, label in PRODUCTION_CREATION_SHOT_MIX_FIELDS if attr in prod_fp["numeric"]
    ) or "no shot-mix data yet"
    style_overview = (style_row["overview"] or "").strip() if style_row else ""

    return PRODUCTION_CREATION_PROMPT.format(
        content_block=content_block,
        production_channel_name=prod_row["channel_name"] if prod_row else "the production profile",
        n_analysed=prod_fp["n_videos_analysed"],
        avg_shot_length=avg_shot_length,
        shot_mix_summary=shot_mix_summary,
        style_channel_name=style_row["channel_name"] if style_row else "the style profile",
        style_overview_suffix=f" — {style_overview[:300]}" if style_overview else "",
        format_block=_format_profile_prompt_block(conn, format_profile_id),
    )


def _generate_production_creation(creation_id):
    """Runs the LLM generation for a just-created production_spec_creations
    row and saves the result. Runtime/shot-count totals are computed here
    from the beats, not taken from the LLM, so they can't drift from what's
    actually in beats_json."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM production_spec_creations WHERE creation_id = ?", (creation_id,)).fetchone()
    if not row:
        conn.close()
        return
    try:
        label, content_block = _production_creation_source_content(
            conn, row["source_kind"], row["source_video_id"], row["source_book_id"],
            row["source_section_id"], row["source_transformation_id"],
        )
        if content_block is None:
            raise ValueError("Source content not found — it may have been deleted since this creation was started.")
        prompt = _build_production_creation_prompt(
            conn, content_block, row["style_profile_id"], row["production_profile_id"],
            row["format_profile_id"],
        )
        data = generate_json(prompt, max_tokens=4096)
        beats = data.get("beats") or []
        runtime = sum(
            ((b.get("duration_sec_min") or 0) + (b.get("duration_sec_max") or 0)) / 2
            for b in beats
        )
        shot_min = sum(b.get("shot_count_min") or 0 for b in beats)
        shot_max = sum(b.get("shot_count_max") or 0 for b in beats)
        conn.execute(
            """UPDATE production_spec_creations SET
               title = COALESCE(NULLIF(title, ''), ?), dek = ?, beats_json = ?, production_notes_json = ?,
               target_runtime_sec = ?, target_shot_count_min = ?, target_shot_count_max = ?,
               status = 'generated', generation_error = NULL, generated_at = datetime('now')
               WHERE creation_id = ?""",
            (
                data.get("title"), data.get("dek"), json.dumps(beats), json.dumps(data.get("production_notes") or []),
                runtime, shot_min, shot_max, creation_id,
            ),
        )
        conn.commit()
    except Exception as e:
        conn.execute(
            "UPDATE production_spec_creations SET status = 'failed', generation_error = ? WHERE creation_id = ?",
            (str(e), creation_id),
        )
        conn.commit()
    finally:
        conn.close()


PRODUCTION_CREATION_SOURCE_KIND_LABELS = {
    "video": "Video", "video_section": "Video chapter",
    "book": "Book", "book_section": "Book chapter", "creation": "Studio Creation",
}


@app.route("/production/spec-creations")
def production_spec_creations_list():
    conn = get_conn()
    rows = conn.execute(
        """SELECT c.creation_id, c.title, c.view_url, c.created_at, c.status,
                  c.source_kind, c.source_video_id, c.source_book_id, c.source_section_id, c.source_transformation_id,
                  sp.profile_code AS style_profile_code,
                  pp.profile_code AS production_profile_code
           FROM production_spec_creations c
           LEFT JOIN style_profiles sp ON sp.profile_id = c.style_profile_id
           LEFT JOIN style_profiles pp ON pp.profile_id = c.production_profile_id
           ORDER BY c.created_at DESC"""
    ).fetchall()
    rows = [
        dict(r, source_title=_production_creation_source_label(
            conn, r["source_kind"], r["source_video_id"], r["source_book_id"],
            r["source_section_id"], r["source_transformation_id"],
        ), source_kind_label=PRODUCTION_CREATION_SOURCE_KIND_LABELS.get(r["source_kind"] or "video", "Video"))
        for r in rows
    ]
    conn.close()
    return render_template("production_spec_creations_list.html", active="production-spec-creations", rows=rows)


@app.route("/production/transform", methods=["GET", "POST"])
def production_transform_form():
    """Production Transform — take something already written in the Library
    and cut it to a production profile.

    The Library's /transform answers "what should this say, in whose voice".
    This answers the next question: "how should it be SHOT". Same shape, one
    step further down the pipeline, which is why it reads as a sibling of
    that page rather than a different kind of thing.

    The voice profile is NOT asked for here. A Studio Creation was already
    generated toward a target profile, so asking again would let you pick a
    voice the text was never written in — a choice with no right answer and
    a silent way to produce an incoherent spec.

    POST, not GET: generating costs a real Claude call, so a page load,
    refresh or shared link must not be able to trigger one."""
    conn = get_conn()

    if request.method == "POST":
        transformation_id = request.form.get("transformation_id", type=int)
        production_profile_id = request.form.get("production_profile_id", type=int)
        format_profile_id = request.form.get("format_profile_id", type=int) or None

        creation = None
        if transformation_id:
            creation = conn.execute(
                """SELECT t.transformation_id, t.generated_title, t.target_profile_id
                   FROM transformations t WHERE t.transformation_id = ?""",
                (transformation_id,),
            ).fetchone()
        if not creation:
            flash("Pick a Library creation to transform.")
        elif not production_profile_id:
            flash("Pick a production profile to cut it to.")
        else:
            cur = conn.execute(
                """INSERT INTO production_spec_creations
                   (title, source_kind, source_transformation_id, style_profile_id,
                    production_profile_id, format_profile_id)
                   VALUES (?, 'creation', ?, ?, ?, ?)""",
                (creation["generated_title"], creation["transformation_id"],
                 creation["target_profile_id"], production_profile_id, format_profile_id),
            )
            conn.commit()
            creation_id = cur.lastrowid
            conn.close()
            _generate_production_creation(creation_id)
            conn = get_conn()
            status = conn.execute(
                "SELECT status, generation_error FROM production_spec_creations WHERE creation_id = ?",
                (creation_id,),
            ).fetchone()
            conn.close()
            if status and status["status"] == "failed":
                flash(f"Generation failed: {status['generation_error']}")
            return redirect(url_for("production_spec_creation_detail", creation_id=creation_id))

    creations = conn.execute(
        """SELECT t.transformation_id, t.generated_title, t.generated_at,
                  p.profile_code, c.channel_name
           FROM transformations t
           LEFT JOIN style_profiles p ON p.profile_id = t.target_profile_id
           LEFT JOIN channels c ON c.channel_id = p.channel_id
           ORDER BY t.generated_at DESC LIMIT 300"""
    ).fetchall()
    production_profiles = conn.execute(
        """SELECT p.profile_id, p.profile_code, c.channel_name,
                  (SELECT COUNT(*) FROM production_spec_inputs i
                   WHERE i.channel_id = p.channel_id AND i.status = 'classified') AS n_inputs
           FROM style_profiles p JOIN channels c ON c.channel_id = p.channel_id
           WHERE p.media_type = 'ProductionSpec'
           ORDER BY p.profile_code"""
    ).fetchall()
    format_profiles = conn.execute(
        """SELECT format_profile_id, profile_code, handle, status, n_inputs_analysed
           FROM format_profiles ORDER BY CAST(SUBSTR(profile_code, 5) AS INTEGER)"""
    ).fetchall()
    conn.close()
    return render_template(
        "production_transform_form.html", active="production-transform",
        creations=creations, production_profiles=production_profiles, format_profiles=format_profiles,
    )


@app.route("/production/spec-creations/new", methods=["GET", "POST"])
def production_spec_creation_create():
    conn = get_conn()
    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        source = request.form.get("source") or ""
        style_profile_id = request.form.get("style_profile_id") or None
        production_profile_id = request.form.get("production_profile_id") or None
        view_url = (request.form.get("view_url") or "").strip() or None
        source_kind, _, source_id = source.partition(":")
        if source_kind not in ("video", "video_section", "book", "book_section", "creation") or not source_id:
            source_kind = None
        if not (source_kind and style_profile_id and production_profile_id):
            flash("Source, profile style, and production profile are all required to generate a spec creation.")
        else:
            source_video_id = source_id if source_kind == "video" else None
            source_book_id = source_id if source_kind == "book" else None
            source_section_id = source_id if source_kind in ("video_section", "book_section") else None
            source_transformation_id = source_id if source_kind == "creation" else None
            cur = conn.execute(
                "INSERT INTO production_spec_creations "
                "(title, source_kind, source_video_id, source_book_id, source_section_id, source_transformation_id, "
                "style_profile_id, production_profile_id, view_url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, source_kind, source_video_id, source_book_id, source_section_id, source_transformation_id,
                 style_profile_id, production_profile_id, view_url),
            )
            conn.commit()
            creation_id = cur.lastrowid
            conn.close()
            _generate_production_creation(creation_id)
            conn = get_conn()
            status = conn.execute(
                "SELECT status, generation_error FROM production_spec_creations WHERE creation_id = ?", (creation_id,)
            ).fetchone()
            conn.close()
            if status and status["status"] == "failed":
                flash(f"Generation failed: {status['generation_error']}")
            return redirect(url_for("production_spec_creation_detail", creation_id=creation_id))

    videos = conn.execute("SELECT video_id, title FROM videos ORDER BY ingested_at DESC LIMIT 500").fetchall()
    video_sections = conn.execute(
        """SELECT vs.section_id, vs.section_title, v.title AS parent_title FROM video_sections vs
           JOIN videos v ON v.video_id = vs.video_id
           ORDER BY v.ingested_at DESC, vs.section_number LIMIT 300"""
    ).fetchall()
    books = conn.execute("SELECT book_id, title FROM books ORDER BY ingested_at DESC LIMIT 200").fetchall()
    book_sections = conn.execute(
        """SELECT bs.section_id, bs.section_title, b.title AS parent_title FROM book_sections bs
           JOIN books b ON b.book_id = bs.book_id
           ORDER BY b.ingested_at DESC, bs.section_number LIMIT 300"""
    ).fetchall()
    creations = conn.execute(
        """SELECT t.transformation_id, t.generated_title, p.profile_code FROM transformations t
           LEFT JOIN style_profiles p ON p.profile_id = t.target_profile_id
           ORDER BY t.generated_at DESC LIMIT 200"""
    ).fetchall()
    style_profiles = conn.execute(
        """SELECT p.profile_id, p.profile_code, c.channel_name FROM style_profiles p
           JOIN channels c ON c.channel_id = p.channel_id
           WHERE p.media_type != 'ProductionSpec' OR p.media_type IS NULL
           ORDER BY p.profile_code"""
    ).fetchall()
    production_profiles = conn.execute(
        """SELECT p.profile_id, p.profile_code, c.channel_name FROM style_profiles p
           JOIN channels c ON c.channel_id = p.channel_id
           WHERE p.media_type = 'ProductionSpec' ORDER BY p.profile_code"""
    ).fetchall()
    conn.close()
    return render_template(
        "production_spec_creation_form.html", active="production-spec-creations",
        videos=videos, video_sections=video_sections, books=books, book_sections=book_sections,
        creations=creations, style_profiles=style_profiles, production_profiles=production_profiles,
    )


@app.route("/production/spec-creations/<int:creation_id>")
def production_spec_creation_detail(creation_id):
    conn = get_conn()
    row = conn.execute(
        """SELECT c.*, sp.profile_code AS style_profile_code, sc.channel_name AS style_channel_name,
                  pp.profile_code AS production_profile_code, pc.channel_name AS production_channel_name,
                  pp.n_videos_analysed AS production_n_analysed,
                  fp.profile_code AS format_profile_code, fp.handle AS format_handle,
                  fp.status AS format_status
           FROM production_spec_creations c
           LEFT JOIN style_profiles sp ON sp.profile_id = c.style_profile_id
           LEFT JOIN channels sc ON sc.channel_id = sp.channel_id
           LEFT JOIN style_profiles pp ON pp.profile_id = c.production_profile_id
           LEFT JOIN channels pc ON pc.channel_id = pp.channel_id
           LEFT JOIN format_profiles fp ON fp.format_profile_id = c.format_profile_id
           WHERE c.creation_id = ?""",
        (creation_id,),
    ).fetchone()
    if not row:
        conn.close()
        flash(f"No such production spec creation: {creation_id}")
        return redirect(url_for("production_spec_creations_list"))

    row = dict(row)
    row["source_title"] = _production_creation_source_label(
        conn, row["source_kind"], row["source_video_id"], row["source_book_id"],
        row["source_section_id"], row["source_transformation_id"],
    )
    row["source_kind_label"] = PRODUCTION_CREATION_SOURCE_KIND_LABELS.get(row["source_kind"] or "video", "Video")

    beats = json.loads(row["beats_json"]) if row["beats_json"] else []
    production_notes = json.loads(row["production_notes_json"]) if row["production_notes_json"] else []

    shot_mix = {}
    if row["production_profile_id"]:
        fp = _production_profile_fingerprint(conn, row["production_profile_id"])
        for attr, label in PRODUCTION_CREATION_SHOT_MIX_FIELDS:
            v = (fp["numeric"].get(attr) or {}).get("mean_val")
            if v is not None:
                shot_mix[label] = round(v, 1)
    conn.close()

    return render_template(
        "production_creation_detail.html", active="production-spec-creations",
        creation=row, beats=beats, production_notes=production_notes, shot_mix=shot_mix,
    )


@app.route("/production/spec-creations/<int:creation_id>/regenerate", methods=["POST"])
def production_spec_creation_regenerate(creation_id):
    _generate_production_creation(creation_id)
    conn = get_conn()
    status = conn.execute(
        "SELECT status, generation_error FROM production_spec_creations WHERE creation_id = ?", (creation_id,)
    ).fetchone()
    conn.close()
    if status and status["status"] == "failed":
        flash(f"Generation failed: {status['generation_error']}")
    return redirect(url_for("production_spec_creation_detail", creation_id=creation_id))


@app.route("/production/spec-creations/<int:creation_id>/delete", methods=["POST"])
def production_spec_creation_delete(creation_id):
    conn = get_conn()
    conn.execute("DELETE FROM production_spec_creations WHERE creation_id = ?", (creation_id,))
    conn.commit()
    conn.close()
    flash(f"Deleted production spec creation {creation_id}.")
    return redirect(url_for("production_spec_creations_list"))


@app.route("/books")
def books_list():
    conn = get_conn()
    rows = conn.execute(
        """SELECT b.book_id, b.title, b.author, b.subject, b.publication_year, b.word_count, b.is_read,
                  COALESCE(a.classified_by, 'pending') AS classified_by,
                  (SELECT COUNT(*) FROM book_examples e WHERE e.book_id = b.book_id) AS n_examples
           FROM books b LEFT JOIN book_attributes a ON a.book_id = b.book_id
           ORDER BY b.ingested_at DESC"""
    ).fetchall()
    conn.close()
    return render_template("books_list.html", active="books", books=rows)


@app.route("/books/<int:book_id>/toggle-read", methods=["POST"])
def book_toggle_read(book_id):
    conn = get_conn()
    book = conn.execute("SELECT is_read FROM books WHERE book_id = ?", (book_id,)).fetchone()
    if not book:
        conn.close()
        flash(f"No such book: {book_id}")
        return redirect(url_for("books_list"))
    conn.execute("UPDATE books SET is_read = ? WHERE book_id = ?", (0 if book["is_read"] else 1, book_id))
    conn.commit()
    conn.close()
    return redirect(request.referrer or url_for("books_list"))


@app.route("/books/<int:book_id>/page/<int:page_num>.png")
def book_page_image(book_id, page_num):
    # Screenshots matched to examples live on the same persistent disk as the
    # DB (see match_book_screenshots.py) — this is the primary location and
    # works the same in local dev and production.
    image_path = BOOK_PAGES_DIR / str(book_id) / f"page_{page_num:04d}.png"
    if image_path.is_file():
        return send_file(image_path, mimetype="image/png")

    # Fall back to the older convention (a "pages" dir next to the book's own
    # local file), which only ever resolves when this is run on the same
    # machine the original PDF was uploaded from.
    conn = get_conn()
    book = conn.execute("SELECT source_file_path FROM books WHERE book_id = ?", (book_id,)).fetchone()
    conn.close()
    if not book or not book["source_file_path"]:
        abort(404)
    legacy_path = os.path.join(os.path.dirname(book["source_file_path"]), "pages", f"page_{page_num:04d}.png")
    if not os.path.isfile(legacy_path):
        abort(404)
    return send_file(legacy_path, mimetype="image/png")


@app.route("/api/books/<int:book_id>/examples")
def api_book_examples(book_id):
    """Lets match_book_screenshots.py (run locally, where the original PDF
    and its rendered page images live) fetch a book's examples from the live
    engine so it can match each one to a page number without needing direct
    DB access."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    conn = get_conn()
    book = conn.execute("SELECT book_id, title FROM books WHERE book_id = ?", (book_id,)).fetchone()
    if not book:
        conn.close()
        return jsonify({"ok": False, "error": "no such book"}), 404
    rows = conn.execute(
        "SELECT example_id, example_title, example_text, reinforces_point, screenshot_page_num "
        "FROM book_examples WHERE book_id = ? ORDER BY example_id", (book_id,)
    ).fetchall()
    conn.close()
    return jsonify({"ok": True, "book_id": book_id, "title": book["title"],
                     "examples": [dict(r) for r in rows]})


@app.route("/api/books/<int:book_id>/examples/<int:example_id>/screenshot", methods=["POST"])
def api_set_example_screenshot(book_id, example_id):
    """Receives one matched page image from match_book_screenshots.py and
    stores it on the persistent disk + records the page number against the
    example, so book_detail can render it inline."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    data = request.get_json(silent=True) or {}
    page_num = data.get("page_num")
    image_b64 = data.get("image_base64")
    if not page_num or not image_b64:
        return jsonify({"ok": False, "error": "'page_num' and 'image_base64' are required"}), 400

    conn = get_conn()
    ex = conn.execute(
        "SELECT example_id FROM book_examples WHERE example_id = ? AND book_id = ?", (example_id, book_id)
    ).fetchone()
    if not ex:
        conn.close()
        return jsonify({"ok": False, "error": "no such example on this book"}), 404

    book_dir = BOOK_PAGES_DIR / str(book_id)
    book_dir.mkdir(parents=True, exist_ok=True)
    image_path = book_dir / f"page_{int(page_num):04d}.png"
    image_path.write_bytes(base64.b64decode(image_b64))

    conn.execute("UPDATE book_examples SET screenshot_page_num = ? WHERE example_id = ?", (int(page_num), example_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "book_id": book_id, "example_id": example_id, "page_num": page_num})


@app.route("/api/books")
def api_books_list():
    """Lets local sync scripts (upload_book_pdfs.py, match_book_screenshots.py,
    and the transcriber's auto-finish poller) look up every book's id, title,
    and readiness without direct DB access — has_pdf so upload can skip what's
    already stored, classified so the screenshot matcher (which needs
    book_examples to exist) knows when a freshly-ingested book is ready."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    conn = get_conn()
    rows = conn.execute(
        "SELECT b.book_id, b.title, b.author, a.classified_by "
        "FROM books b LEFT JOIN book_attributes a ON a.book_id = b.book_id ORDER BY b.book_id"
    ).fetchall()
    conn.close()
    books = []
    for r in rows:
        d = {"book_id": r["book_id"], "title": r["title"], "author": r["author"]}
        d["has_pdf"] = (BOOK_FILES_DIR / f"{r['book_id']}.pdf").is_file()
        d["classified"] = r["classified_by"] not in (None, "pending", "needs_review")
        books.append(d)
    return jsonify({"ok": True, "books": books})


@app.route("/api/books/<int:book_id>/pdf", methods=["POST"])
def api_set_book_pdf(book_id):
    """Receives the source PDF from upload_book_pdfs.py (run locally, where
    the PPE Books desktop folder lives) and stores it on the persistent disk
    so it survives deploys and can be served/downloaded from the live site."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    data = request.get_json(silent=True) or {}
    pdf_b64 = data.get("pdf_base64")
    if not pdf_b64:
        return jsonify({"ok": False, "error": "'pdf_base64' is required"}), 400

    conn = get_conn()
    book = conn.execute("SELECT book_id FROM books WHERE book_id = ?", (book_id,)).fetchone()
    conn.close()
    if not book:
        return jsonify({"ok": False, "error": "no such book"}), 404

    BOOK_FILES_DIR.mkdir(parents=True, exist_ok=True)
    (BOOK_FILES_DIR / f"{book_id}.pdf").write_bytes(base64.b64decode(pdf_b64))
    return jsonify({"ok": True, "book_id": book_id})


@app.route("/books/<int:book_id>/pdf")
def book_pdf(book_id):
    pdf_path = BOOK_FILES_DIR / f"{book_id}.pdf"
    if not pdf_path.is_file():
        abort(404)
    return send_file(pdf_path, mimetype="application/pdf")


def _source_cards(conn):
    cards = []
    for mt in ATTRIBUTE_MEDIA_TYPES:
        if not mt["implemented"]:
            cards.append({**mt, "item_count": 0, "with_file_count": 0})
            continue
        if mt["slug"] == "books":
            total = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
            with_file = sum(1 for _ in BOOK_FILES_DIR.glob("*.pdf")) if BOOK_FILES_DIR.is_dir() else 0
        elif mt["slug"] in ("instagram", "youtube"):
            platform = "Instagram" if mt["slug"] == "instagram" else "YouTube"
            total = conn.execute(
                "SELECT COUNT(*) FROM videos v JOIN channels c ON c.channel_id = v.channel_id "
                "WHERE c.platform = ?", (platform,)
            ).fetchone()[0]
            with_file = conn.execute(
                "SELECT COUNT(*) FROM videos v JOIN channels c ON c.channel_id = v.channel_id "
                "WHERE c.platform = ? AND v.url IS NOT NULL AND v.url != ''", (platform,)
            ).fetchone()[0]
        else:
            total, with_file = 0, 0
        cards.append({**mt, "item_count": total, "with_file_count": with_file})
    return cards


@app.route("/sources")
def sources_page():
    conn = get_conn()
    cards = _source_cards(conn)
    conn.close()
    return render_template("sources.html", active="sources", cards=cards)


@app.route("/sources/<slug>")
def source_media_detail(slug):
    mt = next((m for m in ATTRIBUTE_MEDIA_TYPES if m["slug"] == slug), None)
    if not mt or not mt["implemented"]:
        flash(f"No such source category: {slug}")
        return redirect(url_for("sources_page"))

    conn = get_conn()
    rows = []
    if slug == "books":
        book_rows = conn.execute(
            "SELECT book_id, title, author, word_count, ingested_at, source_note "
            "FROM books ORDER BY ingested_at DESC"
        ).fetchall()
        for b in book_rows:
            has_pdf = (BOOK_FILES_DIR / f"{b['book_id']}.pdf").is_file()
            book_detail_url = url_for("book_detail", book_id=b["book_id"])
            example_rows = conn.execute(
                "SELECT example_id, example_title, example_text FROM book_examples "
                "WHERE book_id = ? ORDER BY example_id", (b["book_id"],),
            ).fetchall()
            term_rows = conn.execute(
                "SELECT term_id, term FROM book_terms WHERE book_id = ? ORDER BY term_id", (b["book_id"],),
            ).fetchall()
            rows.append({
                "author_or_channel": b["author"], "title": b["title"],
                "detail_url": book_detail_url,
                "word_count": b["word_count"], "ingested_at": b["ingested_at"],
                "file_url": url_for("book_pdf", book_id=b["book_id"]) if has_pdf else None,
                "file_label": "source PDF ↗", "source_text": b["source_note"],
                "examples": [
                    {"label": ex["example_title"] or _trim(ex["example_text"], 90),
                     "detail_url": f"{book_detail_url}#example-{ex['example_id']}"}
                    for ex in example_rows
                ],
                "terms": [
                    {"label": t["term"], "detail_url": f"{book_detail_url}#term-{t['term_id']}"}
                    for t in term_rows
                ],
            })
    else:  # instagram or youtube — same shape, filtered by platform
        platform = "Instagram" if slug == "instagram" else "YouTube"
        video_rows = conn.execute(
            "SELECT v.video_id, v.title, v.url, v.posted_at, v.duration_sec, c.channel_name "
            "FROM videos v JOIN channels c ON c.channel_id = v.channel_id "
            "WHERE c.platform = ? ORDER BY v.ingested_at DESC", (platform,)
        ).fetchall()
        for v in video_rows:
            rows.append({
                "author_or_channel": v["channel_name"], "title": v["title"],
                "detail_url": url_for("input_detail", video_id=v["video_id"]),
                "posted_at": v["posted_at"], "duration_label": _format_duration(v["duration_sec"]),
                "file_url": v["url"] or None, "file_label": "original post ↗", "source_text": None,
            })
    conn.close()
    return render_template("source_detail.html", active="sources", media=mt, rows=rows)


@app.route("/books/<int:book_id>")
def book_detail(book_id):
    conn = get_conn()
    book = conn.execute("SELECT * FROM books WHERE book_id = ?", (book_id,)).fetchone()
    if not book:
        conn.close()
        flash(f"No such book: {book_id}")
        return redirect(url_for("books_list"))
    attrs_row = conn.execute("SELECT * FROM book_attributes WHERE book_id = ?", (book_id,)).fetchone()

    section_rows = conn.execute(
        "SELECT * FROM book_sections WHERE book_id = ? ORDER BY section_number, section_id", (book_id,)
    ).fetchall()
    chapters = []
    for s in section_rows:
        points = conn.execute(
            "SELECT point_text FROM book_points WHERE section_id = ? ORDER BY point_id", (s["section_id"],)
        ).fetchall()
        terms = conn.execute(
            "SELECT term_id, term, definition FROM book_terms WHERE section_id = ? ORDER BY term_id", (s["section_id"],)
        ).fetchall()
        examples = conn.execute(
            "SELECT example_id, example_title, example_text, reinforces_point, screenshot_page_num "
            "FROM book_examples WHERE section_id = ? ORDER BY example_id",
            (s["section_id"],),
        ).fetchall()
        chapters.append({
            "section": s,
            "topics": [t.strip() for t in s["topics"].split(",")] if s["topics"] else [],
            "points": points, "terms": terms, "examples": examples,
        })

    # examples/points/terms not attributed to any registered section (e.g. from
    # an older flat classification) still need somewhere to show up
    unsectioned_examples = conn.execute(
        "SELECT example_id, example_title, example_text, reinforces_point, chapter_or_location, screenshot_page_num "
        "FROM book_examples WHERE book_id = ? AND section_id IS NULL ORDER BY example_id", (book_id,)
    ).fetchall()
    conn.close()

    attrs = dict(attrs_row) if attrs_row else {}
    needs_review = attrs.get("classified_by") == "needs_review"
    is_classified = attrs.get("classified_by") not in (None, "pending", "needs_review")
    attr_sections = []
    for name, fields in BOOK_ATTRIBUTE_SECTIONS:
        if not fields:
            continue
        attr_sections.append((name, [(BOOK_FIELD_LABELS[f], attrs.get(f)) for f in fields]))

    # Best-fit author profiles + macro-level fit scores, the book-side
    # counterpart to a video creation's profile ranking — only meaningful
    # once classified, since it correlates this book's own rubric values
    # against other book profiles' fingerprints. macro_fit uses the shared
    # cross-media macro names (SHARED_ATTRIBUTE_SECTIONS/
    # SHARED_BOOK_ATTRIBUTE_SECTIONS), so it's directly comparable to a
    # video creation's macro_fit — unlike attr_sections above, which uses
    # this book's own native BOOK_ATTRIBUTE_SECTIONS grouping.
    profile_scores = score_book_against_profiles(attrs) if is_classified else []
    macro_fit = None
    if profile_scores:
        macro_fit = _macro_subscore_from_breakdown(profile_scores[0]["breakdown"], _score_field_to_macro())

    source_file_url = None
    if book["source_file_path"]:
        source_file_url = "file://" + quote(book["source_file_path"], safe="/")
    has_pdf = (BOOK_FILES_DIR / f"{book_id}.pdf").is_file()

    return render_template(
        "book_detail.html", active="books", book=book, attr_sections=attr_sections,
        chapters=chapters, unsectioned_examples=unsectioned_examples, is_classified=is_classified,
        needs_review=needs_review, classification_error=attrs.get("classification_error"),
        profile_scores=profile_scores, macro_fit=macro_fit,
        source_file_url=source_file_url, has_pdf=has_pdf,
    )


@app.route("/books/<int:book_id>/quickview.json")
def book_quickview_json(book_id):
    """Book counterpart to input_quickview_json() — same flattened
    attributes/chapters/terms/examples shape, powering the same Library
    row quick-view panel for book rows."""
    conn = get_conn()
    book = conn.execute("SELECT book_id FROM books WHERE book_id = ?", (book_id,)).fetchone()
    if not book:
        conn.close()
        return jsonify({"ok": False, "error": "no such book"}), 404

    attrs_row = conn.execute("SELECT * FROM book_attributes WHERE book_id = ?", (book_id,)).fetchone()
    attrs = dict(attrs_row) if attrs_row else {}
    attributes = []
    for name, fields in BOOK_ATTRIBUTE_SECTIONS:
        if not fields:
            continue
        section_fields = []
        for f in fields:
            v = attrs.get(f)
            if v in (None, ""):
                continue
            if v == 1:
                v = "Yes"
            elif v == 0:
                v = "No"
            section_fields.append({"label": BOOK_FIELD_LABELS[f], "value": v})
        if section_fields:
            attributes.append({"section": name, "fields": section_fields})

    section_rows = conn.execute(
        "SELECT * FROM book_sections WHERE book_id = ? ORDER BY section_number, section_id", (book_id,)
    ).fetchall()
    chapters, all_terms, all_examples = [], [], []
    for s in section_rows:
        pts = conn.execute(
            "SELECT point_text FROM book_points WHERE section_id = ? ORDER BY point_id", (s["section_id"],)
        ).fetchall()
        trms = conn.execute(
            "SELECT term, definition FROM book_terms WHERE section_id = ? ORDER BY term_id", (s["section_id"],)
        ).fetchall()
        exs = conn.execute(
            "SELECT example_title, example_text, reinforces_point FROM book_examples "
            "WHERE section_id = ? ORDER BY example_id", (s["section_id"],)
        ).fetchall()
        chapters.append({
            "title": s["section_title"],
            "summary": s["summary"],
            "topics": [t.strip() for t in s["topics"].split(",")] if s["topics"] else [],
            "points": [p["point_text"] for p in pts],
        })
        all_terms.extend({"term": t["term"], "definition": t["definition"]} for t in trms)
        all_examples.extend(
            {"title": e["example_title"], "text": e["example_text"], "reinforces": e["reinforces_point"]}
            for e in exs
        )

    unsec_examples = conn.execute(
        "SELECT example_title, example_text, reinforces_point FROM book_examples "
        "WHERE book_id = ? AND section_id IS NULL ORDER BY example_id", (book_id,)
    ).fetchall()
    conn.close()
    all_examples.extend(
        {"title": e["example_title"], "text": e["example_text"], "reinforces": e["reinforces_point"]}
        for e in unsec_examples
    )

    return jsonify({"ok": True, "attributes": attributes, "chapters": chapters, "terms": all_terms, "examples": all_examples})


@app.route("/profiles/<code>")
def profile_detail(code):
    conn = get_conn()
    p = conn.execute(
        """SELECT p.*, c.channel_name, c.channel_id FROM style_profiles p
           JOIN channels c ON c.channel_id = p.channel_id
           WHERE p.profile_code = ?""",
        (code,),
    ).fetchone()
    if not p:
        conn.close()
        flash(f"No such profile: {code}")
        return redirect(url_for("profiles_list"))

    numeric = conn.execute(
        "SELECT * FROM profile_fingerprint_numeric WHERE profile_id=? ORDER BY attribute",
        (p["profile_id"],),
    ).fetchall()

    cat_rows = conn.execute(
        "SELECT * FROM profile_fingerprint_categorical WHERE profile_id=? ORDER BY attribute, share_pct DESC",
        (p["profile_id"],),
    ).fetchall()

    is_book = p["media_type"] == "Book"
    is_hybrid = p["media_type"] == "Hybrid"
    macros = []
    if not is_hybrid:
        macros = _book_profile_macro_coverage(conn, p["profile_id"]) if is_book else _profile_macro_coverage(conn, p["profile_id"])

    hybrid_sources = []
    if is_hybrid:
        hybrid_sources = conn.execute(
            """SELECT hs.weight_pct, sp.profile_code, c.channel_name
               FROM style_profile_hybrid_sources hs
               JOIN style_profiles sp ON sp.profile_id = hs.source_profile_id
               JOIN channels c ON c.channel_id = sp.channel_id
               WHERE hs.profile_id = ? ORDER BY hs.weight_pct DESC""",
            (p["profile_id"],),
        ).fetchall()

    videos, books = [], []
    book_examples, book_terms, pilot_book_titles = [], [], []
    if is_hybrid:
        pass
    elif is_book:
        book_rows = conn.execute(
            """SELECT b.book_id, b.title, b.word_count, b.summary, b.source_file_path
               FROM books b WHERE b.author = ? ORDER BY b.ingested_at DESC""",
            (p["channel_name"],),
        ).fetchall()
        books = [
            {
                "book_id": b["book_id"], "title": b["title"], "word_count": b["word_count"],
                "summary": b["summary"],
                "source_url": ("file://" + quote(b["source_file_path"], safe="/")) if b["source_file_path"] else None,
            }
            for b in book_rows
        ]
        pilot_book_ids = [b["book_id"] for b in books if b["book_id"] in BOOK_BREAKDOWN_PILOT_BOOK_IDS]
        pilot_book_titles = [b["title"] for b in books if b["book_id"] in BOOK_BREAKDOWN_PILOT_BOOK_IDS]
        if pilot_book_ids:
            placeholders = ",".join("?" * len(pilot_book_ids))
            book_examples = conn.execute(
                f"""SELECT example_id, example_title, example_text, reinforces_point, book_id
                    FROM book_examples WHERE book_id IN ({placeholders}) ORDER BY example_id""",
                pilot_book_ids,
            ).fetchall()
            book_terms = conn.execute(
                f"SELECT term, definition, book_id FROM book_terms WHERE book_id IN ({placeholders}) ORDER BY term_id",
                pilot_book_ids,
            ).fetchall()
    else:
        video_rows = conn.execute(
            """SELECT v.video_id, v.title, v.url, v.script, v.summary, v.duration_sec, a.word_count
               FROM videos v LEFT JOIN video_attributes a ON a.video_id = v.video_id
               WHERE v.channel_id=? ORDER BY v.ingested_at DESC, v.video_id DESC""",
            (p["channel_id"],),
        ).fetchall()
        videos = [
            {
                "video_id": v["video_id"], "title": v["title"], "url": v["url"],
                "summary": v["summary"] or _auto_summary(v["script"]),
                "has_written_summary": bool(v["summary"]),
                "script": v["script"],
                "script_preview": (v["script"][:180] + "…") if len(v["script"]) > 180 else v["script"],
                "word_count": v["word_count"],
                "duration_label": _format_duration(v["duration_sec"]),
            }
            for v in video_rows
        ]
    conn.close()

    categorical = {}
    for r in cat_rows:
        categorical.setdefault(r["attribute"], []).append(r)

    return render_template(
        "profile_detail.html", active="profiles", is_book=is_book, is_hybrid=is_hybrid, hybrid_sources=hybrid_sources,
        profile=p, numeric=numeric, categorical=categorical, macros=macros, videos=videos, books=books,
        book_examples=book_examples, book_terms=book_terms, pilot_book_titles=pilot_book_titles,
    )


def _get_profile_or_404(conn, code):
    return conn.execute(
        """SELECT p.*, c.channel_name, c.channel_id FROM style_profiles p
           JOIN channels c ON c.channel_id = p.channel_id
           WHERE p.profile_code = ?""",
        (code,),
    ).fetchone()


def _numeric_fp(conn, profile_id, attr):
    return conn.execute(
        "SELECT * FROM profile_fingerprint_numeric WHERE profile_id=? AND attribute=?",
        (profile_id, attr),
    ).fetchone()


def _top_categorical(conn, profile_id, attr, n=2):
    return conn.execute(
        "SELECT value, share_pct FROM profile_fingerprint_categorical "
        "WHERE profile_id=? AND attribute=? ORDER BY share_pct DESC LIMIT ?",
        (profile_id, attr, n),
    ).fetchall()


# (field_key, label, numeric_attr-or-None) — numeric_attr enables a declared-vs-observed delta
STYLE_CARD_FIELDS = [
    ("sentence_rhythm", "Sentence rhythm", "avg_sentence_len"),
    ("paragraph_shape", "Paragraph shape", "closing_paragraph_ratio"),
    ("vocabulary", "Vocabulary", "lexical_diversity"),
    ("opening_habits", "Opening habits", None),
    ("closing_habits", "Closing habits", None),
    ("media_use", "Additional media resource use", None),
    ("punctuation", "Punctuation", "punctuation_density"),
    ("formality", "Formality", "colloquialism_density"),
    ("humor", "Humor", "humor_marker_count"),
    ("hashtag_behavior", "Hashtag behavior", None),
]


def _style_card_ai_suggestion(conn, profile, field):
    """A starter description derived from the observed fingerprint — the
    'AI-derived' half of the style card. Returns None where the corpus
    genuinely has no signal for that field (e.g. hashtags aren't ingested)."""
    pid = profile["profile_id"]
    if field == "sentence_rhythm":
        avg = _numeric_fp(conn, pid, "avg_sentence_len")
        cv = _numeric_fp(conn, pid, "sentence_rhythm_cv")
        if avg:
            texture = "tight/consistent" if cv and cv["mean_val"] < 0.5 else "loose/varied"
            return f"~{avg['mean_val']:.1f} words/sentence on average, {texture} rhythm (CV {cv['mean_val']:.2f})." if cv else f"~{avg['mean_val']:.1f} words/sentence on average."
    elif field == "paragraph_shape":
        ratio = _numeric_fp(conn, pid, "closing_paragraph_ratio")
        beats = _numeric_fp(conn, pid, "beat_count")
        if ratio:
            shape = "ends smaller than it started" if ratio["mean_val"] < 0.9 else (
                "ends bigger than it started" if ratio["mean_val"] > 1.1 else "roughly even paragraph sizes")
            n_beats = f"~{beats['mean_val']:.1f} paragraphs, " if beats else ""
            return f"{n_beats}{shape} (closing paragraph is {ratio['mean_val']:.2f}x the mean paragraph length)."
    elif field == "vocabulary":
        ttr = _numeric_fp(conn, pid, "lexical_diversity")
        jargon = _numeric_fp(conn, pid, "jargon_density")
        if ttr:
            return f"Lexical diversity (TTR) {ttr['mean_val']:.2f}; jargon density {jargon['mean_val']:.3f}." if jargon else f"Lexical diversity (TTR) {ttr['mean_val']:.2f}."
    elif field == "opening_habits":
        top = _top_categorical(conn, pid, "hook_type", 2)
        if top:
            return "Most common hook type(s): " + ", ".join(f"{r['value']} ({r['share_pct']}%)" for r in top)
    elif field == "closing_habits":
        top = _top_categorical(conn, pid, "close_type", 2)
        if top:
            return "Most common close type(s): " + ", ".join(f"{r['value']} ({r['share_pct']}%)" for r in top)
    elif field == "media_use":
        row = conn.execute(
            """SELECT AVG(a.references_external_media) * 100 AS pct FROM video_attributes a
               JOIN videos v ON v.video_id = a.video_id WHERE v.channel_id = ?""",
            (profile["channel_id"],),
        ).fetchone()
        if row and row["pct"] is not None:
            return f"References another clip/video/footage in ~{row['pct']:.0f}% of scripts (text-level proxy only)."
    elif field == "punctuation":
        pd = _numeric_fp(conn, pid, "punctuation_density")
        em = _numeric_fp(conn, pid, "emdash_count")
        if pd:
            return f"~{pd['mean_val']:.1f} punctuation marks per 100 words; em-dash avg {em['mean_val']:.2f}/script." if em else f"~{pd['mean_val']:.1f} punctuation marks per 100 words."
    elif field == "formality":
        cd = _numeric_fp(conn, pid, "colloquialism_density")
        polish = _top_categorical(conn, pid, "script_polish", 1)
        if cd:
            polish_str = f"; dominant delivery: {polish[0]['value']} ({polish[0]['share_pct']}%)" if polish else ""
            return f"Colloquialism density {cd['mean_val']:.2f}/100w{polish_str}."
    elif field == "humor":
        hm = _numeric_fp(conn, pid, "humor_marker_count")
        if hm:
            return f"Humor-marker count avg {hm['mean_val']:.2f}/script (coarse joke/exclamation proxy, not true humor detection)."
    elif field == "hashtag_behavior":
        return None  # genuinely no data: captions/hashtags aren't part of the ingested corpus
    return None


@app.route("/profiles/<code>/style-card", methods=["GET"])
def style_card(code):
    conn = get_conn()
    profile = _get_profile_or_404(conn, code)
    if not profile:
        conn.close()
        flash(f"No such profile: {code}")
        return redirect(url_for("profiles_list"))

    saved = {
        r["field"]: r for r in conn.execute(
            "SELECT * FROM profile_style_card WHERE profile_id=? AND constraint_type IS NULL",
            (profile["profile_id"],),
        )
    }
    negative_space = conn.execute(
        "SELECT * FROM profile_style_card WHERE profile_id=? AND constraint_type IS NOT NULL ORDER BY constraint_type, id",
        (profile["profile_id"],),
    ).fetchall()

    card_rows = []
    for field, label, numeric_attr in STYLE_CARD_FIELDS:
        row = saved.get(field)
        observed = _numeric_fp(conn, profile["profile_id"], numeric_attr) if numeric_attr else None
        delta = None
        if row and numeric_attr and observed:
            try:
                declared_num = float(row["declared_value"])
                delta = round(declared_num - observed["mean_val"], 3)
            except ValueError:
                delta = None
        card_rows.append({
            "field": field, "label": label, "numeric_attr": numeric_attr,
            "declared_value": row["declared_value"] if row else "",
            "ai_suggestion": _style_card_ai_suggestion(conn, profile, field),
            "observed_mean": observed["mean_val"] if observed else None,
            "delta": delta,
        })
    conn.close()

    return render_template(
        "style_card.html", active="profiles",
        profile=profile, card_rows=card_rows, negative_space=negative_space,
    )


@app.route("/profiles/<code>/style-card/save", methods=["POST"])
def style_card_save(code):
    conn = get_conn()
    profile = _get_profile_or_404(conn, code)
    if not profile:
        conn.close()
        flash(f"No such profile: {code}")
        return redirect(url_for("profiles_list"))

    field = request.form.get("field", "")
    declared_value = request.form.get("declared_value", "").strip()
    numeric_attr = request.form.get("numeric_attr") or None
    valid_fields = {f for f, _, _ in STYLE_CARD_FIELDS}
    if field not in valid_fields:
        conn.close()
        flash("Unknown style card field.")
        return redirect(url_for("style_card", code=code))

    existing = conn.execute(
        "SELECT id FROM profile_style_card WHERE profile_id=? AND field=? AND constraint_type IS NULL",
        (profile["profile_id"], field),
    ).fetchone()
    if not declared_value:
        if existing:
            conn.execute("DELETE FROM profile_style_card WHERE id=?", (existing["id"],))
    elif existing:
        conn.execute(
            "UPDATE profile_style_card SET declared_value=?, numeric_attr=?, updated_at=datetime('now') WHERE id=?",
            (declared_value, numeric_attr, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO profile_style_card (profile_id, field, declared_value, numeric_attr) VALUES (?, ?, ?, ?)",
            (profile["profile_id"], field, declared_value, numeric_attr),
        )
    conn.commit()
    conn.close()
    return redirect(url_for("style_card", code=code))


@app.route("/profiles/<code>/style-card/negative-space/add", methods=["POST"])
def negative_space_add(code):
    conn = get_conn()
    profile = _get_profile_or_404(conn, code)
    if not profile:
        conn.close()
        flash(f"No such profile: {code}")
        return redirect(url_for("profiles_list"))

    constraint_type = request.form.get("constraint_type", "")
    value = request.form.get("value", "").strip()
    if constraint_type in ("banned_word", "banned_tone", "banned_format") and value:
        conn.execute(
            "INSERT INTO profile_style_card (profile_id, field, declared_value, constraint_type) "
            "VALUES (?, 'negative_space', ?, ?)",
            (profile["profile_id"], value, constraint_type),
        )
        conn.commit()
    conn.close()
    return redirect(url_for("style_card", code=code))


@app.route("/profiles/<code>/style-card/negative-space/<int:row_id>/delete", methods=["POST"])
def negative_space_delete(code, row_id):
    conn = get_conn()
    conn.execute("DELETE FROM profile_style_card WHERE id=? AND constraint_type IS NOT NULL", (row_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("style_card", code=code))


# --- Machine-facing style card API (for Hermes/Telegram, key-protected) ---
# Deliberately separate from the browser routes above rather than adding
# auth to them: the website itself still has no login for a person clicking
# Save, and gating those routes would break normal browser use. These two
# routes give an automated caller the same read/write ability, guarded by
# the same X-Ingest-Key convention as the rest of /api/*.

@app.route("/api/profiles/<code>/style-card", methods=["GET"])
def api_style_card_get(code):
    """Read-only: current style card fields + negative-space entries, as
    JSON, so a caller can show the user what's there before overwriting it."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    conn = get_conn()
    profile = _get_profile_or_404(conn, code)
    if not profile:
        conn.close()
        return jsonify({"ok": False, "error": f"no such profile: {code}"}), 404
    fields = conn.execute(
        "SELECT field, declared_value, numeric_attr FROM profile_style_card WHERE profile_id=? AND constraint_type IS NULL",
        (profile["profile_id"],),
    ).fetchall()
    negative_space = conn.execute(
        "SELECT id, constraint_type, declared_value FROM profile_style_card WHERE profile_id=? AND constraint_type IS NOT NULL ORDER BY constraint_type, id",
        (profile["profile_id"],),
    ).fetchall()
    conn.close()
    return jsonify({
        "ok": True,
        "profile_code": code,
        "fields": [dict(f) for f in fields],
        "negative_space": [dict(r) for r in negative_space],
    })


@app.route("/api/profiles/<code>/style-card/save", methods=["POST"])
def api_style_card_save(code):
    """Write one style card field. Same upsert logic as the browser's
    style_card_save route: an empty declared_value deletes the field."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    conn = get_conn()
    profile = _get_profile_or_404(conn, code)
    if not profile:
        conn.close()
        return jsonify({"ok": False, "error": f"no such profile: {code}"}), 404

    payload = request.get_json(silent=True) or {}
    field = request.form.get("field") or payload.get("field") or ""
    declared_value = (request.form.get("declared_value") or payload.get("declared_value") or "").strip()
    numeric_attr = request.form.get("numeric_attr") or payload.get("numeric_attr") or None
    valid_fields = {f for f, _, _ in STYLE_CARD_FIELDS}
    if field not in valid_fields:
        conn.close()
        return jsonify({"ok": False, "error": f"unknown style card field: {field}", "valid_fields": sorted(valid_fields)}), 400

    existing = conn.execute(
        "SELECT id FROM profile_style_card WHERE profile_id=? AND field=? AND constraint_type IS NULL",
        (profile["profile_id"], field),
    ).fetchone()
    if not declared_value:
        if existing:
            conn.execute("DELETE FROM profile_style_card WHERE id=?", (existing["id"],))
        action = "deleted"
    elif existing:
        conn.execute(
            "UPDATE profile_style_card SET declared_value=?, numeric_attr=?, updated_at=datetime('now') WHERE id=?",
            (declared_value, numeric_attr, existing["id"]),
        )
        action = "updated"
    else:
        conn.execute(
            "INSERT INTO profile_style_card (profile_id, field, declared_value, numeric_attr) VALUES (?, ?, ?, ?)",
            (profile["profile_id"], field, declared_value, numeric_attr),
        )
        action = "created"
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "profile_code": code, "field": field, "action": action, "declared_value": declared_value})


# --- Machine-facing video title API (for Hermes/Telegram, key-protected) ---
# Same rationale as the style card API above: there's no browser-side title
# edit UI yet, so this gives an automated caller a safe, auditable way to
# rename a video instead of touching the videos table directly.

@app.route("/api/videos/<int:video_id>/title", methods=["GET"])
def api_video_title_get(video_id):
    """Read-only: current title, so a caller can show the user what's there
    before overwriting it."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    conn = get_conn()
    video = conn.execute("SELECT video_id, title FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    conn.close()
    if not video:
        return jsonify({"ok": False, "error": f"no such video: {video_id}"}), 404
    return jsonify({"ok": True, "video_id": video_id, "title": video["title"]})


@app.route("/api/videos/<int:video_id>/title/save", methods=["POST"])
def api_video_title_save(video_id):
    """Write a video's title. Requires a non-empty new_title — unlike the
    style card fields, a video can't be titleless, so this never deletes."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)
    conn = get_conn()
    video = conn.execute("SELECT video_id, title FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    if not video:
        conn.close()
        return jsonify({"ok": False, "error": f"no such video: {video_id}"}), 404

    payload = request.get_json(silent=True) or {}
    new_title = (request.form.get("new_title") or payload.get("new_title") or "").strip()
    if not new_title:
        conn.close()
        return jsonify({"ok": False, "error": "'new_title' is required and cannot be blank"}), 400

    old_title = video["title"]
    conn.execute("UPDATE videos SET title = ? WHERE video_id = ?", (new_title, video_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "video_id": video_id, "old_title": old_title, "title": new_title})


def _snapshot_fingerprint_json(conn, profile_id):
    numeric = {
        r["attribute"]: r["mean_val"]
        for r in conn.execute("SELECT attribute, mean_val FROM profile_fingerprint_numeric WHERE profile_id=?", (profile_id,))
    }
    categorical = {}
    for r in conn.execute(
        "SELECT attribute, value, share_pct FROM profile_fingerprint_categorical WHERE profile_id=?", (profile_id,)
    ):
        categorical.setdefault(r["attribute"], {})[r["value"]] = r["share_pct"]
    return json.dumps({"numeric": numeric, "categorical": categorical})


@app.route("/profiles/<code>/drift", methods=["GET"])
def profile_drift(code):
    conn = get_conn()
    profile = _get_profile_or_404(conn, code)
    if not profile:
        conn.close()
        flash(f"No such profile: {code}")
        return redirect(url_for("profiles_list"))

    snapshots = conn.execute(
        "SELECT * FROM profile_fingerprint_snapshots WHERE profile_id=? ORDER BY snapshotted_at DESC",
        (profile["profile_id"],),
    ).fetchall()

    compare_id = request.args.get("snapshot_id", type=int)
    numeric_drift, categorical_drift, compared = [], [], None
    if compare_id:
        compared = next((s for s in snapshots if s["snapshot_id"] == compare_id), None)
        if compared:
            old = json.loads(compared["fingerprint_json"])
            current_json = json.loads(_snapshot_fingerprint_json(conn, profile["profile_id"]))
            for attr, old_val in sorted(old["numeric"].items()):
                cur_val = current_json["numeric"].get(attr)
                if cur_val is None or old_val is None:
                    continue
                delta = round(cur_val - old_val, 3)
                pct = round(delta / abs(old_val) * 100, 1) if old_val else None
                numeric_drift.append({"attribute": attr, "old": old_val, "current": cur_val, "delta": delta, "pct": pct})
            for attr, old_dist in sorted(old["categorical"].items()):
                cur_dist = current_json["categorical"].get(attr, {})
                values = set(old_dist) | set(cur_dist)
                tvd = sum(abs(old_dist.get(v, 0) - cur_dist.get(v, 0)) for v in values) / 2
                categorical_drift.append({"attribute": attr, "shift_pct": round(tvd, 1)})
            categorical_drift.sort(key=lambda r: -r["shift_pct"])
    conn.close()

    return render_template(
        "profile_drift.html", active="profiles",
        profile=profile, snapshots=snapshots, compared=compared,
        numeric_drift=numeric_drift, categorical_drift=categorical_drift,
    )


@app.route("/profiles/<code>/drift/snapshot", methods=["POST"])
def profile_drift_snapshot(code):
    conn = get_conn()
    profile = _get_profile_or_404(conn, code)
    if not profile:
        conn.close()
        flash(f"No such profile: {code}")
        return redirect(url_for("profiles_list"))

    label = request.form.get("label", "").strip() or "snapshot"
    fp_json = _snapshot_fingerprint_json(conn, profile["profile_id"])
    conn.execute(
        "INSERT INTO profile_fingerprint_snapshots (profile_id, label, n_videos_analysed, fingerprint_json) "
        "VALUES (?, ?, ?, ?)",
        (profile["profile_id"], label, profile["n_videos_analysed"], fp_json),
    )
    conn.commit()
    conn.close()
    return redirect(url_for("profile_drift", code=code))


def _profile_similarity(conn, profile_a, profile_b):
    """Distance/similarity between two profiles' fingerprints: numeric
    attributes compared by relative difference, categorical attributes
    compared by distribution overlap. Returns (overall_pct, rows)."""
    pid_a, pid_b = profile_a["profile_id"], profile_b["profile_id"]
    num_a = {r["attribute"]: r["mean_val"] for r in conn.execute(
        "SELECT attribute, mean_val FROM profile_fingerprint_numeric WHERE profile_id=?", (pid_a,))}
    num_b = {r["attribute"]: r["mean_val"] for r in conn.execute(
        "SELECT attribute, mean_val FROM profile_fingerprint_numeric WHERE profile_id=?", (pid_b,))}

    rows = []
    for attr in sorted(set(num_a) & set(num_b)):
        va, vb = num_a[attr], num_b[attr]
        denom = max(abs(va), abs(vb), 1e-6)
        rel_diff = min(abs(va - vb) / denom, 1.0)
        rows.append({"attribute": attr, "kind": "numeric", "a": va, "b": vb, "similarity_pct": round((1 - rel_diff) * 100, 1)})

    cat_a, cat_b = {}, {}
    for r in conn.execute("SELECT attribute, value, share_pct FROM profile_fingerprint_categorical WHERE profile_id=?", (pid_a,)):
        cat_a.setdefault(r["attribute"], {})[r["value"]] = r["share_pct"]
    for r in conn.execute("SELECT attribute, value, share_pct FROM profile_fingerprint_categorical WHERE profile_id=?", (pid_b,)):
        cat_b.setdefault(r["attribute"], {})[r["value"]] = r["share_pct"]

    for attr in sorted(set(cat_a) & set(cat_b)):
        values = set(cat_a[attr]) | set(cat_b[attr])
        overlap = sum(min(cat_a[attr].get(v, 0), cat_b[attr].get(v, 0)) for v in values)
        top_a = max(cat_a[attr], key=cat_a[attr].get)
        top_b = max(cat_b[attr], key=cat_b[attr].get)
        rows.append({
            "attribute": attr, "kind": "categorical",
            "a": f"{top_a} ({cat_a[attr][top_a]}%)", "b": f"{top_b} ({cat_b[attr][top_b]}%)",
            "similarity_pct": round(overlap, 1),
        })

    overall = round(sum(r["similarity_pct"] for r in rows) / len(rows), 1) if rows else 0.0
    rows.sort(key=lambda r: r["similarity_pct"])
    return overall, rows


@app.route("/profiles/compare", methods=["GET"])
def profiles_compare():
    profiles = _profiles()
    code_a = request.args.get("a", "")
    code_b = request.args.get("b", "")

    overall, rows, profile_a, profile_b = None, [], None, None
    if code_a and code_b and code_a != code_b:
        conn = get_conn()
        profile_a = _get_profile_or_404(conn, code_a)
        profile_b = _get_profile_or_404(conn, code_b)
        if profile_a and profile_b:
            overall, rows = _profile_similarity(conn, profile_a, profile_b)
        conn.close()

    return render_template(
        "profiles_compare.html", active="profiles",
        profiles=profiles, code_a=code_a, code_b=code_b,
        profile_a=profile_a, profile_b=profile_b, overall=overall, rows=rows,
    )


@app.route("/tests")
def tests_list():
    sort = request.args.get("sort", "test_id")
    direction = "asc" if request.args.get("dir") == "asc" else "desc"

    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM test_inputs ORDER BY test_id DESC LIMIT 200"
    ).fetchall()
    tests = []
    for t in rows:
        best = conn.execute(
            """SELECT s.total_score, p.profile_code FROM test_scores s
               JOIN style_profiles p ON p.profile_id = s.profile_id
               WHERE s.test_id = ? ORDER BY s.rank ASC LIMIT 1""",
            (t["test_id"],),
        ).fetchone()
        d = dict(t)
        d["best_score"] = best["total_score"] if best else None
        d["best_profile"] = best["profile_code"] if best else None
        tests.append(d)
    conn.close()

    sort_keys = {
        "test_id": lambda d: d["test_id"],
        "source_label": lambda d: (d["source_label"] or "").lower(),
        "input_type": lambda d: (d["input_type"] or "").lower(),
        "best_profile": lambda d: (d["best_profile"] or "").lower(),
        "best_score": lambda d: d["best_score"] if d["best_score"] is not None else -1,
        "submitted_at": lambda d: d["submitted_at"] or "",
    }
    tests.sort(key=sort_keys.get(sort, sort_keys["test_id"]), reverse=(direction == "desc"))

    return render_template(
        "tests_list.html", active="tests", tests=tests,
        filters={"sort": sort, "dir": direction},
    )


@app.route("/tests/<int:test_id>")
def test_detail(test_id):
    conn = get_conn()
    test = conn.execute("SELECT * FROM test_inputs WHERE test_id=?", (test_id,)).fetchone()
    if not test:
        conn.close()
        flash(f"No such test: {test_id}")
        return redirect(url_for("tests_list"))

    scores = conn.execute(
        """SELECT s.*, p.profile_code FROM test_scores s
           JOIN style_profiles p ON p.profile_id = s.profile_id
           WHERE s.test_id = ? ORDER BY s.rank""",
        (test_id,),
    ).fetchall()

    creations = conn.execute(
        """SELECT t.transformation_id, t.generated_title, t.generated_text, t.generated_by, t.generated_at,
                  p.profile_code
           FROM transformations t
           JOIN style_profiles p ON p.profile_id = t.target_profile_id
           WHERE t.test_id = ?
           ORDER BY t.generated_at DESC""",
        (test_id,),
    ).fetchall()
    conn.close()

    return render_template(
        "test_detail.html", active="tests",
        test=test, scores=scores, creations=creations,
    )


@app.route("/profiles/hybrid/create", methods=["POST"])
def hybrid_profile_create():
    codes = [c for c in request.form.getlist("source_profiles") if c]
    weights = [request.form.get(f"weight__{c}", default=1.0, type=float) or 1.0 for c in codes]

    carry = {
        "source": request.form.getlist("source"),
        "pasted_title": request.form.get("pasted_title", ""),
        "pasted_text": request.form.get("pasted_text", ""),
    }

    try:
        new_code = create_hybrid_profile(codes, weights)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("transform_form", **carry))

    flash(f"Created hybrid profile {new_code} — selected as target below.")
    carry["profile"] = new_code
    return redirect(url_for("transform_form", **carry))


def _transform_inputs():
    """Transform inputs are restricted to three categories — see the 2026-09
    cost investigation: picking a whole book (or a whole long-form video
    script) as input was quietly making generate_transform calls average
    ~25-30k input tokens, dwarfing the ~300-word output ever used from it.
    Book chapters and YouTube sections use their already-extracted
    points/terms/examples (small) instead of the source's full text."""
    conn = get_conn()
    insta_rows = conn.execute(
        """SELECT v.video_id, v.title, c.channel_name, a.word_count
           FROM videos v JOIN channels c ON c.channel_id = v.channel_id
           LEFT JOIN video_attributes a ON a.video_id = v.video_id
           WHERE v.media_type = 'Instagram'
           ORDER BY v.ingested_at DESC"""
    ).fetchall()
    book_chapter_rows = conn.execute(
        """SELECT s.section_id, s.section_title, s.section_number, b.title AS book_title, b.author
           FROM book_sections s JOIN books b ON b.book_id = s.book_id
           WHERE EXISTS (SELECT 1 FROM book_points p WHERE p.section_id = s.section_id)
              OR EXISTS (SELECT 1 FROM book_terms t WHERE t.section_id = s.section_id)
              OR EXISTS (SELECT 1 FROM book_examples e WHERE e.section_id = s.section_id)
           ORDER BY b.author, b.title, s.section_number"""
    ).fetchall()
    yt_section_rows = conn.execute(
        """SELECT s.section_id, s.section_title, s.section_number, v.title AS video_title, c.channel_name
           FROM video_sections s JOIN videos v ON v.video_id = s.video_id
           JOIN channels c ON c.channel_id = v.channel_id
           WHERE v.media_type = 'YouTube'
             AND (EXISTS (SELECT 1 FROM video_points p WHERE p.section_id = s.section_id)
               OR EXISTS (SELECT 1 FROM video_terms t WHERE t.section_id = s.section_id)
               OR EXISTS (SELECT 1 FROM video_examples e WHERE e.section_id = s.section_id))
           ORDER BY c.channel_name, v.title, s.section_number"""
    ).fetchall()
    conn.close()
    return insta_rows, book_chapter_rows, yt_section_rows


def _transform_input_options(insta_rows, book_chapter_rows, yt_section_rows):
    """Flattens the three source-type row sets into per-type option lists the
    client-side type-tab + checklist picker renders from and filters."""
    insta_options, book_chapter_options, yt_section_options = [], [], []
    for v in insta_rows:
        label = f"{v['channel_name']} — {v['title']}"
        if v["word_count"]:
            label += f" ({v['word_count']}w)"
        insta_options.append({"value": f"insta_video:{v['video_id']}", "label": label})
    for s in book_chapter_rows:
        label = f"{s['author'] or 'Unknown'} — {s['book_title']}: {s['section_title']}"
        book_chapter_options.append({"value": f"book_chapter:{s['section_id']}", "label": label})
    for s in yt_section_rows:
        label = f"{s['channel_name']} — {s['video_title']}: {s['section_title']}"
        yt_section_options.append({"value": f"yt_section:{s['section_id']}", "label": label})
    for options in (insta_options, book_chapter_options, yt_section_options):
        for o in options:
            o["search"] = o["label"].lower()
    return insta_options, book_chapter_options, yt_section_options


TRANSFORM_SOURCE_TYPES = {"insta_video", "book_chapter", "yt_section"}
MAX_TRANSFORM_SOURCES = 3


def _book_chapter_content(conn, section_id):
    """Builds a book chapter's Transform content from its already-extracted
    points/terms/examples — not the book's full text (that was the cost
    problem this replaced)."""
    section = conn.execute(
        """SELECT s.section_title, s.summary, b.title AS book_title, b.author
           FROM book_sections s JOIN books b ON b.book_id = s.book_id WHERE s.section_id = ?""",
        (section_id,),
    ).fetchone()
    if not section:
        raise ValueError(f"No such book chapter: section_id={section_id}")
    parts = [f"Chapter: {section['section_title']}"]
    if section["summary"]:
        parts.append(section["summary"])
    points = [r["point_text"] for r in conn.execute(
        "SELECT point_text FROM book_points WHERE section_id = ?", (section_id,))]
    if points:
        parts.append("Key points:\n" + "\n".join(f"- {p}" for p in points))
    terms = conn.execute(
        "SELECT term, definition FROM book_terms WHERE section_id = ?", (section_id,)
    ).fetchall()
    if terms:
        parts.append("Terms:\n" + "\n".join(f"- {t['term']}: {t['definition'] or ''}" for t in terms))
    examples = conn.execute(
        "SELECT example_title, example_text FROM book_examples WHERE section_id = ?", (section_id,)
    ).fetchall()
    if examples:
        parts.append("Examples:\n" + "\n\n".join(
            f"{e['example_title']}\n{e['example_text']}" if e["example_title"] else e["example_text"]
            for e in examples
        ))
    label = f"{section['author'] or 'Unknown'} — {section['book_title']}: {section['section_title']}"
    return "\n\n".join(parts), label


def _yt_section_content(conn, section_id):
    """Video-section counterpart to _book_chapter_content() — same
    already-extracted-content approach instead of a slice of the full
    transcript (which the app has no stored start/end offsets for anyway)."""
    section = conn.execute(
        """SELECT s.section_title, s.summary, v.title AS video_title, c.channel_name
           FROM video_sections s JOIN videos v ON v.video_id = s.video_id
           JOIN channels c ON c.channel_id = v.channel_id WHERE s.section_id = ?""",
        (section_id,),
    ).fetchone()
    if not section:
        raise ValueError(f"No such video section: section_id={section_id}")
    parts = [f"Section: {section['section_title']}"]
    if section["summary"]:
        parts.append(section["summary"])
    points = [r["point_text"] for r in conn.execute(
        "SELECT point_text FROM video_points WHERE section_id = ?", (section_id,))]
    if points:
        parts.append("Key points:\n" + "\n".join(f"- {p}" for p in points))
    terms = conn.execute(
        "SELECT term, definition FROM video_terms WHERE section_id = ?", (section_id,)
    ).fetchall()
    if terms:
        parts.append("Terms:\n" + "\n".join(f"- {t['term']}: {t['definition'] or ''}" for t in terms))
    examples = conn.execute(
        "SELECT example_title, example_text FROM video_examples WHERE section_id = ?", (section_id,)
    ).fetchall()
    if examples:
        parts.append("Examples:\n" + "\n\n".join(
            f"{e['example_title']}\n{e['example_text']}" if e["example_title"] else e["example_text"]
            for e in examples
        ))
    label = f"{section['channel_name']} — {section['video_title']}: {section['section_title']}"
    return "\n\n".join(parts), label


def _resolve_transform_sources(source_values):
    """Turns 1-3 'type:id' source values (all the same type) into one
    combined (raw_text, title, source_label, input_type) tuple. Multiple
    sources are concatenated with a labeled divider so Claude sees them as
    distinct passages, not one continuous piece."""
    if not source_values:
        raise ValueError("Choose an input or paste some text.")
    if len(source_values) > MAX_TRANSFORM_SOURCES:
        raise ValueError(f"Choose at most {MAX_TRANSFORM_SOURCES} inputs.")
    parsed = [sv.partition(":") for sv in source_values]
    types = {t for t, _, _ in parsed}
    if len(types) > 1:
        raise ValueError("All selected inputs must be the same type (e.g. all book chapters).")
    src_type = types.pop()
    if src_type not in TRANSFORM_SOURCE_TYPES:
        raise ValueError("Choose an input or paste some text.")

    conn = get_conn()
    try:
        pieces = []  # (content, label)
        for _, _, sid_str in parsed:
            sid = int(sid_str)
            if src_type == "insta_video":
                row = conn.execute(
                    """SELECT v.title, v.script, c.channel_name FROM videos v
                       JOIN channels c ON c.channel_id = v.channel_id WHERE v.video_id = ?""",
                    (sid,),
                ).fetchone()
                if not row:
                    raise ValueError("No such video")
                pieces.append((row["script"], f"{row['channel_name']} — {row['title']}"))
            elif src_type == "book_chapter":
                pieces.append(_book_chapter_content(conn, sid))
            else:  # yt_section
                pieces.append(_yt_section_content(conn, sid))
    finally:
        conn.close()

    kind_label = {"insta_video": "Insta video", "book_chapter": "Book chapter", "yt_section": "YouTube section"}[src_type]
    if len(pieces) == 1:
        raw_text, label = pieces[0]
        title = label
        source_label = f"{kind_label}: {label}"
    else:
        raw_text = "\n\n".join(f"--- Source {i + 1}: {label} ---\n{content}" for i, (content, label) in enumerate(pieces))
        title = " + ".join(label for _, label in pieces)
        source_label = f"{kind_label} ({len(pieces)} combined): " + "; ".join(label for _, label in pieces)
    return raw_text, title, source_label, src_type


@app.route("/transform", methods=["GET", "POST"])
def transform_form():
    # Generation only ever happens on POST. This used to run on GET whenever
    # the query string carried a profile + a source, which made a BILLED
    # Claude call (plus a test_inputs row and N test_scores rows) a side
    # effect of merely loading a URL: refreshing re-charged, the back button
    # re-charged, and sharing the link re-charged. Worst of all,
    # hybrid_profile_create() redirects here with exactly those params, so
    # creating a hybrid profile fired a full generation nobody asked for.
    # GET now only ever renders the form (carrying any pre-selected values
    # through, so that redirect still works — it just doesn't spend money).
    src = request.form if request.method == "POST" else request.args
    sources = [s for s in src.getlist("source") if s]
    pasted_title = (src.get("pasted_title") or "").strip()
    pasted_text = (src.get("pasted_text") or "").strip()
    profile = (src.get("profile") or "").strip()
    mode = src.get("mode", "transform")
    output_form = (src.get("form") or "").strip()

    insta_rows, book_chapter_rows, yt_section_rows = _transform_inputs()
    insta_options, book_chapter_options, yt_section_options = _transform_input_options(
        insta_rows, book_chapter_rows, yt_section_rows
    )

    prompt_text = None
    selected_test_id = None
    original_text = None
    gen_title = gen_text = None
    if request.method != "POST":
        pass  # GET renders the form only — never generates, never bills
    elif mode == "data_output" and not output_form and profile and (sources or pasted_text):
        flash("Choose an output form to generate a data output.")
    elif profile and (sources or pasted_text):
        try:
            if pasted_text:
                raw_text = pasted_text
                title = pasted_title or "(pasted text)"
                source_label = "Pasted text"
                input_type = "pasted_text"
            else:
                raw_text, title, source_label, input_type = _resolve_transform_sources(sources)

            original_text = raw_text
            result = rate_input(raw_text, title=title, source_label=source_label, input_type=input_type)
            selected_test_id = result["test_id"]
            if mode == "data_output":
                prompt_text = build_data_output_prompt(selected_test_id, profile, output_form)
            else:
                prompt_text = build_transform_prompt(selected_test_id, profile)

            try:
                gen_result = generate_transform(prompt_text)
                gen_title, gen_text = gen_result["title"], gen_result["script"]
            except LLMConfigError as e:
                flash(str(e))
            except Exception as e:
                flash(f"Generation failed: {e}")
        except ValueError as e:
            flash(str(e))

    return render_template(
        "transform_form.html", active="transform",
        insta_options=insta_options, book_chapter_options=book_chapter_options, yt_section_options=yt_section_options,
        max_transform_sources=MAX_TRANSFORM_SOURCES,
        profiles=_profiles(),
        selected_sources=sources, selected_profile=profile, prompt_text=prompt_text,
        selected_test_id=selected_test_id, pasted_title=pasted_title, pasted_text=pasted_text,
        original_text=original_text, gen_title=gen_title, gen_text=gen_text,
        selected_mode=mode, selected_form=output_form, form_specs=FORM_SPECS,
    )


@app.route("/transform/generate", methods=["POST"])
def transform_generate():
    test_id = request.form.get("test_id", type=int)
    profile = request.form.get("profile", "")
    sources = [s for s in request.form.getlist("source") if s]
    pasted_title = request.form.get("pasted_title", "")
    pasted_text = request.form.get("pasted_text", "")
    mode = request.form.get("mode", "transform")
    output_form = request.form.get("form", "").strip()

    insta_rows, book_chapter_rows, yt_section_rows = _transform_inputs()
    insta_options, book_chapter_options, yt_section_options = _transform_input_options(
        insta_rows, book_chapter_rows, yt_section_rows
    )

    if not test_id or not profile:
        flash("Generate the prompt first (pick an input + target profile above).")
        return redirect(url_for("transform_form"))
    if mode == "data_output" and not output_form:
        flash("Choose an output form to generate a data output.")
        return redirect(url_for("transform_form"))

    if mode == "data_output":
        prompt_text = build_data_output_prompt(test_id, profile, output_form)
    else:
        prompt_text = build_transform_prompt(test_id, profile)

    conn = get_conn()
    test_row = conn.execute("SELECT raw_text FROM test_inputs WHERE test_id=?", (test_id,)).fetchone()
    conn.close()
    original_text = test_row["raw_text"] if test_row else None

    gen_title = gen_text = None
    try:
        result = generate_transform(prompt_text)
        gen_title, gen_text = result["title"], result["script"]
    except LLMConfigError as e:
        flash(str(e))
    except Exception as e:
        flash(f"Generation failed: {e}")

    return render_template(
        "transform_form.html", active="transform",
        insta_options=insta_options, book_chapter_options=book_chapter_options, yt_section_options=yt_section_options,
        max_transform_sources=MAX_TRANSFORM_SOURCES,
        profiles=_profiles(),
        selected_sources=sources, selected_profile=profile, prompt_text=prompt_text,
        selected_test_id=test_id, pasted_title=pasted_title, pasted_text=pasted_text,
        original_text=original_text, gen_title=gen_title, gen_text=gen_text,
        selected_mode=mode, selected_form=output_form, form_specs=FORM_SPECS,
    )


@app.route("/transform/save", methods=["POST"])
def transform_save():
    test_id = request.form.get("test_id", type=int)
    profile = request.form.get("profile", "")
    gen_title = request.form.get("gen_title", "")
    gen_text = request.form.get("gen_text", "").strip()

    if not test_id or not profile or not gen_text:
        flash("Missing test_id, profile, or generated text.")
        return redirect(url_for("transform_form"))

    try:
        transformation_id = save_transformation(test_id, profile, gen_title, gen_text)
        result = score_transformation(transformation_id)
    except ValueError as e:
        flash(str(e))
        return redirect(url_for("transform_form"))

    return render_template("transform_result.html", active="transform", result=result)


# ============================================================
# SWIPE MECHANISM (v1 — single-source candidates only)
# ============================================================
# A phone-first way to triage the existing corpus: one source item at a
# time, swipe past or keep, and a "like" surfaces three style profiles
# worth writing it up for. Deliberately single-source for now — combining
# 2-3 inputs into one candidate is a harder, separate problem to tackle
# once this core loop is validated. See swipe.html for the frontend.

SWIPE_BUFFER_TARGET = 6  # keep at least this many 'queued' candidates on hand

# Weighted random source count for each new candidate: mostly single-source
# (the proven core loop), sometimes 2 or 3 combined. Not user-configurable
# yet — a reasonable starting split, easy to retune once there's a sense
# of how often multi-source combos actually land well.
SWIPE_SOURCE_COUNT_WEIGHTS = [(1, 0.60), (2, 0.25), (3, 0.15)]

SWIPE_PITCH_PROMPT_SINGLE = """You're curating short-form video ideas from an archive of source material for a creator who makes Instagram Reels.

SOURCE TYPE: {kind}
TITLE: {title}
CONTENT (may be truncated):
{content}

Return raw JSON only, matching exactly this shape — no markdown fence, no commentary:
{{
  "hook": "a punchy one-line hook for the video this source could become, under 90 characters",
  "summary": "2-3 sentences: what this source covers and why it could make an interesting Instagram script",
  "terms": ["up to 4 short key terms or concepts from this source"],
  "examples": ["up to 3 concrete named examples or case studies from this source, if any exist"]
}}"""

# Cross-subject combining is allowed on purpose (see the "Combining"
# decision) — the prompt explicitly asks Claude to find a real connecting
# thread rather than default to combining only same-subject sources.
SWIPE_PITCH_PROMPT_MULTI = """You're curating short-form video ideas from an archive of source material for a creator who makes Instagram Reels. You've been given {n} different sources, possibly on different subjects. Find a genuine connecting thread between them — a real link, not a forced one — and pitch ONE combined video idea that weaves them together. If you can't find a real connection, it's fine for the thread to be a shared theme, tension, or contrast between them rather than a shared topic.

{sources_block}

Return raw JSON only, matching exactly this shape — no markdown fence, no commentary:
{{
  "hook": "a punchy one-line hook for the combined video, under 90 characters",
  "summary": "2-3 sentences: the connecting thread across these sources and why combining them makes an interesting Instagram script",
  "terms": ["up to 4 short key terms or concepts spanning the sources"],
  "examples": ["up to 3 concrete named examples or case studies drawn from the sources"]
}}"""


def _excluded_source_keys(conn):
    """(kind, id) pairs that must not be picked as a new candidate source:
    anything referenced by a currently queued or already-liked candidate,
    whether as that candidate's sole source (legacy source_video_id/
    source_book_id) or as one of several combined ones (sources_json).
    Disliked candidates' sources are deliberately NOT excluded here — see
    _pick_unswiped_source()."""
    excluded = set()
    for r in conn.execute(
        "SELECT source_kind, source_video_id, source_book_id, sources_json "
        "FROM swipe_candidates WHERE status != 'disliked'"
    ).fetchall():
        if r["source_video_id"] is not None:
            excluded.add(("video", r["source_video_id"]))
        if r["source_book_id"] is not None:
            excluded.add(("book", r["source_book_id"]))
        if r["sources_json"]:
            for s in json.loads(r["sources_json"]):
                excluded.add((s["kind"], s["id"]))
    return excluded


# Weighted kind pool for picking one new source. "video"/"book" are the
# whole-item fallback (an Instagram video, or a book/YouTube-video with no
# chapter breakdown at all); "video_section"/"book_section" swipe on one
# chapter/section instead of the whole thing, using its own already-
# classified points/terms/examples where the breakdown exists — which is
# most books and most long-form YouTube videos already ingested. This is
# what actually answers "input = book chapter / section of long video",
# not a separate mechanism from whole-item swiping.
SWIPE_SOURCE_KIND_WEIGHTS = [("video", 0.30), ("video_section", 0.25), ("book", 0.05), ("book_section", 0.40)]


def _weighted_kind_order():
    """A full ordering of the 4 source kinds, sampled without replacement
    by weight — so a pool that's empty this call falls through to the
    next-most-likely kind instead of failing outright."""
    remaining = list(SWIPE_SOURCE_KIND_WEIGHTS)
    order = []
    while remaining:
        total = sum(w for _, w in remaining)
        r = random.random() * total
        cumulative = 0.0
        for i, (kind, w) in enumerate(remaining):
            cumulative += w
            if r < cumulative:
                order.append(kind)
                remaining.pop(i)
                break
        else:
            order.append(remaining.pop()[0])
    return order


def _pick_unswiped_source(conn, excluded=None):
    """Returns (kind, row) for a random item eligible to become a swipe
    candidate source, or (None, None) once every pool is exhausted. Tries
    the 4 kinds (video / video_section / book / book_section) in a
    weighted random order each call, so the queue doesn't skew toward
    whichever pool happens to be biggest.

    `excluded` is a set of (kind, id) pairs to skip — pass the same set
    across multiple calls (see _pick_unswiped_sources()) so a multi-source
    candidate never picks the same item twice. Defaults to
    _excluded_source_keys(conn) when not given.

    Excluding a source doesn't mean "gone forever": a disliked candidate's
    sources are NOT in the default excluded set, so they're eligible again
    the same as anything never swiped — there's no cooldown/delay logic,
    with hundreds of other never-tried items in the pool, ORDER BY RANDOM()
    naturally makes "later" mean later, not next card."""
    if excluded is None:
        excluded = _excluded_source_keys(conn)
    for kind in _weighted_kind_order():
        excluded_ids = [k[1] for k in excluded if k[0] == kind]
        placeholders = ",".join("?" * len(excluded_ids))

        if kind == "video":
            where_id = f"AND v.video_id NOT IN ({placeholders})" if excluded_ids else ""
            row = conn.execute(
                f"""SELECT v.video_id, v.title, v.script AS content, c.channel_id, c.channel_name
                    FROM videos v JOIN channels c ON c.channel_id = v.channel_id
                    WHERE (v.media_type != 'YouTube' OR NOT EXISTS (
                        SELECT 1 FROM video_sections vs WHERE vs.video_id = v.video_id
                    )) {where_id}
                    ORDER BY RANDOM() LIMIT 1""",
                excluded_ids,
            ).fetchone()
        elif kind == "video_section":
            where_id = f"AND vs.section_id NOT IN ({placeholders})" if excluded_ids else ""
            row = conn.execute(
                f"""SELECT vs.section_id, vs.section_title AS title, vs.summary, vs.topics,
                           v.title AS parent_title, c.channel_id, c.channel_name,
                           (SELECT GROUP_CONCAT(point_text, ' | ') FROM video_points WHERE section_id = vs.section_id) AS points,
                           (SELECT GROUP_CONCAT(term || ': ' || COALESCE(definition, ''), ' | ') FROM video_terms WHERE section_id = vs.section_id) AS terms,
                           (SELECT GROUP_CONCAT(example_title || ' - ' || example_text, ' | ') FROM video_examples WHERE section_id = vs.section_id) AS examples
                    FROM video_sections vs
                    JOIN videos v ON v.video_id = vs.video_id
                    JOIN channels c ON c.channel_id = v.channel_id
                    WHERE 1=1 {where_id}
                    ORDER BY RANDOM() LIMIT 1""",
                excluded_ids,
            ).fetchone()
        elif kind == "book":
            where_id = f"AND b.book_id NOT IN ({placeholders})" if excluded_ids else ""
            row = conn.execute(
                f"""SELECT b.book_id, b.title, COALESCE(b.summary, substr(b.full_text, 1, 4000)) AS content,
                           b.author AS channel_name,
                           (SELECT channel_id FROM channels WHERE channel_name = b.author AND platform = 'Book') AS channel_id
                    FROM books b
                    WHERE NOT EXISTS (SELECT 1 FROM book_sections bs WHERE bs.book_id = b.book_id) {where_id}
                    ORDER BY RANDOM() LIMIT 1""",
                excluded_ids,
            ).fetchone()
        else:  # book_section
            where_id = f"AND bs.section_id NOT IN ({placeholders})" if excluded_ids else ""
            row = conn.execute(
                f"""SELECT bs.section_id, bs.section_title AS title, bs.summary, bs.topics,
                           b.title AS parent_title, b.author AS channel_name,
                           (SELECT channel_id FROM channels WHERE channel_name = b.author AND platform = 'Book') AS channel_id,
                           (SELECT GROUP_CONCAT(point_text, ' | ') FROM book_points WHERE section_id = bs.section_id) AS points,
                           (SELECT GROUP_CONCAT(term || ': ' || COALESCE(definition, ''), ' | ') FROM book_terms WHERE section_id = bs.section_id) AS terms,
                           (SELECT GROUP_CONCAT(example_title || ' - ' || example_text, ' | ') FROM book_examples WHERE section_id = bs.section_id) AS examples
                    FROM book_sections bs
                    JOIN books b ON b.book_id = bs.book_id
                    WHERE 1=1 {where_id}
                    ORDER BY RANDOM() LIMIT 1""",
                excluded_ids,
            ).fetchone()
        if row:
            return kind, row
    return None, None


def _choose_source_count():
    r = random.random()
    cumulative = 0.0
    for count, weight in SWIPE_SOURCE_COUNT_WEIGHTS:
        cumulative += weight
        if r < cumulative:
            return count
    return SWIPE_SOURCE_COUNT_WEIGHTS[-1][0]


def _pick_unswiped_sources(conn, n):
    """Picks up to n distinct eligible sources for one candidate. May
    return fewer than n if the corpus runs out mid-pick."""
    excluded = _excluded_source_keys(conn)
    picked = []
    for _ in range(n):
        kind, row = _pick_unswiped_source(conn, excluded=excluded)
        if not row:
            break
        picked.append((kind, row))
        excluded.add((kind, row[0]))
    return picked


def _source_content_block(kind, row, limit):
    """Builds the CONTENT text fed to the pitch prompt for one source. For
    a chapter/section source (video_section / book_section), this uses the
    section's own already-classified summary/topics/points/terms/examples
    rather than raw script/book text — that content is real (extracted by
    the classification pipeline, not guessed), so it's both cheaper to
    include and more accurate than asking Claude to invent terms/examples
    from scratch the way a whole-book/whole-video source still has to."""
    if kind in ("video_section", "book_section"):
        parts = [f"Chapter: {row['title']}", f"From: {row['parent_title']}"]
        if row["summary"]:
            parts.append(f"Summary: {row['summary']}")
        if row["topics"]:
            parts.append(f"Topics: {row['topics']}")
        if row["points"]:
            parts.append(f"Key points: {row['points']}")
        if row["terms"]:
            parts.append(f"Known terms (reuse these rather than inventing new ones): {row['terms']}")
        if row["examples"]:
            parts.append(f"Known examples (reuse these rather than inventing new ones): {row['examples']}")
        return "\n".join(parts)[:limit]
    return (row["content"] or "")[:limit]


def _build_pitch_prompt(sources):
    """sources: list of (kind, row) tuples, length 1-3."""
    if len(sources) == 1:
        kind, row = sources[0]
        content = _source_content_block(kind, row, 4000)
        return SWIPE_PITCH_PROMPT_SINGLE.format(kind=kind, title=row["title"], content=content)
    blocks = []
    for i, (kind, row) in enumerate(sources, start=1):
        content = _source_content_block(kind, row, 2000)
        blocks.append(f'SOURCE {i} ({kind}): "{row["title"]}"\nCONTENT (may be truncated):\n{content}')
    return SWIPE_PITCH_PROMPT_MULTI.format(n=len(sources), sources_block="\n\n".join(blocks))


def _generate_swipe_candidate(conn):
    """Picks 1-3 never-swiped source items (weighted toward 1, see
    SWIPE_SOURCE_COUNT_WEIGHTS), asks Claude for a pitch spanning all of
    them, and stores it as a fresh 'queued' candidate. Returns the new
    candidate_id, or None if the corpus has nothing left to turn into a
    candidate (or the LLM call itself fails, e.g. no ANTHROPIC_API_KEY
    configured)."""
    n = _choose_source_count()
    sources = _pick_unswiped_sources(conn, n)
    if not sources:
        return None

    prompt = _build_pitch_prompt(sources)
    try:
        pitch = generate_json(prompt, max_tokens=700)
    except Exception as e:
        print(f"[swipe] pitch generation failed for {len(sources)} source(s): {e}")
        return None

    sources_payload = [
        {
            "kind": kind, "id": row[0], "title": row["title"],
            "parent_title": row["parent_title"] if "parent_title" in row.keys() else None,
            "channel_id": row["channel_id"], "channel_name": row["channel_name"],
        }
        for kind, row in sources
    ]
    # Legacy singular columns stay populated only for a true single-source
    # candidate (kept for cheap indexed lookups / older-row compatibility);
    # a combined candidate relies entirely on sources_json.
    single_kind, single_row = sources[0] if len(sources) == 1 else (None, None)
    cur = conn.execute(
        """INSERT INTO swipe_candidates
           (source_kind, source_video_id, source_book_id, sources_json, hook, pitch_summary, terms_json, examples_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            single_kind or "mixed",
            single_row["video_id"] if single_kind == "video" else None,
            single_row["book_id"] if single_kind == "book" else None,
            json.dumps(sources_payload),
            pitch.get("hook"), pitch.get("summary"),
            json.dumps(pitch.get("terms") or []), json.dumps(pitch.get("examples") or []),
        ),
    )
    conn.commit()
    return cur.lastrowid


def _fill_swipe_buffer(target=SWIPE_BUFFER_TARGET, max_new=3):
    """Tops the queued buffer back up, generating at most max_new candidates
    in this pass. Runs on its own connection so it's safe to call from a
    background thread — see _maybe_start_swipe_buffer_fill() below. Never
    call this directly from a request handler: each candidate is a live
    Claude call (several seconds), and gunicorn here only has a handful of
    worker threads total — blocking even two or three of them on live LLM
    calls is enough to stall every other page on the site behind it."""
    conn = get_conn()
    try:
        queued = conn.execute("SELECT COUNT(*) FROM swipe_candidates WHERE status = 'queued'").fetchone()[0]
        made = 0
        while queued + made < target and made < max_new:
            if _generate_swipe_candidate(conn) is None:
                break  # corpus exhausted, or generation failed — don't loop forever
            made += 1
        return made
    finally:
        conn.close()


_swipe_buffer_lock = threading.Lock()
_swipe_buffer_filling = False


def _maybe_start_swipe_buffer_fill():
    """Fire-and-forget top-up: starts _fill_swipe_buffer() on a daemon
    thread if one isn't already running, and returns immediately either
    way. Guarded by a lock so a burst of near-simultaneous requests can't
    each spawn their own fill and hammer the LLM API in parallel."""
    global _swipe_buffer_filling
    with _swipe_buffer_lock:
        if _swipe_buffer_filling:
            return
        _swipe_buffer_filling = True

    def _run():
        global _swipe_buffer_filling
        try:
            _fill_swipe_buffer()
        except Exception as e:
            print(f"[swipe] background buffer fill failed: {e}")
        finally:
            with _swipe_buffer_lock:
                _swipe_buffer_filling = False

    threading.Thread(target=_run, daemon=True).start()


def _serialize_candidate(row):
    if row["sources_json"]:
        sources = json.loads(row["sources_json"])
    else:
        # Legacy single-source row from before sources_json existed —
        # build an equivalent one-item list from the joined title/
        # channel_name columns (see the query in api_swipe_next()).
        sid = row["source_video_id"] if row["source_kind"] == "video" else row["source_book_id"]
        sources = [{"kind": row["source_kind"], "id": sid, "title": row["title"], "channel_name": row["channel_name"]}]
    return {
        "candidate_id": row["candidate_id"],
        "sources": sources,
        "hook": row["hook"],
        "summary": row["pitch_summary"],
        "terms": json.loads(row["terms_json"]) if row["terms_json"] else [],
        "examples": json.loads(row["examples_json"]) if row["examples_json"] else [],
    }


@app.route("/swipe")
def swipe_page():
    return render_template("swipe.html", active="swipe", format_choices=SWIPE_FORM_CHOICES)


@app.route("/sw.js")
def swipe_service_worker():
    """Served at the root path (not /static/sw.js) so its default scope is
    '/' and it can actually control /swipe — a service worker's scope is
    the directory it's served from unless widened by a
    Service-Worker-Allowed header, and PWA install criteria require an
    active service worker controlling the page being installed."""
    resp = send_file(os.path.join(app.static_folder, "sw.js"), mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp


@app.route("/api/swipe/next")
def api_swipe_next():
    conn = get_conn()
    queued_count = conn.execute("SELECT COUNT(*) FROM swipe_candidates WHERE status = 'queued'").fetchone()[0]
    if queued_count < SWIPE_BUFFER_TARGET:
        _maybe_start_swipe_buffer_fill()  # never blocks this request on an LLM call
    row = conn.execute(
        """SELECT sc.*,
                  COALESCE(v.title, b.title) AS title,
                  COALESCE(c.channel_name, b.author) AS channel_name,
                  v.url AS source_url
           FROM swipe_candidates sc
           LEFT JOIN videos v ON v.video_id = sc.source_video_id
           LEFT JOIN channels c ON c.channel_id = v.channel_id
           LEFT JOIN books b ON b.book_id = sc.source_book_id
           WHERE sc.status = 'queued'
           ORDER BY sc.created_at LIMIT 1"""
    ).fetchone()
    conn.close()
    if not row:
        message = (
            "Preparing more inputs — check back in a few seconds."
            if queued_count < SWIPE_BUFFER_TARGET
            else "No more inputs to swipe on right now."
        )
        return jsonify({"ok": True, "candidate": None, "message": message})
    return jsonify({"ok": True, "candidate": _serialize_candidate(row)})


def _build_pitch_text(row):
    """The candidate's pitch (hook + summary + terms + examples), flattened
    to plain text — used both as the Transform source in api_swipe_create()
    and as the raw_text fed into the voice-fit scoring in
    _score_profile_matches(). Never the full source book/video text — see
    the 071c959 fix."""
    lines = [row["hook"] or "", row["pitch_summary"] or ""]
    terms = json.loads(row["terms_json"]) if row["terms_json"] else []
    examples = json.loads(row["examples_json"]) if row["examples_json"] else []
    if terms:
        lines.append("Terms: " + ", ".join(terms))
    if examples:
        lines.append("Examples: " + "; ".join(examples))
    return "\n\n".join(line for line in lines if line)


def _candidate_source_channels(conn, row):
    """Returns (channel_ids, subjects) for a candidate's contributing
    source(s) — channel_ids is every channel behind its 1-3 sources
    (for exclusion), subjects is the set of subjects those channels'
    profiles carry (for the blended score's subject component)."""
    if row["sources_json"]:
        channel_ids = [s["channel_id"] for s in json.loads(row["sources_json"]) if s.get("channel_id")]
    else:
        # Legacy single-source row: resolve its one channel directly.
        channel_ids = []
        if row["source_kind"] == "video" and row["source_video_id"]:
            v = conn.execute("SELECT channel_id FROM videos WHERE video_id = ?", (row["source_video_id"],)).fetchone()
            if v:
                channel_ids.append(v["channel_id"])
        elif row["source_kind"] == "book" and row["source_book_id"]:
            b = conn.execute("SELECT author FROM books WHERE book_id = ?", (row["source_book_id"],)).fetchone()
            if b and b["author"]:
                c = conn.execute(
                    "SELECT channel_id FROM channels WHERE channel_name = ? AND platform = 'Book'", (b["author"],)
                ).fetchone()
                if c:
                    channel_ids.append(c["channel_id"])
    subjects = set()
    for cid in channel_ids:
        sp = conn.execute("SELECT subject FROM style_profiles WHERE channel_id = ?", (cid,)).fetchone()
        if sp and sp["subject"]:
            subjects.add(sp["subject"])
    return channel_ids, subjects


_SWIPE_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "for", "with", "is", "are", "was", "were",
    "this", "that", "it", "its", "as", "by", "at", "from", "be", "how", "what", "why", "your", "you", "their",
    "his", "her", "he", "she", "they", "not", "no", "do", "does", "did", "if", "so", "we", "our", "us", "i",
    "my", "me", "can", "could", "would", "should", "will", "just", "about", "into", "than", "then", "there",
    "these", "those", "which", "who", "whom", "when", "where", "been", "have", "has", "had", "one", "over",
}


def _significant_words(texts):
    """Lowercased, stopword-and-short-word-filtered vocabulary from a list
    of strings — the basis for the topical-overlap component of the match
    score. Deliberately simple (no stemming/lemmatizing): this only needs
    to catch shared specific nouns/terms, not do real NLP."""
    words = set()
    for t in texts:
        if not t:
            continue
        for w in re.findall(r"[a-zA-Z']+", t.lower()):
            if len(w) > 3 and w not in _SWIPE_STOPWORDS:
                words.add(w)
    return words


_CHANNEL_VOCAB_CACHE = {"data": None, "computed_at": 0.0}
_CHANNEL_VOCAB_TTL_SEC = 900  # 15 min — this is a soft matching signal, not
                              # something that needs per-second freshness;
                              # a newly-classified video's terms show up in
                              # the next cache refresh, not instantly.


def _channel_topical_vocab(conn):
    """Precomputes every channel's own topical vocabulary (terms + examples,
    video-side and book-side) in 4 bulk queries total, cached process-wide
    for _CHANNEL_VOCAB_TTL_SEC — replaces the original per-profile version
    (4-6+ queries PER profile scored on every like) that measured ~3.5s in
    production. The first bulk-query version (no cache) was actually
    SLOWER end to end (~5s) despite far fewer queries: with no LIMIT, it
    regex-tokenizes every term/example in the entire database on every
    single like, not just the ~100-per-table slice the old per-profile
    version capped itself to. The cache is what actually fixes it — this
    heavy pass now only runs once every 15 minutes, not on every swipe.
    Returns {channel_id: set(significant_words)}."""
    now = time.time()
    if _CHANNEL_VOCAB_CACHE["data"] is not None and (now - _CHANNEL_VOCAB_CACHE["computed_at"]) < _CHANNEL_VOCAB_TTL_SEC:
        return _CHANNEL_VOCAB_CACHE["data"]

    texts_by_channel = {}

    def add(channel_id, *parts):
        if channel_id is None:
            return
        texts_by_channel.setdefault(channel_id, []).extend(p for p in parts if p)

    for r in conn.execute(
        "SELECT v.channel_id, vt.term, vt.definition FROM video_terms vt JOIN videos v ON v.video_id = vt.video_id"
    ).fetchall():
        add(r["channel_id"], r["term"], r["definition"])
    for r in conn.execute(
        "SELECT v.channel_id, ve.example_title, ve.example_text FROM video_examples ve JOIN videos v ON v.video_id = ve.video_id"
    ).fetchall():
        add(r["channel_id"], r["example_title"], r["example_text"])
    for r in conn.execute(
        "SELECT c.channel_id, bt.term, bt.definition FROM channels c "
        "JOIN books b ON b.author = c.channel_name JOIN book_terms bt ON bt.book_id = b.book_id"
    ).fetchall():
        add(r["channel_id"], r["term"], r["definition"])
    for r in conn.execute(
        "SELECT c.channel_id, be.example_title, be.example_text FROM channels c "
        "JOIN books b ON b.author = c.channel_name JOIN book_examples be ON be.book_id = b.book_id"
    ).fetchall():
        add(r["channel_id"], r["example_title"], r["example_text"])

    result = {cid: _significant_words(texts) for cid, texts in texts_by_channel.items()}
    _CHANNEL_VOCAB_CACHE["data"] = result
    _CHANNEL_VOCAB_CACHE["computed_at"] = now
    return result


def _topical_overlap_score(cand_words, profile_words):
    """Fraction of the candidate's own vocabulary (terms + examples) that
    also shows up in a profile's own real content. Asymmetric on purpose:
    it asks "does this profile actually talk about the same specific
    things this candidate mentions", not "how similar are the two
    vocabularies overall" — a profile with a much larger vocabulary
    shouldn't be penalized for it. Takes precomputed word sets (see
    _channel_topical_vocab) rather than querying, so this is a pure,
    near-free set intersection now."""
    if not cand_words or not profile_words:
        return 0.0
    return len(cand_words & profile_words) / max(1, len(cand_words))


def _score_profile_matches(conn, candidate_row):
    """The comprehensive match score: 60% subject match + 20% voice-fit
    (the same fingerprint-correlation scoring already used to score real
    Transform inputs against every profile, reused here rather than
    invented fresh) + 20% topical overlap (shared vocabulary with the
    profile's own real content). No hard subject pre-filter — every
    profile is scored and ranked on the full blend.

    A profile belonging to one of the candidate's own source channels is
    excluded from the results UNLESS it would rank #1 overall even before
    exclusion, in which case it's let through anyway and the remaining two
    slots still come from eligible (non-excluded) profiles."""
    source_channel_ids, source_subjects = _candidate_source_channels(conn, candidate_row)
    excluded_channel_ids = set(source_channel_ids)

    pitch_text = _build_pitch_text(candidate_row)
    features = extract_auto_features(pitch_text, candidate_row["hook"] or "")
    # No raw_text here, deliberately: score_against_profiles' membership
    # check pulls every video's FULL SCRIPT per channel and runs a
    # difflib.SequenceMatcher comparison against it for every profile —
    # meant to catch a real Transform input that's literally a duplicate
    # of a training video. A swipe candidate's synthetic pitch can never
    # be a literal duplicate of a real script, so that check was always
    # going to fail here while still paying its full O(profiles x videos)
    # cost — this was the actual ~5s bottleneck behind a slow swipe-right,
    # not the topical-overlap query. Omitting raw_text skips straight to
    # attribute-correlation scoring, which is the only axis that's
    # actually meaningful for a synthetic pitch anyway.
    voice_raw = score_against_profiles(features)
    voice_by_code = {r["profile_code"]: (r["total_score"] or 0) / 100.0 for r in voice_raw}

    cand_words = _significant_words(
        (json.loads(candidate_row["terms_json"]) if candidate_row["terms_json"] else [])
        + (json.loads(candidate_row["examples_json"]) if candidate_row["examples_json"] else [])
    )

    profiles = conn.execute(
        "SELECT p.profile_code, p.subject, p.channel_id, p.n_videos_analysed, c.channel_name "
        "FROM style_profiles p JOIN channels c ON c.channel_id = p.channel_id"
    ).fetchall()
    channel_vocab = _channel_topical_vocab(conn)

    scored = []
    for p in profiles:
        subject_score = 1.0 if (p["subject"] and p["subject"] in source_subjects) else 0.0
        voice_score = voice_by_code.get(p["profile_code"], 0.0)
        topical_score = _topical_overlap_score(cand_words, channel_vocab.get(p["channel_id"]))
        blended = 0.6 * subject_score + 0.2 * voice_score + 0.2 * topical_score
        scored.append({
            "profile_code": p["profile_code"], "subject": p["subject"], "channel_name": p["channel_name"],
            "channel_id": p["channel_id"], "n_videos_analysed": p["n_videos_analysed"],
            "match_score": round(blended * 100),
        })
    scored.sort(key=lambda r: -r["match_score"])

    final, seen = [], set()
    if scored and scored[0]["channel_id"] in excluded_channel_ids:
        final.append(scored[0])
        seen.add(scored[0]["profile_code"])
    for r in scored:
        if len(final) >= 3:
            break
        if r["profile_code"] in seen or r["channel_id"] in excluded_channel_ids:
            continue
        final.append(r)
        seen.add(r["profile_code"])
    return final


@app.route("/api/swipe/action", methods=["POST"])
def api_swipe_action():
    payload = request.get_json(silent=True) or {}
    candidate_id = payload.get("candidate_id")
    action = payload.get("action")
    if not candidate_id or action not in ("like", "dislike"):
        return jsonify({"ok": False, "error": "'candidate_id' and action ('like'/'dislike') are required"}), 400

    conn = get_conn()
    row = conn.execute("SELECT * FROM swipe_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "no such candidate"}), 404

    status = "liked" if action == "like" else "disliked"
    conn.execute(
        "UPDATE swipe_candidates SET status = ?, decided_at = datetime('now') WHERE candidate_id = ?",
        (status, candidate_id),
    )
    conn.commit()

    matches = []
    if action == "like":
        matches = [
            {
                "profile_code": m["profile_code"], "subject": m["subject"],
                "channel_name": m["channel_name"], "n_videos_analysed": m["n_videos_analysed"],
                "match_score": m["match_score"],
            }
            for m in _score_profile_matches(conn, row)
        ]

    conn.close()
    return jsonify({"ok": True, "status": status, "matches": matches})


@app.route("/api/swipe/create", methods=["POST"])
def api_swipe_create():
    """Step 4 of the swipe loop: candidate liked, profile chosen, format
    chosen (Instagram / Short video / Long video / News) — generate the
    script and save it straight into Studio Creations, scored, the same
    way a normal Transform run does. Always builds the prompt from the
    candidate's own short pitch (never the full source book/video text) —
    see the 071c959 fix this follows on from."""
    payload = request.get_json(silent=True) or {}
    candidate_id = payload.get("candidate_id")
    profile_code = (payload.get("profile_code") or "").strip()
    form = (payload.get("format") or "").strip()

    if not candidate_id or not profile_code or form not in FORM_SPECS:
        return jsonify({"ok": False, "error": "'candidate_id', 'profile_code', and a valid 'format' are required"}), 400

    conn = get_conn()
    row = conn.execute("SELECT * FROM swipe_candidates WHERE candidate_id = ?", (candidate_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "no such candidate"}), 404
    profile = conn.execute("SELECT profile_id FROM style_profiles WHERE profile_code = ?", (profile_code,)).fetchone()
    conn.close()
    if not profile:
        return jsonify({"ok": False, "error": f"no such profile: {profile_code}"}), 404

    pitch_text = _build_pitch_text(row)
    title = row["hook"] or f"Swipe candidate #{candidate_id}"

    try:
        result = rate_input(pitch_text, title=title, source_label=f"Swipe candidate #{candidate_id}", input_type="swipe_candidate")
        test_id = result["test_id"]
        prompt_text = build_data_output_prompt(test_id, profile_code, form)
        gen_result = generate_transform(prompt_text)
        transformation_id = save_transformation(test_id, profile_code, gen_result["title"], gen_result["script"], generated_by="swipe")
        score_transformation(transformation_id)
    except LLMConfigError as e:
        return jsonify({"ok": False, "error": str(e)}), 503
    except Exception as e:
        return jsonify({"ok": False, "error": f"Generation failed: {e}"}), 500

    return jsonify({
        "ok": True,
        "transformation_id": transformation_id,
        "redirect_url": url_for("creation_detail", transformation_id=transformation_id),
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True, use_reloader=False, threaded=True)
