"""
feature_extraction.py
Computes every "Auto" attribute from the template (Section 2, 6, and parts of
5/9/10) directly from raw script text — no LLM call needed. Run this on every
video at ingest time; it's cheap and deterministic.

The "Class" attributes (hook_type, beat_sequence, citation_style, domain,
etc.) are NOT computed here — see classify_template.py for the prompt that
Claude fills in for those, one batch at a time.
"""
import re
import statistics
from collections import Counter


FILLER_WORDS = [
    "well,", "i mean", "you know", "right,", "so,", "actually,", "basically,",
]

# instructional/demo verbs — a proxy for how "hands-on"/demonstrative a script is
# (e.g. Hannah Fry's "take off your glasses", "hold your fingers up" style, vs.
# purely expository/reflective scripts)
INSTRUCTION_VERBS = [
    "take", "try", "imagine", "picture", "hold", "look", "notice", "pick",
    "grab", "close your", "point", "guess", "use your", "peer", "squint", "cover",
]

# explicit enumerated-framework markers ("step one", "number two", "the
# third thing") — a proxy for how much a script organizes itself as a named,
# numbered list/framework (e.g. robdwillis's "Element one... Element two...")
_FRAMEWORK_ORDINALS = [
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "1", "2", "3", "4", "5",
]
_FRAMEWORK_NOUNS = ["step", "number", "element", "rule"]
FRAMEWORK_MARKER_RE = re.compile(
    r"\b(?:" + "|".join(_FRAMEWORK_NOUNS) + r")\s*(?:number\s+)?(?:" + "|".join(_FRAMEWORK_ORDINALS) + r")\b"
    r"|\b(?:the\s+)?(?:first|second|third|fourth|fifth|sixth)\s+(?:thing|reason|element|step|point)\b",
    re.I,
)

# --- title format taxonomy ---------------------------------------------
# Expanded from the original 4-bucket classifier (Question / Name, Concept /
# Name: Concept / Other) per a title-taxonomy analysis run across the full
# Philosophyminis / fryrsquared / robdwillis corpus (see
# Title_Format_Taxonomy.xlsx). More buckets = more signal for cross-profile
# comparison once there are many style profiles to segment by title strategy.
#
# TITLE_FORMATS is the full valid value set (auto-detected ones first, then
# the two rare hook types below that are real categories but too semantically
# subtle to regex reliably — they exist so they can be assigned manually or
# by an LLM pass later, and so the Inputs filter dropdown can offer them).
TITLE_FORMATS = [
    "Untitled/Missing",
    "Question",
    "Question (implied)",
    "Name/Source + Concept",
    "Topic/Concept: Elaboration",
    "Concept Label (Unattributed)",
    "Framework / Acronym Label",
    "Named Example / Case Study",
    "Numbered List",
    "How-To / Instructional",
    "Command / Imperative",
    "Second-Person Callout",
    "Superlative / Ranking",
    "Curiosity Gap / Teaser",
    "Declarative Claim",
    "Wordplay / Pun",       # not auto-detected — manual/LLM only
    "Brand / Sponsor Tie-in",  # not auto-detected — manual/LLM only
    "Other",
]

_TF_SUPERLATIVE_RE = re.compile(
    r"\b(biggest|best|worst|most|only|greatest|smartest|hardest|easiest|highest|lowest)\b", re.I
)
_TF_SECOND_PERSON_RE = re.compile(r"^(you're|you are|your|you)\b", re.I)
_TF_COMMAND_RE = re.compile(
    r"^(don'?t|never|always|stop|steal|try|avoid|ask|say|watch|learn|use|make|build|read|write|give|create)\b",
    re.I,
)
_TF_HOWTO_RE = re.compile(r"^how\s+to\b", re.I)
_TF_IMPLIED_Q_RE = re.compile(r"^(why|how|what|when|where|does|do|did|is|are|can|could|should|would|will)\b", re.I)
_TF_NUMBERED_RE = re.compile(r"^\d+\s")
_TF_OPENER_THE_RE = re.compile(r"^(the|this)\b", re.I)
_TF_ACRONYM_RE = re.compile(r"\b[A-Z]{2,6}\b")
# common acronyms that show up as ordinary vocabulary rather than a title
# naming a specific framework/model/method (e.g. "AI" in an AI-news channel
# shouldn't make every title read as a "Framework / Acronym Label")
_TF_COMMON_ACRONYMS = {
    "AI", "US", "UK", "EU", "CEO", "CTO", "IPO", "TV", "OK", "ID", "PC",
    "VS", "API", "LLM", "GPU", "CPU", "RAM", "URL", "FAQ", "DIY", "CTA",
    "ETF", "GDP", "IT",
}
_TF_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_TF_FULLNAME_RE = re.compile(r"\b[A-Z][a-z]+\s[A-Z][a-z]+\b")
_TF_COPULA_RE = re.compile(r"\b(is|are|was|were)\b", re.I)


