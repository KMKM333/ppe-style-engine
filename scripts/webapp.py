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
import re
import subprocess
import sys
from collections import Counter
from urllib.parse import quote

from flask import Flask, render_template, request, redirect, url_for, flash, session, send_file, abort, jsonify

from db_init import get_conn, BOOK_PAGES_DIR, BOOK_FILES_DIR, VIDEO_VISUALS_DIR
from feature_extraction import extract_auto_features, _sentences
from profile_builder import build_profile
from score_engine import rate_input, score_intrinsic, score_book_against_profiles, CROSS_MEDIA_SHARED_FIELDS
from transform import build_transform_prompt, build_data_output_prompt, save_transformation, score_transformation, FORM_SPECS
from hybrid_profile_builder import create_hybrid_profile
from llm_client import generate_transform, LLMConfigError
from ingest_book import ingest_book_text
from ingest_video import ingest_video_row

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


def _profiles():
    conn = get_conn()
    rows = conn.execute(
        """SELECT p.*, c.channel_name FROM style_profiles p
           JOIN channels c ON c.channel_id = p.channel_id
           ORDER BY p.profile_code"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
           WHERE vt.term IS NOT NULL AND vt.term != ''
           UNION ALL
           SELECT bt.term AS value, bt.definition AS description,
                  b.author AS source_name, p.profile_code AS profile_code,
                  NULL AS video_id, b.book_id AS book_id
           FROM book_terms bt
           JOIN books b ON b.book_id = bt.book_id
           LEFT JOIN channels c ON c.channel_name = b.author AND c.platform = 'Book'
           LEFT JOIN style_profiles p ON p.channel_id = c.channel_id
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
           WHERE ve.example_title IS NOT NULL AND ve.example_title != ''
           UNION ALL
           SELECT be.example_title AS value, be.example_text AS description,
                  b.author AS source_name, p.profile_code AS profile_code,
                  NULL AS video_id, b.book_id AS book_id, be.example_id AS example_id
           FROM book_examples be
           JOIN books b ON b.book_id = be.book_id
           LEFT JOIN channels c ON c.channel_name = b.author AND c.platform = 'Book'
           LEFT JOIN style_profiles p ON p.channel_id = c.channel_id
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
    ("book", "Books"),
    # YouTube videos and news articles will get their own kind here once ingestion exists for them.
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
                       c.channel_name, p.profile_code, a.word_count, a.title_format,
                       a.you_freq_per_100w, a.readability_score
                FROM videos v
                JOIN channels c ON c.channel_id = v.channel_id
                LEFT JOIN style_profiles p ON p.channel_id = c.channel_id
                LEFT JOIN video_attributes a ON a.video_id = v.video_id
                {where_sql}""",
            params,
        ).fetchall()
        for v in video_rows:
            rows.append({
                "kind": "video",
                "media_type": v["media_type"] or "Instagram",
                "channel_name": v["channel_name"],
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
            })

    if kind != "video" and not title_format:
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
    n_short_videos = conn.execute("SELECT COUNT(*) FROM videos WHERE media_type = 'Instagram'").fetchone()[0]
    n_short_video_accounts = conn.execute(
        "SELECT COUNT(DISTINCT channel_id) FROM videos WHERE media_type = 'Instagram'"
    ).fetchone()[0]
    n_long_videos_only = conn.execute("SELECT COUNT(*) FROM videos WHERE media_type = 'YouTube'").fetchone()[0]
    n_long_video_examples = conn.execute(
        "SELECT COUNT(*) FROM video_examples e JOIN videos v ON v.video_id = e.video_id WHERE v.media_type = 'YouTube'"
    ).fetchone()[0]
    n_long_videos = n_long_videos_only + n_long_video_examples
    n_news = 0  # news article ingestion not built yet
    # Matches the Dashboard's "Inputs ingested" count exactly: videos + books + book_examples,
    # and (like books) long-form videos here already bundles in their examples.
    n_all_inputs = n_short_videos + n_long_videos + n_books + n_news
    conn.close()

    return render_template(
        "inputs_list.html", active="inputs",
        rows=rows, channels=channels, title_formats=title_formats, total=total, kinds=INPUT_KINDS,
        all_terms=all_terms, all_examples=all_examples,
        n_books=n_books, n_books_only=n_books_only, n_book_examples=n_book_examples,
        n_short_videos=n_short_videos, n_short_video_accounts=n_short_video_accounts,
        n_long_videos=n_long_videos, n_long_videos_only=n_long_videos_only,
        n_long_video_examples=n_long_video_examples, n_news=n_news, n_all_inputs=n_all_inputs,
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

    # avoid FK violations on tables that reference this video, then delete it
    conn.execute("UPDATE test_scores SET match_video_id = NULL WHERE match_video_id = ?", (video_id,))
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
        """SELECT c.channel_id, c.channel_name, p.profile_code, p.subject, p.status, p.n_videos_analysed,
                  (SELECT COUNT(*) FROM videos v WHERE v.channel_id = c.channel_id) AS n_videos
           FROM channels c
           LEFT JOIN style_profiles p ON p.channel_id = c.channel_id
           WHERE c.platform = ?
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
    Transcriber hand one long-form YouTube video straight to the live
    engine. Ingestion is synchronous (an INSERT + local feature extraction),
    but classification/breakdown/visual-detection run in a detached
    subprocess, same rationale as api_ingest_book: a multi-minute LLM call
    inside an in-process thread risks gunicorn's arbiter recycling the
    worker mid-call, silently killing the thread with it."""
    if not INGEST_API_KEY or request.headers.get("X-Ingest-Key") != INGEST_API_KEY:
        abort(403)

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    script = (data.get("script") or "").strip()
    channel = (data.get("channel") or "").strip()
    if not title or not script or not channel:
        return jsonify({"ok": False, "error": "'title', 'script', and 'channel' are required."}), 400

    video_id = ingest_video_row(
        title=title,
        script=script,
        channel=channel,
        platform=data.get("platform") or "YouTube",
        url=data.get("url") or None,
        duration_sec=data.get("duration_sec") or None,
        posted_at=data.get("posted_at") or None,
        chapter_count=data.get("chapter_count"),
        timed_transcript=data.get("timed_transcript") or None,
        length_band=data.get("length_band") or "C",
    )

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_process_video.py")
    subprocess.Popen([sys.executable, script_path, "--video_id", str(video_id)], start_new_session=True)

    return jsonify({"ok": True, "video_id": video_id, "status": "ingested, classifying in the background"})


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
        "source": request.form.get("source", ""),
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
    conn = get_conn()
    video_rows = conn.execute(
        """SELECT v.video_id, v.title, c.channel_name, v.media_type, a.word_count
           FROM videos v JOIN channels c ON c.channel_id = v.channel_id
           LEFT JOIN video_attributes a ON a.video_id = v.video_id
           ORDER BY v.ingested_at DESC"""
    ).fetchall()
    book_rows = conn.execute(
        "SELECT book_id, title, author, word_count FROM books ORDER BY ingested_at DESC"
    ).fetchall()
    example_rows = conn.execute(
        """SELECT e.example_id, e.example_title, e.example_text, b.title AS book_title, b.author
           FROM book_examples e JOIN books b ON b.book_id = e.book_id
           ORDER BY b.author, b.title, e.example_id"""
    ).fetchall()
    conn.close()
    return video_rows, book_rows, example_rows


def _transform_input_options(video_rows, book_rows, example_rows):
    """Flattens the same three row sets the <select id="source"> renders from
    into one list the client-side search box can filter — labels mirror the
    <option> text exactly so picking a search result and picking straight
    from the dropdown always agree."""
    options = []
    for v in video_rows:
        label = f"{v['media_type'] or 'Video'} — {v['channel_name']} — {v['title']}"
        if v["word_count"]:
            label += f" ({v['word_count']}w)"
        options.append({"value": f"video:{v['video_id']}", "label": label, "kind": "Video"})
    for b in book_rows:
        label = f"Book — {b['author'] or 'Unknown'} — {b['title']}"
        if b["word_count"]:
            label += f" ({b['word_count']}w)"
        options.append({"value": f"book:{b['book_id']}", "label": label, "kind": "Book"})
    for e in example_rows:
        title = e["example_title"] or f"Example #{e['example_id']}"
        label = f"Example — {e['author'] or 'Unknown'} — {e['book_title']}: {title}"
        options.append({"value": f"example:{e['example_id']}", "label": label, "kind": "Example"})
    for o in options:
        o["search"] = o["label"].lower()
    return options


def _transform_profile_options(profiles):
    """Flattens the target-profile <select> options for the same
    search-box-over-a-select pattern as the Input field."""
    options = [
        {"value": p["profile_code"], "label": f"{p['profile_code']} ({p['channel_name']})", "kind": p.get("media_type") or ""}
        for p in profiles
    ]
    for o in options:
        o["search"] = o["label"].lower()
    return options


def _transform_output_form_options():
    options = [{"value": code, "label": spec["label"], "kind": ""} for code, spec in FORM_SPECS.items()]
    for o in options:
        o["search"] = o["label"].lower()
    return options


@app.route("/transform", methods=["GET"])
def transform_form():
    source = request.args.get("source", "").strip()
    pasted_title = request.args.get("pasted_title", "").strip()
    pasted_text = request.args.get("pasted_text", "").strip()
    profile = request.args.get("profile", default="", type=str)
    mode = request.args.get("mode", "transform")
    output_form = request.args.get("form", "").strip()

    video_rows, book_rows, example_rows = _transform_inputs()
    input_options = _transform_input_options(video_rows, book_rows, example_rows)
    profile_options = _transform_profile_options(_profiles())
    output_form_options = _transform_output_form_options()

    prompt_text = None
    selected_test_id = None
    original_text = None
    gen_title = gen_text = None
    if mode == "data_output" and not output_form and profile and (source or pasted_text):
        flash("Choose an output form to generate a data output.")
    elif profile and (source or pasted_text):
        try:
            if pasted_text:
                raw_text = pasted_text
                title = pasted_title or "(pasted text)"
                source_label = "Pasted text"
                input_type = "pasted_text"
            else:
                kind, _, sid_str = source.partition(":")
                sid = int(sid_str)
                conn = get_conn()
                if kind == "video":
                    row = conn.execute("SELECT title, script FROM videos WHERE video_id=?", (sid,)).fetchone()
                    conn.close()
                    if not row:
                        raise ValueError("No such video")
                    raw_text, title = row["script"], row["title"]
                    source_label, input_type = f"Video #{sid}: {title}", "video_script"
                elif kind == "book":
                    row = conn.execute("SELECT title, full_text FROM books WHERE book_id=?", (sid,)).fetchone()
                    conn.close()
                    if not row or not row["full_text"]:
                        raise ValueError("No such book, or book has no stored full text")
                    raw_text, title = row["full_text"], row["title"]
                    source_label, input_type = f"Book #{sid}: {title}", "book_text"
                elif kind == "example":
                    row = conn.execute(
                        "SELECT example_title, example_text FROM book_examples WHERE example_id=?", (sid,)
                    ).fetchone()
                    conn.close()
                    if not row:
                        raise ValueError("No such example")
                    title = row["example_title"] or f"Example #{sid}"
                    raw_text = row["example_text"]
                    source_label, input_type = f"Example #{sid}: {title}", "book_example"
                else:
                    conn.close()
                    raise ValueError("Choose an input or paste some text.")

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
        video_rows=video_rows, book_rows=book_rows, example_rows=example_rows,
        input_options=input_options, profile_options=profile_options, output_form_options=output_form_options,
        profiles=_profiles(),
        selected_source=source, selected_profile=profile, prompt_text=prompt_text,
        selected_test_id=selected_test_id, pasted_title=pasted_title, pasted_text=pasted_text,
        original_text=original_text, gen_title=gen_title, gen_text=gen_text,
        selected_mode=mode, selected_form=output_form, form_specs=FORM_SPECS,
    )


@app.route("/transform/generate", methods=["POST"])
def transform_generate():
    test_id = request.form.get("test_id", type=int)
    profile = request.form.get("profile", "")
    source = request.form.get("source", "")
    pasted_title = request.form.get("pasted_title", "")
    pasted_text = request.form.get("pasted_text", "")
    mode = request.form.get("mode", "transform")
    output_form = request.form.get("form", "").strip()

    video_rows, book_rows, example_rows = _transform_inputs()
    input_options = _transform_input_options(video_rows, book_rows, example_rows)
    profile_options = _transform_profile_options(_profiles())
    output_form_options = _transform_output_form_options()

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
        video_rows=video_rows, book_rows=book_rows, example_rows=example_rows,
        input_options=input_options, profile_options=profile_options, output_form_options=output_form_options,
        profiles=_profiles(),
        selected_source=source, selected_profile=profile, prompt_text=prompt_text,
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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050, debug=True, use_reloader=False, threaded=True)
