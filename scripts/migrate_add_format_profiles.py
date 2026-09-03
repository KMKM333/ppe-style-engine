"""
migrate_add_format_profiles.py — adds the Production Inputs (P+S) layer:
FORMAT profiles (PVS.*), which describe how a creator's VISUALS and SCRIPT
relate to each other.

Why this exists as its own thing rather than more columns on an existing
profile: the library pipeline measures what a video SAYS (A.*/BK.*) and the
production pipeline measures how it is CUT (PS.*), and both assume the two
are separable — analyse each, pair them later. For several real formats
that assumption fails outright:

  - a silent comic account has no audio at all, so the library pipeline
    returns nothing and no A.* profile is even possible;
  - a meme-audio account's transcript is SOMEBODY ELSE'S words, so
    profiling it as their writing style is not merely thin but wrong;
  - a caption+cartoon joke lives in the combination, and neither the frames
    nor the text contains it alone.

So the interesting attributes here are RELATIONAL, and unobservable from
either pipeline in isolation.

Deliberately kept OUT of profile_fingerprint_numeric/categorical: those
tables are read by score_engine, and everything seeded below is ASSERTED
(from a description of each channel) rather than MEASURED from analysed
videos. Putting unmeasured values where the scorer reads them is exactly
how profile A.1 came to look 'confirmed' while knowing almost nothing.
Every seeded row is therefore marked source='preliminary', and every
profile starts status='preliminary' with n_inputs_analysed = 0.

Safe to re-run: CREATE TABLE IF NOT EXISTS + INSERT OR IGNORE throughout,
so it never overwrites values that a real classification pass has since
refined.

Usage:
    python3 migrate_add_format_profiles.py
"""
from db_init import get_conn

FORMAT_PROFILES_TABLE = """
CREATE TABLE IF NOT EXISTS format_profiles (
    format_profile_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_code          TEXT UNIQUE NOT NULL,   -- 'PVS.1'
    handle                  TEXT,                 -- '@art.of.boo'
    display_name              TEXT,
    channel_id                  INTEGER REFERENCES channels(channel_id),
    style_profile_id              INTEGER REFERENCES style_profiles(profile_id),      -- A.* when one exists
    production_profile_id           INTEGER REFERENCES style_profiles(profile_id),    -- PS.* when one exists
    summary                           TEXT,       -- one-line description of the format
    breaks_today                        TEXT,     -- what the current split gets wrong for this account
    status                                TEXT DEFAULT 'preliminary',  -- preliminary / confirmed
    n_inputs_analysed                       INTEGER DEFAULT 0,
    created_at                                TEXT DEFAULT (datetime('now'))
);
"""

# One row per (profile, axis). source distinguishes an asserted starting
# point from a value a real joint classification pass produced, so the two
# can never be mistaken for each other on the page.
FORMAT_ATTRS_TABLE = """
CREATE TABLE IF NOT EXISTS format_profile_attributes (
    format_profile_id   INTEGER REFERENCES format_profiles(format_profile_id),
    axis                  TEXT NOT NULL,
    value                   TEXT NOT NULL,
    note                      TEXT,
    source                      TEXT DEFAULT 'preliminary',  -- preliminary / classified
    PRIMARY KEY (format_profile_id, axis)
);
"""

# The axes. Values are a controlled vocabulary so a later classifier has a
# fixed set to choose from, and so the page can show what a value is being
# contrasted against rather than presenting it as free text.
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