def classify_title_format(title_text):
    """Priority-ordered heuristic classifier over TITLE_FORMATS. Order matters —
    more specific/structural patterns are checked before broad semantic ones,
    same approach used in the taxonomy analysis this was built from."""
    if not title_text or not title_text.strip():
        return None
    t = title_text.strip()
    if t.lower() == "untitled":
        return "Untitled/Missing"
    if t.endswith("?"):
        return "Question"
    if _TF_NUMBERED_RE.match(t):
        return "Numbered List"
    if _TF_HOWTO_RE.match(t):
        return "How-To / Instructional"
    if _TF_SECOND_PERSON_RE.match(t):
        return "Second-Person Callout"
    if _TF_COMMAND_RE.match(t):
        return "Command / Imperative"

    # comma/colon split — whichever separator appears first decides the shape;
    # an all-caps head before either one means it's naming a framework/acronym
    sep_match = re.search(r"[,:]", t)
    if sep_match:
        sep = sep_match.group()
        head = t[: sep_match.start()].strip()
        if re.fullmatch(r"[A-Z]{2,6}", head) and head not in _TF_COMMON_ACRONYMS:
            return "Framework / Acronym Label"
        if sep == ",":
            return "Name/Source + Concept"
        return "Topic/Concept: Elaboration"

    if _TF_SUPERLATIVE_RE.search(t):
        return "Superlative / Ranking"
    if _TF_FULLNAME_RE.search(t) or _TF_YEAR_RE.search(t):
        return "Named Example / Case Study"
    if _TF_OPENER_THE_RE.match(t):
        return "Curiosity Gap / Teaser"
    if _TF_IMPLIED_Q_RE.match(t):
        return "Question (implied)"
    if any(m not in _TF_COMMON_ACRONYMS for m in _TF_ACRONYM_RE.findall(t)):
        return "Framework / Acronym Label"
    if len(t.split()) <= 4:
        return "Concept Label (Unattributed)"
    if _TF_COPULA_RE.search(t):
        return "Declarative Claim"
    return "Other"

# colloquial/informal markers — contractions plus a short list of casual words
# ("gonna", "kinda"...); a proxy for formality (distinct from filler_count,
# which is about verbal tics like "you know" / "I mean")
_COLLOQUIAL_WORDS = [
    "gonna", "wanna", "kinda", "sorta", "yeah", "okay", "ok", "stuff", "guys",
    "totally", "literally", "honestly", "super", "folks",
]
COLLOQUIAL_RE = re.compile(
    r"\b\w+'(?:t|s|re|ve|ll|d|m)\b|\b(" + "|".join(_COLLOQUIAL_WORDS) + r")\b", re.I
)

# does the script explicitly point at another piece of media ("this clip",
# "watch this footage")? A text-level proxy for cross-media referencing —
# NOT actual video/audio analysis, which this text-only engine can't do
EXTERNAL_MEDIA_RE = re.compile(
    r"\b(this clip|this footage|this recording|this speech|watch this|this video)\b", re.I
)

# "X instead of Y" / "rather than" / "as opposed to" / "not X but Y" — explicit
# contrast structures, a distinct rhetorical device from rule-of-three or
# framework markers
CONTRAST_RE = re.compile(
    r"\b(instead of|rather than|as opposed to)\b|\bnot\b[^.?!]{1,40}\bbut\b", re.I
)

# capitalized multi-word sequences ("Steve Jobs", "Hannah Fry") — a lightweight,
# non-ML proxy for "who/what this script is about"; noisy (title-case sentence
# starts can false-positive) but cheap and directionally useful
NAMED_ENTITY_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")

# coarse humor proxy — explicit joke/laughter markers plus exclamation density.
# This is NOT humor detection; it's a cheap textual signal, documented as such.
_HUMOR_MARKERS = ["haha", "lol", "joking", "kidding", "hilarious", "just kidding"]
HUMOR_MARKER_RE = re.compile(r"\b(" + "|".join(_HUMOR_MARKERS) + r")\b", re.I)

PUNCTUATION_RE = re.compile(r"[.,;:!?—–\-\"'()]")

