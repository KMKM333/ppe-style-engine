"""
classify_book_template.py

The book-analysis counterpart to classify_template.py. Every field in
book_attributes is qualitative/interpretive, so — same as the video "Class"
attributes — none of it is computed with regex. It's filled by an LLM pass
against a fixed rubric, so every book gets scored consistently and can be
cross-referenced against other books and against the video-creator style
profiles on the same subject.

This file has three parts:

1. BOOK_CLASSIFICATION_PROMPT — the fixed prompt to send per-book to Claude
   (via API, Claude Code, or pasted into a chat) so every book is scored
   against the same 42-attribute rubric, plus a full chapter/section
   breakdown (topics, key points, terms, and examples per section).

2. merge_book_classification() — takes the JSON Claude returns and writes it
   into book_attributes, book_sections, book_points, book_terms, and
   book_examples (each example/point/term is attributed to its section).

3. export_book_for_classification() — pulls a book's stored text (or prints
   a reminder to paste it in, if it wasn't stored locally) into the prompt.

Recommended workflow, same as videos:
  - Register the book with ingest_book.py first (creates the books row)
  - Export it for classification with `--export --book_id N`
  - Paste the batch into a chat with Claude, using BOOK_CLASSIFICATION_PROMPT
  - Save Claude's JSON response to a .json file
  - Run `python3 classify_book_template.py --load results.json` to merge it in
"""
import argparse
import json

from db_init import get_conn


