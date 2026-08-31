"""
migrate_backfill_militia_divide_creation.py — backfills the pre-existing
"The Militia Divide" production_spec_creations row with its real content,
transcribed directly from the hand-authored artifact that row's title and
view_url already referenced (https://claude.ai/code/artifact/cf9c1aa4-...).

That row predates the generation pipeline built this session — it only
ever had title/source/profile links + a manual view_url, no beats_json.
This is not a fresh LLM generation: it's the original artifact's actual
6-beat content, transcribed as-is into the new structured shape, so
clicking the title shows the real thing instead of "Not generated yet".

target_runtime_sec / target_shot_count_min/max are recomputed here from
the beats below (same rule the live generation pipeline uses — computed,
never a separately hand-typed number), which is why they read a bit
lower than the artifact's own rounder top-line estimate (~175s / 62-68
shots): that page's stat strip was a pre-generation guess, this is the
honest sum of the actual beats.

Safe to re-run: only touches the row if beats_json is still NULL, so it
never overwrites a real regeneration.

Usage:
    python3 migrate_backfill_militia_divide_creation.py
"""
import json

from db_init import get_conn

BEATS = [
    {
        "step": 1, "title": "The Puzzle",
        "duration_sec_min": 10, "duration_sec_max": 12,
        "shot_count_min": 4, "shot_count_max": 5,
        "content_points": [
            "Switzerland is peaceful and neutral, yet armed almost like the US per capita.",
            "The government issues citizens guns and requires villages to have ranges.",
        ],
        "illustration_captions": [
            "Title-hook card over an alpine village panel: a shooting range sits beside a quiet street.",
            "Cut straight to Step 1 divider — no lingering.",
        ],
        "punch_tags": ["peaceful", "neutral", "armed"],
    },
    {
        "step": 2, "title": "Guns as Duty",
        "duration_sec_min": 25, "duration_sec_max": 30,
        "shot_count_min": 10, "shot_count_max": 11,
        "content_points": [
            "Every able-bodied Swiss man must serve, train, and keep his service rifle at home.",
            "Shooting clubs work as social hubs; annual proficiency is a civic norm, not a hobby.",
            "Switzerland has no Second Amendment — guns are duty first, privilege second.",
        ],
        "illustration_captions": [
            "Soldier carrying his rifle home on a train; a mountain-bunker range mid-drill.",
            "Narrator-reaction beat: solid-ground cutaway on the word duty.",
        ],
        "punch_tags": ["every man", "at home", "duty"],
    },
    {
        "step": 3, "title": "The Legend",
        "duration_sec_min": 18, "duration_sec_max": 20,
        "shot_count_min": 7, "shot_count_max": 8,
        "content_points": [
            "Founding myth: William Tell defies a tyrant's order.",
            "700 years ago, Swiss tribes confederate and choose a citizen militia over a standing army.",
        ],
        "illustration_captions": [
            "Apple-on-the-head Tell panel; a map of scattered tribes banding into one confederacy.",
        ],
        "punch_tags": ["the legend", "no king", "armed people"],
    },
    {
        "step": 4, "title": "Crossing the Atlantic",
        "duration_sec_min": 28, "duration_sec_max": 32,
        "shot_count_min": 11, "shot_count_max": 12,
        "content_points": [
            "European thinkers praise Switzerland's \"well-regulated militia\" as key to liberty.",
            "John Adams studies the Swiss system before helping draft the Constitution.",
            "The Second Amendment ties bearing arms explicitly to militia service.",
            "Washington's militia puts down the Whiskey Rebellion — the system works, once.",
        ],
        "illustration_captions": [
            "Ship crossing the Atlantic carrying a rolled document; Adams at a writing desk; parchment close-up on the Second Amendment text.",
            "Map-graphic beat: a dotted line, Switzerland → America.",
        ],
        "punch_tags": ["well-regulated", "Adams studies", "it crosses"],
    },
    {
        "step": 5, "title": "America's Divergence",
        "duration_sec_min": 35, "duration_sec_max": 40,
        "shot_count_min": 14, "shot_count_max": 15,
        "content_points": [
            "Expansion and war make a standing army permanent; the militia duty quietly fades.",
            "Guns shift from collective defense to personal tool, then personal identity.",
            "1970s NRA hardliners push the individual-rights reading of the 2nd Amendment.",
            "Heller (2008) cements gun ownership as an individual right, unlinked from militia service.",
            "Tens of thousands die from guns yearly; political gridlock blocks reform.",
        ],
        "illustration_captions": [
            "Standing army marching past a disbanding militia; NRA-era shift panel; courtroom Heller gavel-strike; closing statistic card.",
            "Data-graphic beat: a bar chart ticking up, gridlocked Capitol silhouette.",
        ],
        "punch_tags": ["the army stays", "a right now", "gridlock"],
    },
    {
        "step": 6, "title": "Two Symbols",
        "duration_sec_min": 16, "duration_sec_max": 18,
        "shot_count_min": 6, "shot_count_max": 7,
        "content_points": [
            "The gap isn't policy — it's two different founding stories.",
            "Switzerland: guns as community duty. America: guns as individual identity.",
            "As long as it's identity, not law, real policy change stays unlikely.",
        ],
        "illustration_captions": [
            "Split-screen final panel: a rifle racked at home (CH) beside a rifle worn as identity (US).",
            "Narrator-reaction close on symbols, held slightly longer than the body pace.",
        ],
        "punch_tags": ["not policy", "two stories", "symbols"],
    },
]