# Preliminary readings, derived from the user's description of each account
# (2026-09-02) — NOT from analysed videos. Expect these to move once real
# inputs are imported; that is the point of showing them.
SEED = [
    {
        "code": "PVS.1", "handle": "@theintrovertedattorney", "name": "The Introverted Attorney",
        "summary": "Satirical monologue. The script carries essentially all of the meaning; visuals are presence, not argument.",
        "breaks_today": "Handled adequately by the library pipeline alone — the production side adds little, because the visuals aren't doing narrative work.",
        "attrs": {
            "verbal_channel": ("Spoken monologue", "Delivered to camera; the transcript is the piece."),
            "verbal_authorship": ("Original", "Own writing throughout."),
            "visual_role": ("Decorative", "Framing and presence rather than illustration — removing the visuals would lose little."),
            "audio_role": ("Primary", "Voice is the whole delivery vehicle."),
            "coupling": ("Independent", "Visuals don't track the argument's beats."),
        },
    },
    {
        "code": "PVS.2", "handle": "@americanbaron", "name": "American Baron",
        "summary": "Recognisable meme audio re-scored with new visuals and titles. The borrowed track is the hook; the original writing is in the titles.",
        "breaks_today": "ACTIVELY WRONG today: the transcript is the borrowed audio, so a library profile would measure somebody else's words as this creator's writing style.",
        "attrs": {
            "verbal_channel": ("Borrowed audio", "Spoken words come from a pre-existing meme track."),
            "verbal_authorship": ("Hybrid", "Audio borrowed; titles and on-screen framing original — the original authorship lives ONLY in the titles."),
            "visual_role": ("Carry meaning alone", "The joke is the gap between the familiar audio and the new visuals."),
            "audio_role": ("Borrowed hook", "Recognition of the track does the attention work."),
            "coupling": ("Beat-synced", "Cuts land on the audio's beats — that timing is the craft."),
        },
    },
    {
        "code": "PVS.3", "handle": "@art.of.boo", "name": "Art of Boo",
        "summary": "Silent illustrated comics. Words are drawn into the panels; there is no spoken track at all.",
        "breaks_today": "IMPOSSIBLE today: no audio means transcription returns nothing, so no library profile can be built. The script exists only inside the images.",
        "attrs": {
            "verbal_channel": ("On-screen text", "All words are lettering within the panels."),
            "verbal_authorship": ("Original", "Own writing, drawn rather than spoken."),
            "visual_role": ("Are the content", "The panels aren't illustrating a script — they ARE the script."),
            "audio_role": ("Silent", "No spoken track; sound is absent by design."),
            "coupling": ("Beat-synced", "Panel and text are authored as one unit and cannot be separated."),
        },
    },
    {
        "code": "PVS.4", "handle": "@casuallyfinance", "name": "Casually Finance",
        "summary": "PPE-topic explainers: spoken script with simple supporting visuals that track the argument.",
        "breaks_today": "The one format the current split already handles cleanly — which is exactly why it's the right place to validate the pipeline first.",
        "attrs": {
            "verbal_channel": ("Spoken monologue", "Narrated explainer."),
            "verbal_authorship": ("Original", "Own script."),
            "visual_role": ("Illustrate the script", "Simple visuals restate what's being said."),
            "audio_role": ("Primary", "Narration leads; visuals follow."),
            "coupling": ("Beat-synced", "Visuals change as the argument moves — the clearest visual/script correspondence of the five."),
        },
    },
    {
        "code": "PVS.5", "handle": "@paulnoth", "name": "Paul Noth",
        "summary": "Single-panel satirical cartoons carrying PPE-adjacent concepts, in the caption + drawing tradition.",
        "breaks_today": "Neither pipeline alone contains the joke: it exists in the relationship between caption and drawing, and is invisible to each in isolation.",
        "attrs": {
            "verbal_channel": ("Caption only", "One line of text against the drawing."),
            "verbal_authorship": ("Original", "Own caption and own drawing."),
            "visual_role": ("Carry meaning alone", "The drawing carries half the joke; the caption is inert without it."),
            "audio_role": ("Silent", "Static cartoon, no track."),
            "coupling": ("Beat-synced", "Caption and image are one gag — the setup/punch split runs across the two."),
        },
    },
]


def run():
    conn = get_conn()
    conn.execute(FORMAT_PROFILES_TABLE)
    conn.execute(FORMAT_ATTRS_TABLE)
    conn.commit()

    for spec in SEED:
        conn.execute(
            """INSERT OR IGNORE INTO format_profiles
               (profile_code, handle, display_name, summary, breaks_today, status, n_inputs_analysed)
               VALUES (?, ?, ?, ?, ?, 'preliminary', 0)""",
            (spec["code"], spec["handle"], spec["name"], spec["summary"], spec["breaks_today"]),
        )
        row = conn.execute(
            "SELECT format_profile_id FROM format_profiles WHERE profile_code = ?", (spec["code"],)
        ).fetchone()
        fpid = row["format_profile_id"]

        # Link to the channel's existing A.*/PS.* profiles when the handle
        # matches a known channel — several of these five have neither yet,
        # which is itself worth showing on the page.
        handle_name = spec["handle"].lstrip("@")
        chan = conn.execute(
            "SELECT channel_id FROM channels WHERE LOWER(REPLACE(channel_name,' ','')) = ?",
            (handle_name.lower().replace(" ", ""),),
        ).fetchone()
        if chan:
            style = conn.execute(
                "SELECT profile_id FROM style_profiles WHERE channel_id = ? AND media_type != 'ProductionSpec'",
                (chan["channel_id"],),
            ).fetchone()
            prod = conn.execute(
                "SELECT profile_id FROM style_profiles WHERE channel_id = ? AND media_type = 'ProductionSpec'",
                (chan["channel_id"],),
            ).fetchone()
            conn.execute(
                "UPDATE format_profiles SET channel_id = ?, style_profile_id = ?, production_profile_id = ? "
                "WHERE format_profile_id = ?",
                (chan["channel_id"], style["profile_id"] if style else None,
                 prod["profile_id"] if prod else None, fpid),
            )

        for axis, (value, note) in spec["attrs"].items():
            conn.execute(
                """INSERT OR IGNORE INTO format_profile_attributes
                   (format_profile_id, axis, value, note, source) VALUES (?, ?, ?, ?, 'preliminary')""",
                (fpid, axis, value, note),
            )

    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM format_profiles").fetchone()[0]
    a = conn.execute("SELECT COUNT(*) FROM format_profile_attributes").fetchone()[0]
    conn.close()
    print(f"[migrate_add_format_profiles] {n} format profile(s), {a} attribute row(s).")


if __name__ == "__main__":
    run()