BOOK_CLASSIFICATION_PROMPT = """You are analysing a non-fiction book against a fixed qualitative rubric, the
same way short-form video scripts get scored in this engine, so the results can be cross-referenced against
each other and against creator style profiles on the same subject. For EACH book below, return one JSON object
with exactly these keys and allowed values. Do not add commentary outside the JSON array.

Thesis & Purpose
- thesis_statement: string — 1-2 sentence plain-language summary of the book's central argument/claim
- primary_goal: one of ["Inform/Explain", "Persuade/Argue", "Critique/Deconstruct", "Instruct/Prescribe"]

Evidence & Authority
- primary_evidence_type: one of ["Statistics/data", "Expert interviews", "Historical documents/archival",
  "Personal anecdote/memoir", "Case studies", "Scientific studies/citations",
  "Philosophical argument (no external evidence)", "Mixed/multiple"] — the DOMINANT evidence type used
- secondary_evidence_types: string or null — comma-separated other evidence types also present

Tone & Voice
- tone: one of ["Objective/neutral", "Passionate/advocacy", "Skeptical/critical", "Urgent/alarmist",
  "Wry/ironic", "Reverent/admiring"]
- emotional_register: one of ["Humorous/light", "Dark/somber", "Serious/grave", "Whimsical/playful",
  "Melancholic", "Uplifting/inspiring", "Mixed/variable"] — the book's overall emotional feel, distinct
  from `tone` above (which is about persuasive stance, not emotional register)
- narrative_voice: one of ["Distinct/idiosyncratic", "Neutral/generic", "Formal/detached narrator",
  "Conversational/intimate narrator", "Multiple distinct voices"] — how the narrator/author sounds
- polemical_tone: one of ["Polite/measured critique", "Firm but respectful", "Sharply critical",
  "Aggressive/dismissive of opposing views"] — how adversarial the language gets toward opposing views
- narrative_presence: one of ["Detached/impersonal (passive, third person)",
  "Occasional first-person ('I argue')", "Consistently participatory ('we must')",
  "Highly personal/confessional"] — how much the author inserts themselves into the text

Structure & Organization
- structure_style: one of ["Linear/chronological", "Thematic (non-chronological)",
  "Building framework (each chapter = one building block)", "Case-study anthology (loosely linked chapters)",
  "Argument + rebuttal", "Problem -> mechanism -> solution"]
- uses_visual_aids: 0 or 1 — charts, diagrams, or data visuals used to clarify concepts
- subheading_density: one of ["None/sparse", "Moderate", "Heavy/highly scannable"]

Target Audience
- target_audience: one of ["General public", "Educated lay reader", "Practitioners/professionals in the field",
  "Academic/specialist", "Policy makers", "Students"]
- vocabulary_complexity: one of ["Plain language", "Some field terms, defined inline",
  "Assumes prior domain knowledge", "Heavy jargon"]
- jargon_accessibility: one of ["Plain, no jargon", "Light jargon, mostly defined",
  "Moderate jargon, some undefined", "Heavy jargon, assumes expertise"] — specifically whether
  specialized terminology gets defined in-text (via definitions or analogies), not just its density

Bias & Assumptions
- bias_assumptions: string — hidden viewpoints, missing counterarguments, or unstated beliefs influencing the text

Additional dimensions
- counter_argument_engagement: one of ["Ignored", "Strawmanned", "Acknowledged briefly",
  "Substantively engaged", "Steelmanned"] — how seriously the book engages views it disagrees with
- argument_architecture: one of ["Deductive (general principle -> specific cases)",
  "Inductive (specific cases -> general principle)", "Dialectical (thesis-antithesis-synthesis)",
  "Narrative-driven", "Comparative/case-based", "Historical-chronological"]
- prescriptiveness: one of ["Purely descriptive", "Diagnostic with implied direction",
  "Explicit prescriptions/policy recommendations", "How-to/practical playbook"]
- temporal_orientation: one of ["Backward-looking/historical", "Present-diagnostic",
  "Forward-looking/predictive", "Cyclical (uses history to forecast)"]
- interdisciplinary_fields: string or null — comma-separated secondary subjects/disciplines the book draws
  on beyond its primary subject (e.g. a Politics book that also draws heavily on Psychology)
- named_frameworks_coined: string or null — comma-separated original models/terms/frameworks the book itself
  introduces (its own new vocabulary — not concepts it's just citing from elsewhere)
- narrative_density: one of ["Story-led (mostly anecdote/narrative)", "Balanced", "Argument-led/abstract"]
- claim_falsifiability: one of ["Highly empirical/testable", "Mixed", "Primarily normative/philosophical"]
- ideological_positioning: string or null — where the book sits on a subject-relevant spectrum, stated in
  its own terms (e.g. "free-market skeptic", "progressive institutionalist", "techno-optimist")
- rhetorical_appeal_balance: one of ["Primarily logical/data-driven", "Primarily emotional/narrative",
  "Primarily credibility/authority-driven", "Balanced blend"]
- thesis_consistency: one of ["Tight and consistent throughout", "Expands but stays coherent",
  "Drifts/loses focus by the end"]
- comparative_positioning: string or null — other named books/thinkers/schools of thought the book
  explicitly positions itself against or builds on
- citation_density: one of ["Heavily footnoted/academic", "Moderately sourced", "Light/conversational sourcing",
  "Essayistic/unsourced opinion"]
- argumentative_density: one of ["Low (few logical transitions)", "Moderate", "High (frequent transitions)",
  "Very high/dense argumentation"] — frequency of logical transition words (therefore, however,
  consequently, moreover)
- abstraction_concreteness_balance: one of ["Highly abstract, few concrete examples",
  "Balanced abstract & concrete", "Highly concrete, grounded in examples",
  "Alternates deliberately between abstract and concrete"] — how abstract concepts (e.g. justice,
  equilibrium) are cross-referenced against concrete examples (e.g. a courtroom, a market stall)
- hedging_vs_assertion: one of ["Highly hedged/speculative", "Balanced hedging and assertion",
  "Assertive with occasional hedges", "Highly assertive/absolute"] — speculative qualifiers
  ("it appears that", "perhaps", "likely") versus absolute declarations ("undeniably", "always", "proves")
- rhetorical_questioning: one of ["Rare/absent", "Used to transition between ideas",
  "Used to challenge reader's assumptions", "Frequent — both transitional and challenging"]

Style & Craft
- diction: one of ["Simple/plain", "Moderate/accessible", "Complex/elevated", "Formal",
  "Old-fashioned/archaic"] — the dominant register of word choice
- syntax_pattern: one of ["Short & simple, low variety", "Long & complex, low variety",
  "Highly varied (short and long mixed)", "Fragmented/experimental"] — sentence length, variety, and
  grammar patterns
- pacing: one of ["Fast/action-driven", "Slow/descriptive", "Balanced action & description",
  "Variable/uneven pacing"] — how fast or slow scenes/sections move, action versus description
- sensory_language_density: one of ["Minimal/abstract", "Occasional sensory detail",
  "Rich/immersive sensory detail", "Saturated with sensory imagery"] — how often the text invokes
  sight, sound, smell, taste, and touch
- narrative_distance: one of ["Stream-of-consciousness/intimate", "Close/internal", "Moderate distance",
  "Detached/observational", "Omniscient/distant"] — how close the reader is to the character's/subject's
  thoughts
- figurative_language_density: one of ["Sparse/literal", "Occasional figurative language",
  "Frequent figurative language", "Dense/highly figurative"] — density of metaphors, similes, and
  personification
- prose_rhythm: one of ["Staccato/choppy", "Flowing/poetic", "Balanced/mixed cadence",
  "Monotonous/uniform"] — sentence cadence
- noun_verb_ratio_style: one of ["Verb-driven/dynamic", "Balanced", "Noun-heavy/nominalized (formal)",
  "Heavily nominalized/dense academic"] — whether the prose leans on nouns/nominalizations
  (e.g. "globalization", "quantification") for a heavier, more formal style, or stays verb-driven
- cognitive_metaphor_domain: one of ["Organic/biological (growth, health, decay)",
  "Mechanical/engineering (gears, friction, leverage)", "Journey/spatial (path, direction, movement)",
  "Combat/competition (battle, war, fight)", "Ecosystem/network", "Mixed/no dominant domain"] — the
  primary metaphorical domain used to explain systems or ideas

Cross-media shared fields (also scored this way for short-form video, so a book and a video can be
compared directly on these dimensions)
- value_promise: one of ["Practical how-to/skill", "Surprising fact/reveal",
  "Emotional payoff/relatability", "Entertainment/humor", "Social currency/talking point",
  "Intellectual framework/mental model"] — what the book implicitly promises the reader for sticking
  with it
- information_density: one of ["Fact-packed/dense", "Balanced", "Loose/lifestyle-focused"]
- curiosity_loop: 0 or 1 — does the book open a question/tease early (e.g. in the intro) that's only
  resolved later or at the very end?
- relatability_factor: one of ["Specific everyday pain point", "Broad universal experience",
  "Niche/insider experience", "Abstract/impersonal, low relatability"]
- identity_framing: 0 or 1 — does the language invite the reader to see themselves in it (a "that's
  so me" framing), as opposed to describing a general/other case?
- contrarian_positioning: one of ["Explicitly contrarian/against consensus", "Mildly unconventional",
  "Aligned with mainstream view", "No clear positioning"]
- adjective_intensity: one of ["Extreme/superlative modifiers", "Moderate descriptive language",
  "Neutral/objective phrasing"]
- punctuation_delivery: one of ["Frequent rhetorical questions", "Exclamation-heavy/enthusiastic",
  "Trail-off/ellipsis-driven", "Measured/standard punctuation"]
- rhythmic_repetition: 0 or 1 — does the book use anaphora (deliberately reusing the same phrase
  starter across consecutive lines/sentences for rhythmic effect)?
- vulnerability_depth: one of ["High — shares mistakes/failures openly", "Moderate — some personal
  admission", "Low/none — impersonal or purely authoritative"]
- condescension_vs_empowerment: one of ["Empowering/collaborative ('helpful friend')",
  "Neutral/informational", "Condescending/gatekeeping ('the guru')"]

Chapter/section breakdown
- sections: array of objects, one per chapter or major section of the book, IN ORDER, each with:
  - section_number: integer — order within the book (1, 2, 3, ...)
  - section_title: string — the chapter/section title or heading as the book gives it
  - summary: string — a 1-2 sentence plain-language soundbite of what this section covers
  - topics: array of strings — short topic tags for this section (e.g. ["inflation", "central banking"])
  - points: array of strings — the key arguments/claims made in this section, one per distinct point.
    Pull out every distinct point, not just the headline one — this is meant to be a reusable reference,
    not a highlight reel.
  - terms: array of objects, one per term/concept this section defines or introduces, each with:
    "term" (string) and "definition" (string — how the book itself defines or uses it)
  - examples: array of objects, one per distinct example/case study used in this section to reinforce a
    point, each with:
    - "example_title" (string, 3-7 words — a short, punchy label for the example, styled like a
      short-form video title: concrete and specific, e.g. "EU vs. Microsoft's Fines" or "The 10-Cent
      Placebo Pill", not a generic description)
    - "example_text" (string, 2-3 sentences written up in plain language — what the example is, its
      key specifics, and why it's a notable/useful illustration)
    - "reinforces_point" (string, 1-2 sentences — which point above it supports AND how/why it
      demonstrates that point, i.e. the mechanism or logic connecting the example to the point, not
      just a restatement of the point)

Return a JSON array, one object per book, each including "book_id" (matching the id given) plus all fields above.

BOOKS:
{books_block}
"""