PRODUCTION_NOTES = [
    {
        "heading": "One narrator, drawn once",
        "text": "Design a single recurring figure for reaction shots and reuse it verbatim across all six Steps — consistency here is what makes the fast cutting elsewhere read as one show, not a slideshow.",
    },
    {
        "heading": "Caption rule",
        "text": "One highlighted word per caption line, timed to the beat of the voiceover — not the whole sentence. The punch words listed per Step are the candidates.",
    },
    {
        "heading": "Motion budget",
        "text": "Each illustration gets one slow pan or zoom, direction chosen by what the sentence is doing (push in on a name, pull back on a place). No shot holds static longer than ~4s outside the CTA.",
    },
    {
        "heading": "Step cards as breathers",
        "text": "Keep every Step-card dip near 1s — short enough to read as a beat, not a pause. It's the reset that lets the next Step open at full pace.",
    },
]

DEK = (
    "A 6-step illustrated recut of the Swiss/American gun-rights video, built on the shot-pacing "
    "template measured from @Null.histories's Opium Wars explainer (69 shots, 2.54s average, 175s runtime)."
)


def run():
    conn = get_conn()
    runtime = sum((b["duration_sec_min"] + b["duration_sec_max"]) / 2 for b in BEATS)
    shot_min = sum(b["shot_count_min"] for b in BEATS)
    shot_max = sum(b["shot_count_max"] for b in BEATS)

    cur = conn.execute(
        """UPDATE production_spec_creations SET
           dek = ?, beats_json = ?, production_notes_json = ?,
           target_runtime_sec = ?, target_shot_count_min = ?, target_shot_count_max = ?,
           status = 'generated', generation_error = NULL, generated_at = datetime('now')
           WHERE title = 'PPE Engine · Production Spec, The Militia Divide' AND beats_json IS NULL""",
        (DEK, json.dumps(BEATS), json.dumps(PRODUCTION_NOTES), runtime, shot_min, shot_max),
    )
    conn.commit()
    if cur.rowcount:
        print(f"Backfilled 'The Militia Divide' creation ({cur.rowcount} row).")
    else:
        print("No matching row to backfill (already generated, or title doesn't match) — no-op.")
    conn.close()


if __name__ == "__main__":
    run()
