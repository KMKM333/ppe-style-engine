"""
migrate_add_format_inputs.py — per-video storage for the Production Inputs
(P+S) pipeline: one row per analysed video, plus its per-axis readings.

Shape mirrors the production-spec pipeline (inputs -> attributes -> profile)
rather than writing straight onto format_profiles, for a reason that matters:
a creator may use several formats. Storing a reading per VIDEO means the
SPREAD across an account is visible — whether they're formally consistent or
varied is itself a style signal, and it's one you destroy by assuming a
single format per account and overwriting.

It also lets each axis flip from 'preliminary' (asserted by hand when the
profile was seeded) to 'classified' independently, so the pages can always
show which readings have been earned and which are still guesses.

Safe to re-run: CREATE TABLE IF NOT EXISTS throughout.

Usage:
    python3 migrate_add_format_inputs.py
"""
from db_init import get_conn

FORMAT_INPUTS_TABLE = """
CREATE TABLE IF NOT EXISTS format_inputs (
    format_input_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    format_profile_id     INTEGER REFERENCES format_profiles(format_profile_id),
    title                   TEXT,
    url                       TEXT,
    platform                    TEXT,
    duration_sec                  REAL,
    content_hash                    TEXT,   -- normalized URL hash, dedupes re-submits
    transcript                        TEXT, -- empty/NULL for a silent format; that is data, not a failure
    has_audio                           INTEGER DEFAULT 0,
    n_frames                              INTEGER DEFAULT 0,
    on_screen_text                          TEXT,  -- text the vision pass read out of the frames
    text_audio_relation                       TEXT,  -- same / differs / no_audio / no_on_screen_text
                                              -- Captions that DIFFER from the speech are the
                                              -- clearest evidence of borrowed audio with the
                                              -- creator's own writing on screen.
    status                                    TEXT DEFAULT 'ingested',
                                              -- ingested / classifying / classified / needs_review
    classification_error                        TEXT,
    ingested_at                                   TEXT DEFAULT (datetime('now'))
);
"""

FORMAT_INPUT_FRAMES_TABLE = """
CREATE TABLE IF NOT EXISTS format_input_frames (
    frame_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    format_input_id     INTEGER REFERENCES format_inputs(format_input_id),
    frame_number          INTEGER NOT NULL,
    at_sec                  REAL,
    captured                  INTEGER DEFAULT 0,
    created_at                  TEXT DEFAULT (datetime('now'))
);
"""

FORMAT_INPUT_READINGS_TABLE = """
CREATE TABLE IF NOT EXISTS format_input_readings (
    format_input_id   INTEGER REFERENCES format_inputs(format_input_id),
    axis                TEXT NOT NULL,
    value                 TEXT NOT NULL,
    note                    TEXT,
    PRIMARY KEY (format_input_id, axis)
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_format_inputs_profile ON format_inputs(format_profile_id)",
    "CREATE INDEX IF NOT EXISTS idx_format_inputs_hash ON format_inputs(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_format_input_frames_input ON format_input_frames(format_input_id)",
]


# Columns added after the table first shipped. CREATE TABLE IF NOT EXISTS is a
# no-op once the table exists, so a new column needs an explicit ALTER —
# same pattern as the other add-column migrations in this project.
ADDED_COLUMNS = [
    ("format_inputs", "text_audio_relation", "TEXT"),
]


def run():
    conn = get_conn()
    for stmt in (FORMAT_INPUTS_TABLE, FORMAT_INPUT_FRAMES_TABLE, FORMAT_INPUT_READINGS_TABLE):
        conn.execute(stmt)
    for idx in INDEXES:
        conn.execute(idx)

    for table, column, coltype in ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            print(f"[migrate_add_format_inputs] added {table}.{column}")

    conn.commit()
    conn.close()
    print("[migrate_add_format_inputs] format_inputs / format_input_frames / format_input_readings ready.")


if __name__ == "__main__":
    run()