# Controlled-vocabulary allowed values for every enum field in
# BOOK_CLASSIFICATION_PROMPT above — kept in sync with it by hand. Used by
# validate_book_classification() to catch a model reply that hallucinates a
# value outside the fixed rubric before it ever reaches the database.
BOOK_CLASS_ALLOWED_VALUES = {
    "primary_goal": ["Inform/Explain", "Persuade/Argue", "Critique/Deconstruct", "Instruct/Prescribe"],
    "primary_evidence_type": ["Statistics/data", "Expert interviews", "Historical documents/archival",
        "Personal anecdote/memoir", "Case studies", "Scientific studies/citations",
        "Philosophical argument (no external evidence)", "Mixed/multiple"],
    "tone": ["Objective/neutral", "Passionate/advocacy", "Skeptical/critical", "Urgent/alarmist",
        "Wry/ironic", "Reverent/admiring"],
    "structure_style": ["Linear/chronological", "Thematic (non-chronological)",
        "Building framework (each chapter = one building block)", "Case-study anthology (loosely linked chapters)",
        "Argument + rebuttal", "Problem -> mechanism -> solution"],
    "uses_visual_aids": [0, 1],
    "subheading_density": ["None/sparse", "Moderate", "Heavy/highly scannable"],
    "target_audience": ["General public", "Educated lay reader", "Practitioners/professionals in the field",
        "Academic/specialist", "Policy makers", "Students"],
    "vocabulary_complexity": ["Plain language", "Some field terms, defined inline",
        "Assumes prior domain knowledge", "Heavy jargon"],
    "counter_argument_engagement": ["Ignored", "Strawmanned", "Acknowledged briefly",
        "Substantively engaged", "Steelmanned"],
    "argument_architecture": ["Deductive (general principle -> specific cases)",
        "Inductive (specific cases -> general principle)", "Dialectical (thesis-antithesis-synthesis)",
        "Narrative-driven", "Comparative/case-based", "Historical-chronological"],
    "prescriptiveness": ["Purely descriptive", "Diagnostic with implied direction",
        "Explicit prescriptions/policy recommendations", "How-to/practical playbook"],
    "temporal_orientation": ["Backward-looking/historical", "Present-diagnostic",
        "Forward-looking/predictive", "Cyclical (uses history to forecast)"],
    "narrative_density": ["Story-led (mostly anecdote/narrative)", "Balanced", "Argument-led/abstract"],
    "claim_falsifiability": ["Highly empirical/testable", "Mixed", "Primarily normative/philosophical"],
    "rhetorical_appeal_balance": ["Primarily logical/data-driven", "Primarily emotional/narrative",
        "Primarily credibility/authority-driven", "Balanced blend"],
    "thesis_consistency": ["Tight and consistent throughout", "Expands but stays coherent",
        "Drifts/loses focus by the end"],
    "citation_density": ["Heavily footnoted/academic", "Moderately sourced", "Light/conversational sourcing",
        "Essayistic/unsourced opinion"],
    "argumentative_density": ["Low (few logical transitions)", "Moderate", "High (frequent transitions)",
        "Very high/dense argumentation"],
    "abstraction_concreteness_balance": ["Highly abstract, few concrete examples", "Balanced abstract & concrete",
        "Highly concrete, grounded in examples", "Alternates deliberately between abstract and concrete"],
    "hedging_vs_assertion": ["Highly hedged/speculative", "Balanced hedging and assertion",
        "Assertive with occasional hedges", "Highly assertive/absolute"],
    "rhetorical_questioning": ["Rare/absent", "Used to transition between ideas",
        "Used to challenge reader's assumptions", "Frequent — both transitional and challenging"],
    "diction": ["Simple/plain", "Moderate/accessible", "Complex/elevated", "Formal", "Old-fashioned/archaic"],
    "syntax_pattern": ["Short & simple, low variety", "Long & complex, low variety",
        "Highly varied (short and long mixed)", "Fragmented/experimental"],
    "pacing": ["Fast/action-driven", "Slow/descriptive", "Balanced action & description", "Variable/uneven pacing"],
    "sensory_language_density": ["Minimal/abstract", "Occasional sensory detail", "Rich/immersive sensory detail",
        "Saturated with sensory imagery"],
    "narrative_distance": ["Stream-of-consciousness/intimate", "Close/internal", "Moderate distance",
        "Detached/observational", "Omniscient/distant"],
    "figurative_language_density": ["Sparse/literal", "Occasional figurative language",
        "Frequent figurative language", "Dense/highly figurative"],
    "prose_rhythm": ["Staccato/choppy", "Flowing/poetic", "Balanced/mixed cadence", "Monotonous/uniform"],
    "noun_verb_ratio_style": ["Verb-driven/dynamic", "Balanced", "Noun-heavy/nominalized (formal)",
        "Heavily nominalized/dense academic"],
    "jargon_accessibility": ["Plain, no jargon", "Light jargon, mostly defined", "Moderate jargon, some undefined",
        "Heavy jargon, assumes expertise"],
    "cognitive_metaphor_domain": ["Organic/biological (growth, health, decay)",
        "Mechanical/engineering (gears, friction, leverage)", "Journey/spatial (path, direction, movement)",
        "Combat/competition (battle, war, fight)", "Ecosystem/network", "Mixed/no dominant domain"],
    "polemical_tone": ["Polite/measured critique", "Firm but respectful", "Sharply critical",
        "Aggressive/dismissive of opposing views"],
    "narrative_presence": ["Detached/impersonal (passive, third person)", "Occasional first-person ('I argue')",
        "Consistently participatory ('we must')", "Highly personal/confessional"],
    "emotional_register": ["Humorous/light", "Dark/somber", "Serious/grave", "Whimsical/playful",
        "Melancholic", "Uplifting/inspiring", "Mixed/variable"],
    "narrative_voice": ["Distinct/idiosyncratic", "Neutral/generic", "Formal/detached narrator",
        "Conversational/intimate narrator", "Multiple distinct voices"],
    # cross-media shared fields
    "value_promise": ["Practical how-to/skill", "Surprising fact/reveal",
        "Emotional payoff/relatability", "Entertainment/humor", "Social currency/talking point",
        "Intellectual framework/mental model"],
    "information_density": ["Fact-packed/dense", "Balanced", "Loose/lifestyle-focused"],
    "curiosity_loop": [0, 1],
    "relatability_factor": ["Specific everyday pain point", "Broad universal experience",
        "Niche/insider experience", "Abstract/impersonal, low relatability"],
    "identity_framing": [0, 1],
    "contrarian_positioning": ["Explicitly contrarian/against consensus", "Mildly unconventional",
        "Aligned with mainstream view", "No clear positioning"],
    "adjective_intensity": ["Extreme/superlative modifiers", "Moderate descriptive language",
        "Neutral/objective phrasing"],
    "punctuation_delivery": ["Frequent rhetorical questions", "Exclamation-heavy/enthusiastic",
        "Trail-off/ellipsis-driven", "Measured/standard punctuation"],
    "rhythmic_repetition": [0, 1],
    "vulnerability_depth": ["High — shares mistakes/failures openly", "Moderate — some personal admission",
        "Low/none — impersonal or purely authoritative"],
    "condescension_vs_empowerment": ["Empowering/collaborative ('helpful friend')",
        "Neutral/informational", "Condescending/gatekeeping ('the guru')"],
}


