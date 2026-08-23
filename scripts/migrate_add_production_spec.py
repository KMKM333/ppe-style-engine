"""
migrate_add_production_spec.py — adds the schema needed for the Production
Spec pipeline: shot/pacing analysis of reference videos (production_spec_inputs,
production_spec_shots, production_spec_attributes), parallel to the writing-style
pipeline, plus a lightweight video_creations tracking table.

Safe to re-run: uses CREATE TABLE/INDEX IF NOT EXISTS throughout.

Usage:
    python3 migrate_add_production_spec.py
"""
from db_init import get_conn

PRODUCTION_SPEC_INPUTS_TABLE = """
CREATE TABLE IF NOT EXISTS production_spec_inputs (
    input_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id            INTEGER REFERENCES channels(channel_id),
    title                  TEXT,
    platform                TEXT,
    url                       TEXT,
    duration_sec                REAL,
    posted_at                     TEXT,
    content_hash                   TEXT,
    scene_threshold                  REAL DEFAULT 0.25,
    status                             TEXT DEFAULT 'ingested',
    classification_error                 TEXT,
    ingested_at                            TEXT DEFAULT (datetime('now'))
)
"""

PRODUCTION_SPEC_INPUTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_production_spec_inputs_content_hash ON production_spec_inputs(content_hash)
"""

PRODUCTION_SPEC_SHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS production_spec_shots (
    shot_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    input_id             INTEGER REFERENCES production_spec_inputs(input_id),
    shot_number            INTEGER NOT NULL,
    start_sec                 REAL NOT NULL,
    end_sec                     REAL,
    duration_sec                  REAL,
    content_category                TEXT,
    classified_by                     TEXT,
    frame_captured                      INTEGER DEFAULT 0,
    created_at                             TEXT DEFAULT (datetime('now'))
)
"""

PRODUCTION_SPEC_SHOTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_production_spec_shots_input ON production_spec_shots(input_id)
"""

PRODUCTION_SPEC_ATTRIBUTES_TABLE = """
CREATE TABLE IF NOT EXISTS production_spec_attributes (
    input_id                 INTEGER PRIMARY KEY REFERENCES production_spec_inputs(input_id),
    total_shots                 INTEGER,
    avg_shot_length_sec            REAL,
    median_shot_length_sec            REAL,
    shot_length_stdev                    REAL,
    pct_shots_under_1s                      REAL,
    pct_shots_under_2s                         REAL,
    pct_shots_2to5s                               REAL,
    pct_shots_over_5s                                REAL,
    pct_illustration_panel                              REAL,
    pct_step_card                                          REAL,
    pct_narrator_reaction                                     REAL,
    pct_map_data_graphic                                         REAL,
    pct_cta                                                         REAL,
    pct_other                                                         REAL,
    dominant_shot_category                                              TEXT,
    pacing_curve_json                                                      TEXT,
    classified_by                                                             TEXT,
    classified_at                                                                TEXT DEFAULT (datetime('now'))
)
"""

VIDEO_CREATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS video_creations (
    creation_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_input_id        INTEGER REFERENCES production_spec_inputs(input_id),
    target_profile_id         INTEGER REFERENCES style_profiles(profile_id),
    title                       TEXT,
    brief                         TEXT,
    status                          TEXT DEFAULT 'planned',
    generation_tool                    TEXT,
    output_url                            TEXT,
    output_file_path                         TEXT,
    created_by                                  TEXT DEFAULT 'manual',
    created_at                                     TEXT DEFAULT (datetime('now')),
    updated_at                                        TEXT DEFAULT (datetime('now'))
)
"""


def run():
    conn = get_conn()
    conn.execute(PRODUCTION_SPEC_INPUTS_TABLE)
    conn.execute(PRODUCTION_SPEC_INPUTS_INDEX)
    conn.execute(PRODUCTION_SPEC_SHOTS_TABLE)
    conn.execute(PRODUCTION_SPEC_SHOTS_INDEX)
    conn.execute(PRODUCTION_SPEC_ATTRIBUTES_TABLE)
    conn.execute(VIDEO_CREATIONS_TABLE)
    conn.commit()
    conn.close()
    print("production_spec_inputs, production_spec_shots, production_spec_attributes, "
          "video_creations tables ensured.")


if __name__ == "__main__":
    run()
