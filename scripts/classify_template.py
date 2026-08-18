"""
classify_template.py

The "Class" attributes (structure, hook type, rhetoric, taxonomy, CTA — the
subjective/interpretive half of the template) aren't computed with regex.
They're filled by an LLM pass. This file has two parts:

1. CLASSIFICATION_PROMPT — the fixed prompt to send per-video (or in small
   batches) to Claude (via API, Claude Code, or pasted into a chat) so that
   every video gets scored against the same rubric, consistently.

2. merge_classification() — takes the JSON Claude returns and writes it into
   video_attributes, alongside the auto features already sitting there.

Recommended workflow while you don't have API wiring set up:
  - Export a channel's un-classified videos with `export_for_classification()`
  - Paste the batch into a chat with Claude, using CLASSIFICATION_PROMPT
  - Save Claude's JSON array response to a .json file
  - Run `python3 classify_template.py --load results.json` to merge it in
"""
import argparse
import json
import sqlite3

from db_init import get_conn


CLASSIFICATION_PROMPT = """You are scoring a short-form PPE (philosophy/politics/economics)
explainer video script against a fixed rubric. For EACH video below, return one JSON object
with exactly these keys and allowed values. Do not add commentary outside the JSON array.

- beat_sequence: string, e.g. "Hook-Definition-Example-Close" (use your own beat labels,
  hyphen-joined, in order)
- formula_explicit: 0 or 1 — does the creator explicitly name their own structure aloud?
- hook_type: one of ["Rhetorical question", "Relatable scenario", "Thought experiment",
  "Name-drop claim", "Definition cold-open", "Bold/contrarian claim",
  "Credibility/I-did-the-work", "Interactive challenge/demo", "Other"]
- hook_names_source: 0 or 1 — does the opening paragraph name the philosopher/source?
- hook_source_word_position: integer — word index at which the source name first appears
  (null if never named in the hook)
- hook_ends_on_pivot: 0 or 1
- hook_self_demonstrating: 0 or 1 — does the hook perform the technique it's describing?
- close_type: one of ["Direct question", "Rhetorical mic-drop", "Practical takeaway",
  "Soft/light remark", "CTA-only"]
- callback_to_hook: 0 or 1
- citation_style: one of ["Named philosopher + direct quote", "Named study/data",
  "Anecdote only", "Unsourced assertion"]
- analogy_count: integer
- names_bias_or_law: 0 or 1
- named_bias_or_law_term: string or null
- dialectic_structure: 0 or 1
- certainty_register: one of ["Hedged", "Assertive", "Mixed"]
- rule_of_three_present: 0 or 1
- rule_of_three_count: integer
- rhetorical_mode: one of ["Story-led", "Advice-led", "Opinion-led", "Question-led/Exploratory"]
  — the overall communicative mode of the whole video: is it primarily telling a story,
  giving direct advice/instructions, asserting an opinion/take, or exploring a question
  out loud? Distinct from hook_type (just the opening) and explanation_mechanism (just
  the technique used to land one concept)
- explanation_mechanism: one of ["Physical demonstration/experiment", "Statistic/data-led",
  "Analogy-led", "Historical/anecdotal example", "Direct definition", "Thought experiment",
  "Case-study breakdown"]
  — the primary mechanism used to make the concept land, as distinct from concept_type
  (which is about the concept's own nature) and citation_style (about sourcing).
  "Case-study breakdown" = the video's entire structure is a step-by-step analysis of
  one specific named person's real communication/action (a speech, a scene, a meeting)
- domain: one of ["Philosophy", "Politics", "Economics", "Psychology-adjacent",
  "Science/Mathematics", "Communication/Rhetoric", "Technology", "Interdisciplinary"]
- concept_type: one of ["Thought experiment", "Named bias/effect/law",
  "Historical anecdote", "Direct quote explainer", "Current-events application",
  "Scientific/mathematical phenomenon", "Practical framework/how-to"]
- source_era: one of ["Ancient", "Early modern", "Modern", "Contemporary", "No named source"]
- framing: one of ["Practical/actionable", "Purely descriptive"]
- script_polish: one of ["Tightly edited", "Conversational/raw"]
- has_cta: 0 or 1
- cta_type: one of ["Comment-bait question", "Follow/subscribe",
  "Newsletter/product plug", "None"]
- cta_placement: one of ["Start", "Mid", "End", "None"]
- cta_count: integer

Ported from the book rubric (same fields, same allowed values, so a video and a book can be
compared directly on these dimensions):
- tone: one of ["Objective/neutral", "Passionate/advocacy", "Skeptical/critical", "Urgent/alarmist",
  "Wry/ironic", "Reverent/admiring"]
- emotional_register: one of ["Humorous/light", "Dark/somber", "Serious/grave", "Whimsical/playful",
  "Melancholic", "Uplifting/inspiring", "Mixed/variable"]
- narrative_voice: one of ["Distinct/idiosyncratic", "Neutral/generic", "Formal/detached narrator",
  "Conversational/intimate narrator", "Multiple distinct voices"]
- narrative_density: one of ["Story-led (mostly anecdote/narrative)", "Balanced", "Argument-led/abstract"]
- counter_argument_engagement: one of ["Ignored", "Strawmanned", "Acknowledged briefly",
  "Substantively engaged", "Steelmanned"]
- rhetorical_appeal_balance: one of ["Primarily logical/data-driven", "Primarily emotional/narrative",
  "Primarily credibility/authority-driven", "Balanced blend"]
- prose_rhythm: one of ["Staccato/choppy", "Flowing/poetic", "Balanced/mixed cadence", "Monotonous/uniform"]
- noun_verb_ratio_style: one of ["Verb-driven/dynamic", "Balanced", "Noun-heavy/nominalized (formal)",
  "Heavily nominalized/dense academic"]
- syntax_pattern: one of ["Short & simple, low variety", "Long & complex, low variety",
  "Highly varied (short and long mixed)", "Fragmented/experimental"]
- pacing: one of ["Fast/action-driven", "Slow/descriptive", "Balanced action & description",
  "Variable/uneven pacing"]
- polemical_tone: one of ["Polite/measured critique", "Firm but respectful", "Sharply critical",
  "Aggressive/dismissive of opposing views"]
- narrative_presence: one of ["Detached/impersonal (passive, third person)",
  "Occasional first-person ('I argue')", "Consistently participatory ('we must')",
  "Highly personal/confessional"]

New shared fields (also present on the book rubric, so scored the same way there):
- value_promise: one of ["Practical how-to/skill", "Surprising fact/reveal",
  "Emotional payoff/relatability", "Entertainment/humor", "Social currency/talking point",
  "Intellectual framework/mental model"] — what the piece implicitly promises the audience for
  sticking with it
- information_density: one of ["Fact-packed/dense", "Balanced", "Loose/lifestyle-focused"]
- curiosity_loop: 0 or 1 — does the piece open a question/tease early that's only resolved later
  or at the very end?
- relatability_factor: one of ["Specific everyday pain point", "Broad universal experience",
  "Niche/insider experience", "Abstract/impersonal, low relatability"]
- identity_framing: 0 or 1 — does the language invite the audience to see themselves in it
  (a "that's so me" framing), as opposed to describing a general/other case?
- contrarian_positioning: one of ["Explicitly contrarian/against consensus", "Mildly unconventional",
  "Aligned with mainstream view", "No clear positioning"]
- adjective_intensity: one of ["Extreme/superlative modifiers", "Moderate descriptive language",
  "Neutral/objective phrasing"]
- punctuation_delivery: one of ["Frequent rhetorical questions", "Exclamation-heavy/enthusiastic",
  "Trail-off/ellipsis-driven", "Measured/standard punctuation"]
- rhythmic_repetition: 0 or 1 — does the piece use anaphora (deliberately reusing the same phrase
  starter across consecutive lines/sentences for rhythmic effect)?
- vulnerability_depth: one of ["High — shares mistakes/failures openly", "Moderate — some personal
  admission", "Low/none — impersonal or purely authoritative"]
- condescension_vs_empowerment: one of ["Empowering/collaborative ('helpful friend')",
  "Neutral/informational", "Condescending/gatekeeping ('the guru')"]

New video-only fields (no book-rubric equivalent):
- structure_archetype: one of ["Problem to solution", "Listicle/numbered list", "Story-led narrative",
  "Tutorial/how-to walkthrough", "Myth-bust/correction", "Comparison/versus", "Single-concept explainer"]
- shareability_trigger: one of ["Highly opinionated take", "Saveable cheat-sheet/reference",
  "Deeply validating statement", "Surprising/counterintuitive fact", "None/low shareability"]
- product_placement: one of ["None", "Organic/subtle mention", "Explicit/obvious plug"]
- core_value_reinforcement: 0 or 1 — does the script repeat/reinforce a consistent tagline,
  catchphrase, or core message, as opposed to a one-off point?
- status_signaling: one of ["None/low", "Implies sophistication/being ahead of the curve",
  "Implies belonging/in-group membership", "Implies practical competence"]
- niche_slang_usage: one of ["None/general audience language", "Light community jargon",
  "Heavy in-group slang/jargon"]

Long-form-only fields — ONLY score these for long-form videos (roughly 15+ minutes runtime).
For a short-form (under ~3 minute) script, return null for every field in this group; a
60-second clip has no cold open, acts, or sponsor read to score:
- cold_open_present: 0 or 1 — does the video open with a teaser/preview moment before its
  real intro (e.g. a quick flash of the payoff or a dramatic clip) rather than starting
  directly on the main content?
- intro_length_sec: number or null — estimated seconds of runtime before the main content/
  thesis actually starts (base this on word count and typical speaking pace, ~150 words/min,
  since you only have the transcript, not the audio)
- act_count: integer — number of distinct large structural movements/acts the video moves
  through (NOT the same as beat_count's micro-beats — think "3 acts", not "12 paragraphs")
- re_engagement_hook_count: integer — count of mid-video re-hooks ("but here's where it gets
  interesting", "stick around because...", "and this is the part nobody talks about") used
  to re-capture attention and fight drop-off partway through a long runtime
- outro_type: one of ["Summary/recap", "CTA-stack (subscribe+like+comment chained together)",
  "Cliffhanger/teaser for next video", "Soft/abrupt end", "Personal sign-off"]
- pacing_arc: one of ["Steady throughout", "Accelerating toward the end", "Slows then quickens",
  "Front-loaded then coasts", "Uneven/inconsistent"] — how pacing changes across the FULL
  runtime, distinct from the "pacing" field above (which is a single overall qualitative read)
- topic_shift_count: integer — number of distinct sub-topics the video covers (a tight
  single-concept video scores 1; a wide-ranging essay covering many angles scores higher)

Return a JSON array, one object per video, each including "video_id" (matching the id given)
plus all fields above.

VIDEOS:
{videos_block}
"""