# CTA (call-to-action) heuristics, matching classify_template.py's cta_type
# controlled vocabulary — a text-pattern proxy for the same signal an LLM
# classification pass would tag, so has_cta/cta_type/cta_placement/cta_count
# are available for scoring text (pasted input, freshly generated rewrites)
# that's never been through a Claude classification pass.
_COMMENT_BAIT_RE = re.compile(
    r"\b(comment below|let me know|what do you think|drop a comment|"
    r"comment your|tell me in the comments|let us know)\b", re.I,
)
_FOLLOW_SUBSCRIBE_RE = re.compile(
    r"\b(follow|subscribe|hit (?:that |the )?follow|"
    r"smash (?:that |the )?(?:like|follow)|turn on notifications)\b", re.I,
)
_NEWSLETTER_PRODUCT_RE = re.compile(
    r"\b(newsletter|link in (?:my |the )?bio|check out my|sign up|join my|"
    r"buy my|my book|my course|my podcast|my substack)\b", re.I,
)
_CTA_PATTERNS = [
    ("Comment-bait question", _COMMENT_BAIT_RE),
    ("Follow/subscribe", _FOLLOW_SUBSCRIBE_RE),
    ("Newsletter/product plug", _NEWSLETTER_PRODUCT_RE),
]


def classify_cta(text):
    """Heuristic auto-classifier for has_cta/cta_type/cta_placement/cta_count
    — same keyword-pattern approach as classify_title_format, over the exact
    controlled vocabulary classify_template.py uses. cta_type picks whichever
    category has the most hits; cta_placement is Start/Mid/End by where the
    first hit falls in the text."""
    if not text or not text.strip():
        return {"has_cta": 0, "cta_type": "None", "cta_placement": "None", "cta_count": 0}

    hits = [(m.start(), category) for category, pattern in _CTA_PATTERNS for m in pattern.finditer(text)]
    if not hits:
        return {"has_cta": 0, "cta_type": "None", "cta_placement": "None", "cta_count": 0}

    cta_type = Counter(category for _, category in hits).most_common(1)[0][0]
    first_pos = min(pos for pos, _ in hits)
    frac = first_pos / len(text)
    cta_placement = "Start" if frac < 1 / 3 else ("Mid" if frac < 2 / 3 else "End")
    return {"has_cta": 1, "cta_type": cta_type, "cta_placement": cta_placement, "cta_count": len(hits)}

# a small general jargon list to seed jargon_density; extend per-domain as you go
PPE_JARGON_SEED = [
    "epistemic", "ontology", "dialectic", "utilitarian", "deontological",
    "asabiyyah", "telic", "atelic", "hegemony", "praxis", "phenomenology",
    "heuristic", "cognitive bias", "externality", "elasticity", "marginal",
    "equilibrium", "hegemonic", "paradigm", "normative", "empirical",
    "stoic", "nihilism", "existentialism", "transcendentalism",
]


def _sentences(text: str):
    clean = text.replace("\n", " ")
    parts = re.split(r"(?<=[.!?])\s+", clean.strip())
    return [p.strip() for p in parts if p.strip()]


def _words(text: str):
    return re.findall(r"[A-Za-z']+", text)


def _syllable_count(word: str) -> int:
    """Heuristic syllable counter (vowel-group method) — good enough for
    Flesch-Kincaid at script length; not phonetically perfect."""
    word = word.lower()
    vowels = "aeiouy"
    count = 0
    prev_was_vowel = False
    for ch in word:
        is_vowel = ch in vowels
        if is_vowel and not prev_was_vowel:
            count += 1
        prev_was_vowel = is_vowel
    if word.endswith("e") and count > 1:
        count -= 1
    return max(count, 1)


def flesch_kincaid_grade(text: str) -> float:
    sents = _sentences(text)
    words = _words(text)
    if not sents or not words:
        return 0.0
    syllables = sum(_syllable_count(w) for w in words)
    n_sent, n_word = len(sents), len(words)
    grade = 0.39 * (n_word / n_sent) + 11.8 * (syllables / n_word) - 15.59
    return round(grade, 2)


