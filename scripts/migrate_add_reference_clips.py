"""
migrate_add_reference_clips.py — the clips, filed under the account they came from.

Ten seconds of a creator's real work beat every written description of their
style, so the clips are the asset worth having. Until now they were 441 files
in a directory on one server, named by content hash, belonging to nobody: you
could not answer "show me Johnny Harris's reference clips" without a script.

Each row ties a clip to its account and to the exact source and timestamps it
was cut from, so it can be found, watched, attached to a render style, and
re-cut if the file is ever lost.

Safe to re-run: CREATE TABLE IF NOT EXISTS.

Usage:
    python3 migrate_add_reference_clips.py
"""
from db_init import get_conn

TABLE = """
CREATE TABLE IF NOT EXISTS reference_clips (
    clip_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_key         TEXT NOT NULL UNIQUE,   -- key in the render-asset store
    url                 TEXT,                 -- public URL the renderer fetches
    channel_id            INTEGER REFERENCES channels(channel_id),
    channel_name            TEXT,             -- kept even when no channel row matched
    source_video_id           INTEGER REFERENCES videos(video_id),
    source_url                  TEXT,
    source_file                   TEXT,       -- cached file it was cut from
    start_sec                       REAL,
    duration_sec                      REAL,
    width                               INTEGER,
    height                                INTEGER,
    bytes                                   INTEGER,
    created_at                                TEXT DEFAULT (datetime('now'))
);
"""
INDEX = "CREATE INDEX IF NOT EXISTS idx_reference_clips_channel ON reference_clips(channel_name)"


def run():
    conn = get_conn()
    conn.execute(TABLE)
    conn.execute(INDEX)
    conn.commit()
    conn.close()
    print("[migrate_add_reference_clips] ready.")


if __name__ == "__main__":
    run()