def validate_book_classification(results):
    """Checks a parsed classification reply (the JSON array Claude returns)
    against BOOK_CLASS_ALLOWED_VALUES plus basic structural requirements on
    `sections`. Returns a list of human-readable error strings — empty means
    the reply is safe to merge with merge_book_classification_results()."""
    errors = []
    if not isinstance(results, list) or not results:
        return ["Expected a non-empty JSON array of book classification objects."]

    for r in results:
        book_id = r.get("book_id", "?")
        if not isinstance(r, dict):
            errors.append(f"book_id {book_id}: entry is not a JSON object")
            continue
        if "book_id" not in r:
            errors.append("entry missing book_id")

        for field, allowed in BOOK_CLASS_ALLOWED_VALUES.items():
            val = r.get(field)
            if val is None:
                errors.append(f"book_id {book_id}: missing {field}")
            elif val not in allowed:
                errors.append(f"book_id {book_id}: invalid {field}={val!r}, not in {allowed}")

        sections = r.get("sections")
        if not sections or not isinstance(sections, list):
            errors.append(f"book_id {book_id}: missing or empty 'sections' array")
            continue
        for sec in sections:
            if not sec.get("section_title"):
                errors.append(f"book_id {book_id}: section missing section_title: {sec.get('section_number')}")
            for ex in sec.get("examples", []):
                if not ex.get("example_text") or not ex.get("reinforces_point"):
                    errors.append(
                        f"book_id {book_id}: incomplete example in section {sec.get('section_number')}: {ex}"
                    )

    return errors


