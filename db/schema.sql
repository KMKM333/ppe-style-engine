-- PPE Script Style Engine — Database Schema
-- One DB, four layers: raw inputs -> per-video attributes -> per-profile fingerprints -> test/rating runs

PRAGMA foreign_keys = ON;

-- ============================================================
-- 1. CHANNELS & STYLE PROFILES
-- ============================================================

CREATE TABLE IF NOT EXISTS channels (
    channel_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_name    TEXT NOT NULL UNIQUE,
    platform        TEXT,                     -- 'Instagram' / 'YouTube' / etc.
    typical_length_band TEXT,                 -- 'A' (~1 min) / 'B' (~20 min) / etc. — matches your A/B/C scheme
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS style_profiles (
    profile_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_code    TEXT NOT NULL UNIQUE,     -- 'A.1', 'A.2', 'B.1', etc.
    channel_id      INTEGER REFERENCES channels(channel_id),
    media_type      TEXT DEFAULT 'Instagram', -- source medium this profile's channel is built from,
                                                -- e.g. 'Instagram', 'YouTube', 'Book', 'News Article'
    overview        TEXT,                     -- 1-2 sentence human summary of what this channel's content covers
    subject         TEXT,                     -- one of: Economics, Politics, Philosophy, Psychology, Sustainability, Science, Technology
    length_band     TEXT,                     -- 'A' / 'B' / 'C'
    n_videos_analysed INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'draft',     -- 'draft' / 'confirmed' — becomes 'confirmed' once n_videos passes a threshold
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- 2. RAW INPUTS (Title | Script, as transcribed manually)
-- ============================================================

CREATE TABLE IF NOT EXISTS videos (
    video_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id      INTEGER REFERENCES channels(channel_id),
    title           TEXT NOT NULL,
    script          TEXT NOT NULL,
    media_type      TEXT,               -- source medium of this specific input, e.g. 'Instagram', 'YouTube', 'Upload'
    url             TEXT,
    duration_sec    REAL,
    posted_at       TEXT,
    content_hash    TEXT,              -- normalized-text hash, for exact/near-duplicate membership checks
    summary         TEXT,              -- plain-language soundbite explanation of the video (human/Claude-written,
                                        -- not extracted from the script's own wording)
    timed_transcript_json TEXT,        -- JSON [{"start": float, "text": str}, ...] cue-level timing, captured for
                                        -- long-form YouTube videos only — lets classification/visual-detection
                                        -- point at an approximate on-screen moment without re-transcribing
    ingested_at     TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_videos_content_hash ON videos(content_hash);

-- ============================================================
-- 3. PER-VIDEO ATTRIBUTES (the template, flattened)
-- Auto fields are computed by feature_extraction.py
-- Class fields are filled by the classification pass (Claude)
-- ============================================================

CREATE TABLE IF NOT EXISTS video_attributes (
    video_id                INTEGER PRIMARY KEY REFERENCES videos(video_id),

    -- Section 1: Title
    title_format            TEXT,   -- enum
    title_names_source      INTEGER,-- boolean 0/1
    title_word_count        INTEGER,

    -- Section 2: Length & pacing
    word_count               INTEGER,
    beat_count                INTEGER,
    avg_sentence_len          REAL,
    median_sentence_len       REAL,
    sentence_len_variance     REAL,
    sentence_rhythm_cv        REAL,   -- auto: coefficient of variation (std/mean) of sentence length —
                                       -- normalized rhythm, comparable across profiles unlike raw variance
    time_to_payoff_pct        REAL,
    reveal_placement          TEXT,   -- enum: front/mid/end

    -- Section 3: Structure
    beat_sequence             TEXT,   -- e.g. 'Hook-Definition-Example-Close'
    formula_explicit          INTEGER,
    framework_marker_count    INTEGER,  -- auto: count of explicit "step one/number two/element three"-style markers
    closing_paragraph_ratio   REAL,     -- auto: last paragraph word count / mean paragraph word count
                                        -- (< 1 = "ends smaller than it started")
    references_external_media INTEGER, -- auto: 0/1, script explicitly calls out another clip/video/footage

    -- Section 4: Hook
    hook_type                 TEXT,
    hook_word_count           INTEGER,
    hook_names_source         INTEGER,
    hook_source_word_position INTEGER,
    hook_ends_on_pivot        INTEGER,
    hook_self_demonstrating   INTEGER,

    -- Section 5: Close
    close_type                TEXT,
    ends_on_question           INTEGER,
    callback_to_hook           INTEGER,

    -- Section 6: Diction
    you_freq_per_100w          REAL,
    i_freq_per_100w             REAL,
    question_count               INTEGER,
    emdash_count                  INTEGER,
    quote_count                    INTEGER,
    jargon_density                  REAL,
    colloquialism_density            REAL,
    readability_score                  REAL,
    register_shift_at_cta               INTEGER,
    filler_retention                     INTEGER,
    filler_count                          INTEGER,
    number_count                           INTEGER,  -- auto: count of numeric figures/stats mentioned
    instruction_verb_count                  INTEGER,  -- auto: count of instructional/demo verbs ("take", "try", "look"...)
    lexical_diversity                        REAL,     -- auto: type-token ratio (unique words / total words)
    punctuation_density                       REAL,     -- auto: punctuation marks per 100 words

    -- Section 7: Rhetoric
    citation_style               TEXT,
    analogy_count                 INTEGER,
    names_bias_or_law             INTEGER,
    named_bias_or_law_term        TEXT,
    dialectic_structure           INTEGER,
    certainty_register            TEXT,
    rule_of_three_present         INTEGER,
    rule_of_three_count           INTEGER,
    explanation_mechanism         TEXT,  -- class: how the concept is taught, e.g. "Physical demonstration/experiment"
    rhetorical_mode                TEXT,  -- class: Story-led / Advice-led / Opinion-led / Question-led/Exploratory
    contrast_structure_count        INTEGER, -- auto: count of "X instead of Y" / "not X but Y" / "rather than" patterns

    -- Section 8: Content taxonomy
    domain                     TEXT,
    concept_type                TEXT,
    source_era                   TEXT,
    framing                       TEXT,
    named_entity_count            INTEGER,  -- auto: count of distinct capitalized multi-word proper-noun mentions
                                             -- (lightweight proxy for "what/who this script is about")

    -- Section 9: Delivery
    script_polish               TEXT,
    emphasis_markers_present    INTEGER,
    humor_marker_count           INTEGER,  -- auto: coarse proxy count of humor/wit markers (not true humor detection)

    -- Section 10: Engagement / CTA
    has_cta                    INTEGER,
    cta_type                    TEXT,
    cta_placement                 TEXT,
    cta_count                      INTEGER,

    -- Section 11: Long-form Structure — only meaningful for long-form
    -- (YouTube, 20-40+ min) content; stays NULL forever on short-form
    -- Instagram rows, same as every other media-specific field on this table.
    chapter_count               INTEGER,  -- auto: from yt-dlp chapter metadata at ingest time
    has_chapters                 INTEGER, -- auto: boolean 0/1
    cold_open_present             INTEGER,-- class: boolean 0/1
    intro_length_sec               REAL,  -- class: seconds before the main content/thesis starts
    sponsor_segment_present          INTEGER, -- auto: boolean 0/1
    sponsor_segment_position          TEXT,    -- auto: enum early/mid/late/none
    act_count                          INTEGER, -- class: distinct large structural movements
    re_engagement_hook_count            INTEGER, -- class: mid-video re-hooks fighting runtime drop-off
    outro_cta_count                      INTEGER, -- auto: CTA phrases in the closing portion of the script
    outro_type                            TEXT,   -- class: enum
    pacing_arc                             TEXT,  -- class: enum, steady/accelerating/slows-then-quickens
    topic_shift_count                       INTEGER, -- class: distinct sub-topics covered

    -- Meta
    classified_by              TEXT,   -- 'auto' / 'claude' / 'manual' / 'needs_review'
    classified_at               TEXT DEFAULT (datetime('now')),
    classification_error        TEXT,  -- set when auto_process_video.py's classification pass fails, so a video
                                        -- never looks silently mis-classified (mirrors book_attributes' field)

    FOREIGN KEY (video_id) REFERENCES videos(video_id)
);

-- Chapter/section layer for long-form video — mirrors book_sections. Only
-- populated for long-form (YouTube) content; short-form Instagram videos
-- have no rows here and their breakdown stays flat (section_id NULL below).
CREATE TABLE IF NOT EXISTS video_sections (
    section_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id          INTEGER REFERENCES videos(video_id),
    section_number     INTEGER,
    section_title       TEXT NOT NULL,
    summary               TEXT,
    topics                 TEXT,
    created_at               TEXT DEFAULT (datetime('now'))
);

-- Content breakdown for a video — the qualitative counterpart to
-- video_attributes' rubric scores, mirroring book_points/book_terms/
-- book_examples. section_id is NULL for short-form Instagram videos (no
-- chapter layer needed for a 60-second clip); long-form videos scope each
-- row to a video_sections row, same pattern book_examples uses.
CREATE TABLE IF NOT EXISTS video_points (
    point_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id      INTEGER REFERENCES videos(video_id),
    section_id    INTEGER REFERENCES video_sections(section_id),
    point_text     TEXT NOT NULL,
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS video_terms (
    term_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id      INTEGER REFERENCES videos(video_id),
    section_id    INTEGER REFERENCES video_sections(section_id),
    term            TEXT NOT NULL,
    definition        TEXT,      -- how the video defines/uses this term
    created_at           TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS video_examples (
    example_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id          INTEGER REFERENCES videos(video_id),
    section_id        INTEGER REFERENCES video_sections(section_id),
    example_title       TEXT,    -- short, Instagram-title-style label (3-7 words) for list views
    example_text          TEXT NOT NULL,
    reinforces_point         TEXT,   -- which point/claim of the video this example supports
    created_at                  TEXT DEFAULT (datetime('now'))
);

-- Significant on-screen graphs/charts/tables a long-form video references
-- (e.g. "as you can see in this chart..."). recreated_svg is always filled
-- in at classification time (an LLM recreation from the surrounding
-- transcript, same spirit as book_sections.diagram_svg); screenshot_captured
-- flips to 1 once the local transcriber has grabbed and uploaded the real
-- video frame at timestamp_sec, which then takes display priority over the
-- recreation. Never populated for short-form Instagram videos.
CREATE TABLE IF NOT EXISTS video_visuals (
    visual_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id             INTEGER REFERENCES videos(video_id),
    section_id           INTEGER REFERENCES video_sections(section_id),
    timestamp_sec        REAL,
    caption              TEXT,
    recreated_svg        TEXT,
    screenshot_captured  INTEGER DEFAULT 0,
    created_at           TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- 4. STYLE PROFILE FINGERPRINTS (aggregated stats per profile)
-- One row per (profile, numeric attribute) -> mean/std/min/max
-- One row per (profile, categorical attribute, value) -> share %
-- ============================================================

CREATE TABLE IF NOT EXISTS profile_fingerprint_numeric (
    profile_id      INTEGER REFERENCES style_profiles(profile_id),
    attribute       TEXT NOT NULL,   -- e.g. 'word_count'
    mean_val        REAL,
    std_val         REAL,
    min_val         REAL,
    max_val         REAL,
    median_val      REAL,
    values_json     TEXT,            -- sorted JSON array of every observed value, for percentile-based scoring
    PRIMARY KEY (profile_id, attribute)
);

CREATE TABLE IF NOT EXISTS profile_fingerprint_categorical (
    profile_id      INTEGER REFERENCES style_profiles(profile_id),
    attribute       TEXT NOT NULL,   -- e.g. 'hook_type'
    value           TEXT NOT NULL,   -- e.g. 'Rhetorical question'
    share_pct       REAL,            -- e.g. 16.4
    PRIMARY KEY (profile_id, attribute, value)
);

-- ============================================================
-- 4a. HYBRID PROFILES (a style_profiles row synthesized by blending N
-- other existing profiles' fingerprints, rather than observed from a
-- creator's own videos/books). The blended profile is a normal row in
-- style_profiles/profile_fingerprint_numeric/profile_fingerprint_categorical
-- (media_type='Hybrid') so it needs no special-casing to be used as a
-- rewrite/transform target or scored like any other profile — this table
-- only exists to remember WHICH profiles + weights it was composed from,
-- for display on the profile detail page.
-- ============================================================

CREATE TABLE IF NOT EXISTS style_profile_hybrid_sources (
    profile_id          INTEGER REFERENCES style_profiles(profile_id),   -- the hybrid profile
    source_profile_id   INTEGER REFERENCES style_profiles(profile_id),   -- one ingredient profile
    weight_pct           REAL NOT NULL,   -- normalised share of the blend, sums to ~100 across a profile_id's rows
    PRIMARY KEY (profile_id, source_profile_id)
);

-- ============================================================
-- 4b. STYLE CARD (human-editable, per profile) + NEGATIVE SPACE
-- The AI-observed fingerprint above is computed FROM the corpus. This table
-- is the opposite direction: a human's DECLARED description/target for the
-- profile's voice, editable independently of what the corpus shows. Where
-- `numeric_attr` names a real video_attributes column, the UI can compute a
-- declared-vs-observed delta against that attribute's fingerprint mean.
-- constraint_type is used for negative-space rows (things this profile
-- should never say/do) instead of a normal declared-value row.
-- ============================================================

CREATE TABLE IF NOT EXISTS profile_style_card (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id      INTEGER REFERENCES style_profiles(profile_id),
    field           TEXT NOT NULL,   -- e.g. 'sentence_rhythm', 'humor', 'hashtag_behavior'
    declared_value  TEXT NOT NULL,   -- free text, or a number-as-text if numeric_attr is set
    numeric_attr    TEXT,            -- optional: matching video_attributes column name, for delta computation
    constraint_type TEXT,            -- NULL for a normal style-card row; 'banned_word'/'banned_tone'/'banned_format'
                                      -- for a negative-space constraint
    updated_at      TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- 4c. FINGERPRINT SNAPSHOTS (voice drift over time)
-- A point-in-time copy of a profile's fingerprint, so you can compare "now"
-- against an earlier snapshot to measure how much (and which direction) a
-- profile's voice has moved. Independent of the live fingerprint tables —
-- taking a snapshot never mutates profile_fingerprint_numeric/categorical.
-- ============================================================

CREATE TABLE IF NOT EXISTS profile_fingerprint_snapshots (
    snapshot_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id        INTEGER REFERENCES style_profiles(profile_id),
    label             TEXT,             -- e.g. 'baseline', 'Q1 2026'
    n_videos_analysed INTEGER,
    fingerprint_json  TEXT NOT NULL,    -- {"numeric": {attr: mean_val, ...}, "categorical": {attr: {value: share_pct, ...}}}
    snapshotted_at    TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- 5. RULE-BASED RATING ENGINE CONFIG (weights per attribute)
-- Lets you tune which attributes matter most for matching, without touching code
-- ============================================================

CREATE TABLE IF NOT EXISTS scoring_weights (
    attribute       TEXT PRIMARY KEY,
    weight          REAL NOT NULL DEFAULT 1.0,
    attribute_kind  TEXT NOT NULL   -- 'numeric' or 'categorical'
);

-- ============================================================
-- 6. TEST INPUTS & RATING RESULTS (arbitrary text scored against profiles)
-- ============================================================

CREATE TABLE IF NOT EXISTS test_inputs (
    test_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source_label    TEXT,            -- free text: 'Ali_Razavi12 CPI script', 'book paragraph - Sapiens ch3', etc.
    input_type      TEXT,            -- 'video_script' / 'book_paragraph' / 'news_article' / 'other'
    raw_text        TEXT NOT NULL,
    submitted_at    TEXT DEFAULT (datetime('now'))
);

-- PRE-CREATION scores: how well does the raw input already correlate with
-- each profile, before any rewriting happens?
CREATE TABLE IF NOT EXISTS test_scores (
    test_id         INTEGER REFERENCES test_inputs(test_id),
    profile_id      INTEGER REFERENCES style_profiles(profile_id),
    total_score     REAL,            -- 0-100, higher = better fit
    rank            INTEGER,         -- 1 = best match among profiles compared
    is_corpus_member INTEGER DEFAULT 0,  -- 1 if this text is an exact/near-duplicate of a video already in that profile
    match_video_id   INTEGER REFERENCES videos(video_id),  -- which training video it matched, if any
    match_similarity  REAL,          -- 0-1 text similarity to the matched video
    score_breakdown TEXT,            -- JSON blob: {attribute: sub_score, ...}
    scored_at       TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (test_id, profile_id)
);

-- ============================================================
-- 7. TRANSFORMATIONS: Input X rewritten into a target profile's style,
-- plus POST-CREATION scoring of how well that rewrite actually landed
-- ============================================================

CREATE TABLE IF NOT EXISTS transformations (
    transformation_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id             INTEGER REFERENCES test_inputs(test_id),   -- source Input X
    target_profile_id   INTEGER REFERENCES style_profiles(profile_id),
    generated_title      TEXT,
    generated_text        TEXT NOT NULL,
    generated_by            TEXT,     -- 'claude' / 'manual' / other
    generated_at             TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transform_scores (
    transformation_id  INTEGER REFERENCES transformations(transformation_id),
    profile_id          INTEGER REFERENCES style_profiles(profile_id),  -- normally == target_profile_id, but every
                                                                          -- confirmed profile is scored for comparison
    total_score          REAL,
    rank                   INTEGER,
    is_target_profile        INTEGER DEFAULT 0,  -- 1 for the row matching transformations.target_profile_id
    pre_score_same_profile   REAL,   -- the ORIGINAL Input X's pre-creation score against this same profile, for delta
    score_delta                REAL, -- total_score - pre_score_same_profile  ("did the rewrite move it closer?")
    score_breakdown             TEXT,
    scored_at                    TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (transformation_id, profile_id)
);

-- ============================================================
-- 8. BOOKS: non-fiction book ingestion, analysed against a separate
-- 42-attribute qualitative rubric (see classify_book_template.py). Books
-- are cross-referenced against the same 7-subject taxonomy used for style
-- profiles, so a book and a creator can be compared on the same subject.
-- ============================================================

CREATE TABLE IF NOT EXISTS books (
    book_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title             TEXT NOT NULL,
    author            TEXT,
    subject           TEXT,     -- Economics / Politics / Philosophy / Psychology / Sustainability / Science / Technology
    publication_year  INTEGER,
    word_count        INTEGER,
    page_count        INTEGER,  -- page count of the source PDF/file, if available
    full_text         TEXT,     -- full book text, if stored locally
    source_note       TEXT,     -- provenance, e.g. "user-provided .txt", "public domain via Project Gutenberg"
    source_file_path  TEXT,     -- absolute local path to the original PDF/file, if available
    summary           TEXT,     -- plain-language soundbite explanation, same convention as videos.summary
    is_read           INTEGER DEFAULT 0,  -- 0/1 — whether this book has been read (independent of classification status)
    ingested_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS book_attributes (
    book_id                     INTEGER PRIMARY KEY REFERENCES books(book_id),

    -- Thesis & Purpose
    thesis_statement            TEXT,     -- 1-2 sentence plain-language central argument/claim
    primary_goal                TEXT,     -- Inform/Explain, Persuade/Argue, Critique/Deconstruct, Instruct/Prescribe

    -- Evidence & Authority
    primary_evidence_type       TEXT,     -- dominant evidence type (Statistics/data, Expert interviews, Historical
                                           -- documents/archival, Personal anecdote/memoir, Case studies,
                                           -- Scientific studies/citations, Philosophical argument, Mixed/multiple)
    secondary_evidence_types    TEXT,     -- comma-separated: other evidence types also present

    -- Tone & Voice
    tone                         TEXT,    -- Objective/neutral, Passionate/advocacy, Skeptical/critical,
                                           -- Urgent/alarmist, Wry/ironic, Reverent/admiring

    -- Structure & Organization
    structure_style              TEXT,    -- Linear/chronological, Thematic, Building framework, Case-study
                                           -- anthology, Argument + rebuttal, Problem->mechanism->solution
    uses_visual_aids             INTEGER, -- 0/1: charts, diagrams, or data visuals used to clarify concepts
    subheading_density           TEXT,    -- None/sparse, Moderate, Heavy/highly scannable

    -- Target Audience
    target_audience               TEXT,   -- General public, Educated lay reader, Practitioners/professionals,
                                           -- Academic/specialist, Policy makers, Students
    vocabulary_complexity         TEXT,   -- Plain language, Some field terms defined inline,
                                           -- Assumes prior domain knowledge, Heavy jargon

    -- Bias & Assumptions
    bias_assumptions               TEXT,  -- free-text: hidden viewpoints, missing counterarguments, unstated beliefs

    -- New attributes (13, beyond the 7 seed dimensions above)
    counter_argument_engagement     TEXT, -- Ignored, Strawmanned, Acknowledged briefly, Substantively engaged, Steelmanned
    argument_architecture           TEXT, -- Deductive, Inductive, Dialectical, Narrative-driven, Comparative/case-based,
                                           -- Historical-chronological
    prescriptiveness                TEXT, -- Purely descriptive, Diagnostic with implied direction,
                                           -- Explicit prescriptions/policy recommendations, How-to/practical playbook
    temporal_orientation            TEXT, -- Backward-looking/historical, Present-diagnostic, Forward-looking/predictive,
                                           -- Cyclical
    interdisciplinary_fields        TEXT, -- comma-separated: secondary subjects/disciplines drawn on beyond `subject`
    named_frameworks_coined         TEXT, -- comma-separated: original models/terms/frameworks the book itself introduces
    narrative_density                TEXT, -- Story-led, Balanced, Argument-led/abstract
    claim_falsifiability             TEXT, -- Highly empirical/testable, Mixed, Primarily normative/philosophical
    ideological_positioning          TEXT, -- free-text: where the book sits on a subject-relevant spectrum
    rhetorical_appeal_balance        TEXT, -- Primarily logical/data-driven, Primarily emotional/narrative,
                                            -- Primarily credibility/authority-driven, Balanced blend
    thesis_consistency               TEXT, -- Tight and consistent, Expands but coherent, Drifts/loses focus
    comparative_positioning          TEXT, -- free-text: other named books/thinkers/schools it positions against or builds on
    citation_density                 TEXT, -- Heavily footnoted/academic, Moderately sourced, Light/conversational, Essayistic/unsourced

    -- Style & Craft (18 new attributes, literary/rhetorical dimensions beyond
    -- the structural rubric above — see BOOK_CLASSIFICATION_PROMPT for the
    -- full rating scale of each)
    diction                          TEXT, -- Simple/plain, Moderate/accessible, Complex/elevated, Formal, Old-fashioned/archaic
    syntax_pattern                   TEXT, -- Short & simple low variety, Long & complex low variety, Highly varied, Fragmented/experimental
    pacing                           TEXT, -- Fast/action-driven, Slow/descriptive, Balanced action & description, Variable/uneven
    emotional_register               TEXT, -- Humorous/light, Dark/somber, Serious/grave, Whimsical/playful, Melancholic, Uplifting/inspiring, Mixed/variable
    narrative_voice                  TEXT, -- Distinct/idiosyncratic, Neutral/generic, Formal/detached narrator, Conversational/intimate narrator, Multiple distinct voices
    sensory_language_density         TEXT, -- Minimal/abstract, Occasional sensory detail, Rich/immersive, Saturated with sensory imagery
    narrative_distance               TEXT, -- Stream-of-consciousness/intimate, Close/internal, Moderate distance, Detached/observational, Omniscient/distant
    figurative_language_density      TEXT, -- Sparse/literal, Occasional, Frequent, Dense/highly figurative
    prose_rhythm                     TEXT, -- Staccato/choppy, Flowing/poetic, Balanced/mixed cadence, Monotonous/uniform
    argumentative_density            TEXT, -- Low, Moderate, High, Very high/dense argumentation (logical transition word frequency)
    abstraction_concreteness_balance TEXT, -- Highly abstract, Balanced, Highly concrete, Alternates deliberately
    noun_verb_ratio_style             TEXT, -- Verb-driven/dynamic, Balanced, Noun-heavy/nominalized, Heavily nominalized/dense academic
    jargon_accessibility             TEXT, -- Plain/no jargon, Light jargon mostly defined, Moderate jargon some undefined, Heavy jargon assumes expertise
    cognitive_metaphor_domain        TEXT, -- Organic/biological, Mechanical/engineering, Journey/spatial, Combat/competition, Ecosystem/network, Mixed/no dominant domain
    hedging_vs_assertion             TEXT, -- Highly hedged/speculative, Balanced, Assertive with occasional hedges, Highly assertive/absolute
    polemical_tone                   TEXT, -- Polite/measured critique, Firm but respectful, Sharply critical, Aggressive/dismissive
    narrative_presence               TEXT, -- Detached/impersonal, Occasional first-person, Consistently participatory, Highly personal/confessional
    rhetorical_questioning           TEXT, -- Rare/absent, Used to transition, Used to challenge assumptions, Frequent — both

    -- Readability (auto-computed from full_text via the same Flesch-Kincaid
    -- heuristic feature_extraction.py uses for videos — not a classification
    -- field, so it's untouched by merge_book_classification())
    avg_sentence_len            REAL,  -- average words per sentence, across the whole book
    avg_syllables_per_word      REAL,  -- average syllables per word, across the whole book
    readability_score           REAL,  -- Flesch-Kincaid grade level, combining the two fields above

    classified_by                     TEXT DEFAULT 'pending',  -- 'pending' / 'claude' / 'manual'
    classified_at                     TEXT
);

-- Chapter/section breakdown — one row per chapter or major section of the
-- book, so points/topics/examples/terms can all be scoped to (and easily
-- pulled up by) the specific part of the book they came from.
CREATE TABLE IF NOT EXISTS book_sections (
    section_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id           INTEGER REFERENCES books(book_id),
    section_number     INTEGER,        -- order within the book
    section_title       TEXT NOT NULL, -- chapter/section title or heading
    summary               TEXT,        -- plain-language soundbite of what this section covers
    topics                 TEXT,       -- comma-separated topic tags for this section
    created_at               TEXT DEFAULT (datetime('now')),
    diagram_svg               TEXT     -- optional inline SVG markup illustrating the section's key model/diagram
);

-- Key points/arguments/claims made within a section — many rows per section.
CREATE TABLE IF NOT EXISTS book_points (
    point_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id            INTEGER REFERENCES books(book_id),
    section_id          INTEGER REFERENCES book_sections(section_id),
    point_text            TEXT NOT NULL,
    created_at              TEXT DEFAULT (datetime('now'))
);

-- Terms/concepts defined or introduced within a section — a per-book glossary.
CREATE TABLE IF NOT EXISTS book_terms (
    term_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id             INTEGER REFERENCES books(book_id),
    section_id           INTEGER REFERENCES book_sections(section_id),
    term                   TEXT NOT NULL,
    definition               TEXT,     -- how the book defines/uses this term
    created_at                 TEXT DEFAULT (datetime('now'))
);

-- Examples/case studies the book uses to reinforce its points — many rows
-- per book, kept separate from book_attributes (a single-row-per-book table)
-- so they stay easy to browse and reference independently.
CREATE TABLE IF NOT EXISTS book_examples (
    example_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id               INTEGER REFERENCES books(book_id),
    section_id             INTEGER REFERENCES book_sections(section_id),
    example_text            TEXT NOT NULL,   -- the example/case study, written up in plain language
    example_title            TEXT,           -- short, Instagram-title-style label (3-7 words) for list views
    reinforces_point          TEXT,          -- which argument/point of the book this example supports
    chapter_or_location        TEXT,         -- fallback free-text location, if not tied to a registered section
    page_range                  TEXT,        -- e.g. "47-48" — legacy free-text page(s), unused by current pipeline
    screenshot_page_num          INTEGER,    -- matched PDF page (single int) for the inline example screenshot,
                                              -- see match_book_screenshots.py
    created_at                   TEXT DEFAULT (datetime('now'))
);