def paragraphs(text: str):
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def extract_auto_features(script_text: str, title_text: str = "") -> dict:
    text = script_text.strip()
    sents = _sentences(text)
    words = _words(text)
    paras = paragraphs(text)
    n_words = len(words)

    sent_lens = [len(_words(s)) for s in sents] or [0]

    you_count = len(re.findall(r"\byou(r|rs|self)?\b", text, re.I))
    i_count = len(re.findall(r"\bI\b", text))
    we_count = len(re.findall(r"\bwe('re|'ll|'d|'ve)?\b", text, re.I))
    question_count = text.count("?")
    emdash_count = text.count("—") + text.count("--")
    # quotes: pairs of straight or curly single/double quotes
    quote_count = len(re.findall(r"[\u2018\u201c\"'][^\u2019\u201d\"']{3,}[\u2019\u201d\"']", text))

    lower = text.lower()
    jargon_hits = sum(lower.count(term) for term in PPE_JARGON_SEED)
    jargon_density = round(jargon_hits / n_words, 4) if n_words else 0.0

    filler_count = sum(lower.count(f) for f in FILLER_WORDS)

    number_count = len(re.findall(r"\b\d+(?:[.,]\d+)*%?\b", text))
    instruction_verb_count = len(re.findall(
        r"\b(" + "|".join(re.escape(v) for v in INSTRUCTION_VERBS) + r")\b", text, re.I
    ))
    framework_marker_count = len(FRAMEWORK_MARKER_RE.findall(text))
    contrast_structure_count = len(CONTRAST_RE.findall(text))
    references_external_media = 1 if EXTERNAL_MEDIA_RE.search(text) else 0
    named_entity_count = len(set(NAMED_ENTITY_RE.findall(text)))
    humor_marker_count = len(HUMOR_MARKER_RE.findall(text)) + text.count("!")

    # word economy: filler words per "high-value" word (instruction verbs,
    # framework markers, numeric figures — the closest existing proxies for
    # punchy/substantive content) — a ratio, not a bare filler count, so it
    # reads as "how diluted is the substance" rather than just "how many
    # filler words". Falls back to the raw filler count when there's no
    # high-value word to divide by, rather than an undefined/inf ratio.
    high_value_word_count = instruction_verb_count + framework_marker_count + number_count
    word_economy_ratio = round(filler_count / high_value_word_count, 3) if high_value_word_count else float(filler_count)

    colloquial_hits = len(COLLOQUIAL_RE.findall(text))
    colloquialism_density = round(colloquial_hits / n_words * 100, 2) if n_words else 0.0

    punct_count = len(PUNCTUATION_RE.findall(text))
    punctuation_density = round(punct_count / n_words * 100, 2) if n_words else 0.0

    unique_words = {w.lower() for w in words}
    lexical_diversity = round(len(unique_words) / n_words, 4) if n_words else 0.0

    sent_len_mean = statistics.mean(sent_lens) if sent_lens else 0
    sent_len_std = statistics.pstdev(sent_lens) if len(sent_lens) > 1 else 0.0
    sentence_rhythm_cv = round(sent_len_std / sent_len_mean, 4) if sent_len_mean else 0.0

    first_para = paras[0] if paras else ""
    last_para = paras[-1] if paras else ""

    para_word_counts = [len(_words(p)) for p in paras] or [0]
    mean_para_len = statistics.mean(para_word_counts)
    closing_paragraph_ratio = round(para_word_counts[-1] / mean_para_len, 3) if mean_para_len else None

    # title features
    title_word_count = len(_words(title_text)) if title_text else None
    title_format = classify_title_format(title_text)

    features = {
        # length & pacing
        "word_count": n_words,
        "beat_count": len(paras),
        "avg_sentence_len": round(statistics.mean(sent_lens), 2),
        "median_sentence_len": statistics.median(sent_lens),
        "sentence_len_variance": round(statistics.pvariance(sent_lens), 2) if len(sent_lens) > 1 else 0.0,
        "sentence_rhythm_cv": sentence_rhythm_cv,

        # diction (auto subset)
        "you_freq_per_100w": round(you_count / n_words * 100, 2) if n_words else 0,
        "i_freq_per_100w": round(i_count / n_words * 100, 2) if n_words else 0,
        "we_freq_per_100w": round(we_count / n_words * 100, 2) if n_words else 0,
        "question_count": question_count,
        "emdash_count": emdash_count,
        "quote_count": quote_count,
        "jargon_density": jargon_density,
        "colloquialism_density": colloquialism_density,
        "readability_score": flesch_kincaid_grade(text),
        "filler_retention": 1 if filler_count > 0 else 0,
        "filler_count": filler_count,
        "word_economy_ratio": word_economy_ratio,
        "number_count": number_count,
        "instruction_verb_count": instruction_verb_count,
        "framework_marker_count": framework_marker_count,
        "lexical_diversity": lexical_diversity,
        "punctuation_density": punctuation_density,
        "contrast_structure_count": contrast_structure_count,
        "named_entity_count": named_entity_count,
        "humor_marker_count": humor_marker_count,
        "references_external_media": references_external_media,
        "closing_paragraph_ratio": closing_paragraph_ratio,

        # close (auto subset)
        "ends_on_question": 1 if last_para.endswith("?") else 0,

        # title
        "title_word_count": title_word_count,
        "title_format": title_format,
        "title_names_source": 1 if (title_text and "," in title_text) else 0,

        "classified_by": "auto",
    }
    features.update(classify_cta(text))
    return features


if __name__ == "__main__":
    sample = (
        "Why is Hannah Fry so bloody good? She has this amazing ability to make "
        "maths and science feel impossible to ignore.\n\n"
        "Well, I did what any normal person would do. I transcribed 30 of her "
        "highest performing videos and started looking for patterns."
    )
    import json
    print(json.dumps(extract_auto_features(sample, "Fry, The Curiosity Formula"), indent=2))