def build_book_prompt(rows):
    """rows: list of sqlite3.Row with book_id, title, author, full_text"""
    blocks = []
    for r in rows:
        text = r["full_text"] or "[full text not stored locally — paste it in before running this prompt]"
        blocks.append(f'--- book_id: {r["book_id"]} ---\nTitle: {r["title"]}\nAuthor: {r["author"] or "Unknown"}\nText:\n{text}\n')
    return BOOK_CLASSIFICATION_PROMPT.format(books_block="\n".join(blocks))


def export_book_for_classification(book_id=None, limit=5):
    conn = get_conn()
    q = "SELECT b.book_id, b.title, b.author, b.full_text FROM books b LEFT JOIN book_attributes a ON a.book_id = b.book_id WHERE a.book_id IS NULL OR a.classified_by = 'pending'"
    params = []
    if book_id:
        q += " AND b.book_id = ?"
        params.append(book_id)
    q += " LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    if not rows:
        print("No un-classified books found for that filter.")
        return None
    prompt = build_book_prompt(rows)
    print(prompt)
    return prompt


BOOK_CLASS_FIELDS = [
    "thesis_statement", "primary_goal", "primary_evidence_type", "secondary_evidence_types",
    "tone", "structure_style", "uses_visual_aids", "subheading_density",
    "target_audience", "vocabulary_complexity", "bias_assumptions",
    "counter_argument_engagement", "argument_architecture", "prescriptiveness",
    "temporal_orientation", "interdisciplinary_fields", "named_frameworks_coined",
    "narrative_density", "claim_falsifiability", "ideological_positioning",
    "rhetorical_appeal_balance", "thesis_consistency", "comparative_positioning",
    "citation_density",
    # Style & Craft (18 new)
    "emotional_register", "narrative_voice", "polemical_tone", "narrative_presence",
    "jargon_accessibility", "argumentative_density", "abstraction_concreteness_balance",
    "hedging_vs_assertion", "rhetorical_questioning",
    "diction", "syntax_pattern", "pacing", "sensory_language_density", "narrative_distance",
    "figurative_language_density", "prose_rhythm", "noun_verb_ratio_style", "cognitive_metaphor_domain",
    # cross-media shared fields (also scored this way for video)
    "value_promise", "information_density", "curiosity_loop", "relatability_factor",
    "identity_framing", "contrarian_positioning", "adjective_intensity", "punctuation_delivery",
    "rhythmic_repetition", "vulnerability_depth", "condescension_vs_empowerment",
]


