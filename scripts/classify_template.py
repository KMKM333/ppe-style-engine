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


def export_for_classification(channel_name=None, limit=20):
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
    ap.add_argument("--load", default=None, help="path to a JSON file of Claude's classification results")
    args = ap.parse_args()

    if args.export:
        export_for_classification(args.channel, args.limit)
    elif args.load:
        merge_classification(args.load)
    else:
        print("Use --export to get a prompt, or --load results.json to merge results.")