def build_prompt(rows):
    """rows: list of sqlite3.Row with video_id, title, script"""
    blocks = []
    for r in rows:
        blocks.append(f'--- video_id: {r["video_id"]} ---\nTitle: {r["title"]}\nScript:\n{r["script"]}\n')
    return CLASSIFICATION_PROMPT.format(videos_block="\n".join(blocks))


def export_for_classification(channel_name=None, limit=20, media_type=None):
    """limit defaults to 20, tuned for ~250-word Instagram scripts. A 30-40
    minute YouTube transcript runs 4,000-6,000+ words, so batching 20 of
    those into one prompt is impractical — pass a much smaller --limit
    (1-3) when media_type='YouTube'."""
    conn = get_conn()
    q = """
        SELECT v.video_id, v.title, v.script
        FROM videos v
        JOIN video_attributes a ON a.video_id = v.video_id
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE a.hook_type IS NULL
    """
    params = []
    if channel_name:
        q += " AND c.channel_name = ?"
        params.append(channel_name)
    if media_type:
        q += " AND v.media_type = ?"
        params.append(media_type)
    q += " LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    if not rows:
        print("No un-classified videos found for that filter.")
        return None
    prompt = build_prompt(rows)
    print(prompt)
    return prompt


CLASS_FIELDS = [
    "beat_sequence", "formula_explicit", "hook_type", "hook_names_source",
    "hook_source_word_position", "hook_ends_on_pivot", "hook_self_demonstrating",
    "close_type", "callback_to_hook", "citation_style", "analogy_count",
    "names_bias_or_law", "named_bias_or_law_term", "dialectic_structure",
    "certainty_register", "rule_of_three_present", "rule_of_three_count",
    "rhetorical_mode", "explanation_mechanism", "domain", "concept_type", "source_era", "framing",
    "script_polish", "has_cta", "cta_type", "cta_placement", "cta_count",
    # ported from the book rubric (same fields/values, for direct cross-media comparison)
    "tone", "emotional_register", "narrative_voice", "narrative_density",
    "counter_argument_engagement", "rhetorical_appeal_balance", "prose_rhythm",
    "noun_verb_ratio_style", "syntax_pattern", "pacing", "polemical_tone", "narrative_presence",
    # new shared fields (also on the book rubric)
    "value_promise", "information_density", "curiosity_loop", "relatability_factor",
    "identity_framing", "contrarian_positioning", "adjective_intensity", "punctuation_delivery",
    "rhythmic_repetition", "vulnerability_depth", "condescension_vs_empowerment",
    # new video-only fields
    "structure_archetype", "shareability_trigger", "product_placement",
    "core_value_reinforcement", "status_signaling", "niche_slang_usage",
    # long-form-only fields (null on short-form Instagram rows)
    "cold_open_present", "intro_length_sec", "act_count", "re_engagement_hook_count",
    "outro_type", "pacing_arc", "topic_shift_count",
]


def merge_classification(json_path):
    with open(json_path) as f:
        results = json.load(f)

    conn = get_conn()
    n = 0
    for r in results:
        video_id = r["video_id"]
        set_clause = ", ".join([f"{k} = ?" for k in CLASS_FIELDS if k in r])
        values = [r[k] for k in CLASS_FIELDS if k in r]
        values.append("claude")  # classified_by
        conn.execute(
            f"UPDATE video_attributes SET {set_clause}, classified_by = ? WHERE video_id = ?",
            (*values, video_id),
        )
        n += 1
    conn.commit()
    conn.close()
    print(f"Merged classification for {n} videos.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true", help="print the prompt for un-classified videos")
    ap.add_argument("--channel", default=None)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--media_type", default=None, help="e.g. YouTube — filters to one media type, "
                     "and you should pass a much smaller --limit alongside it (scripts run far longer)")
    ap.add_argument("--load", default=None, help="path to a JSON file of Claude's classification results")
    args = ap.parse_args()

    if args.export:
        export_for_classification(args.channel, args.limit, args.media_type)
    elif args.load:
        merge_classification(args.load)
    else:
        print("Use --export to get a prompt, or --load results.json to merge results.")