def merge_book_classification(json_path):
    with open(json_path) as f:
        results = json.load(f)
    return merge_book_classification_results(results)


def merge_book_classification_results(results):
    """Same as merge_book_classification(), but takes already-parsed JSON —
    used by auto_process_book.py, which gets the classification straight
    from the Anthropic API response instead of a file on disk."""
    conn = get_conn()
    n_books = n_sections = n_points = n_terms = n_examples = 0
    for r in results:
        book_id = r["book_id"]
        fields = [k for k in BOOK_CLASS_FIELDS if k in r]
        set_clause = ", ".join([f"{k} = ?" for k in fields])
        values = [r[k] for k in fields]

        existing = conn.execute("SELECT book_id FROM book_attributes WHERE book_id = ?", (book_id,)).fetchone()
        if existing:
            conn.execute(
                f"UPDATE book_attributes SET {set_clause}, classified_by = ?, classified_at = datetime('now') WHERE book_id = ?",
                (*values, "claude", book_id),
            )
        else:
            cols = ", ".join(["book_id", *fields, "classified_by"])
            placeholders = ", ".join(["?"] * (len(fields) + 2))
            conn.execute(
                f"INSERT INTO book_attributes ({cols}, classified_at) VALUES ({placeholders}, datetime('now'))",
                (book_id, *values, "claude"),
            )
        n_books += 1

        # clear any previous section breakdown for this book before re-inserting,
        # so re-running classification on the same book doesn't duplicate rows
        old_section_ids = [row["section_id"] for row in conn.execute(
            "SELECT section_id FROM book_sections WHERE book_id = ?", (book_id,)
        ).fetchall()]
        if old_section_ids:
            conn.execute("DELETE FROM book_points WHERE book_id = ?", (book_id,))
            conn.execute("DELETE FROM book_terms WHERE book_id = ?", (book_id,))
            conn.execute("DELETE FROM book_examples WHERE book_id = ?", (book_id,))
            conn.execute("DELETE FROM book_sections WHERE book_id = ?", (book_id,))

        for sec in r.get("sections", []):
            cur = conn.execute(
                "INSERT INTO book_sections (book_id, section_number, section_title, summary, topics, diagram_svg) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (book_id, sec.get("section_number"), sec.get("section_title"), sec.get("summary"),
                 ", ".join(sec.get("topics", [])) if sec.get("topics") else None,
                 sec.get("diagram_svg")),
            )
            section_id = cur.lastrowid
            n_sections += 1

            for point_text in sec.get("points", []):
                conn.execute(
                    "INSERT INTO book_points (book_id, section_id, point_text) VALUES (?, ?, ?)",
                    (book_id, section_id, point_text),
                )
                n_points += 1

            for term in sec.get("terms", []):
                conn.execute(
                    "INSERT INTO book_terms (book_id, section_id, term, definition) VALUES (?, ?, ?, ?)",
                    (book_id, section_id, term.get("term"), term.get("definition")),
                )
                n_terms += 1

            for ex in sec.get("examples", []):
                conn.execute(
                    "INSERT INTO book_examples (book_id, section_id, example_title, example_text, reinforces_point) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (book_id, section_id, ex.get("example_title"), ex.get("example_text"), ex.get("reinforces_point")),
                )
                n_examples += 1

    conn.commit()
    conn.close()
    print(f"Merged classification for {n_books} book(s): {n_sections} section(s), "
          f"{n_points} point(s), {n_terms} term(s), {n_examples} example(s).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true", help="print the prompt for un-classified books")
    ap.add_argument("--book_id", type=int, default=None)
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--load", default=None, help="path to a JSON file of Claude's classification results")
    args = ap.parse_args()

    if args.export:
        export_book_for_classification(args.book_id, args.limit)
    elif args.load:
        merge_book_classification(args.load)
    else:
        print("Use --export to get a prompt, or --load results.json to merge results.")
