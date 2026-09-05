"""
migrate_add_video_timelines.py — measured editing, sound and movement, one
row per video.

The vision pass says what is IN a frame. It cannot say how loud a video is,
where it pauses, whether the camera moves, or whether cuts land on the beat —
and those are most of what "editing style" means. All of it is arithmetic on
the file (see instagram_transcriber/analyse_timeline.py), so it costs nothing
and can be recomputed at any time from the cached source video.

Keyed by content_hash rather than by input_id, because one video may exist as
a shot-analysis input, a P+S input and a Library video at once, and the
measurement is a property of the FILE, not of any one pipeline's row.

Safe to re-run: CREATE TABLE IF NOT EXISTS.

Usage:
    python3 migrate_add_video_timelines.py
"""
from db_init import get_conn

TABLE = """
CREATE TABLE IF NOT EXISTS video_timelines (
    content_hash        TEXT PRIMARY KEY,
    url                   TEXT,
    title                   TEXT,
    channel_name              TEXT,
    duration_sec                REAL,

    -- editing
    n_cuts                        INTEGER,
    n_shots                         INTEGER,
    cuts_per_min                      REAL,
    avg_shot_sec                        REAL,
    median_shot_sec                       REAL,
    shortest_shot_sec                       REAL,
    longest_shot_sec                          REAL,
    pct_cuts_on_a_pause                         REAL,  -- undefined on music-backed video
    pct_cuts_on_a_beat                            REAL,  -- what "beat-synced" means, measured
    est_bpm                                         REAL,

    -- sound: every 'Role of sound' reading before this was inferred from
    -- whether a transcript existed. These come from listening to the track.
    integrated_lufs                               REAL,
    loudness_range_lu                               REAL,
    true_peak_dbfs                                    REAL,
    n_pauses                                            INTEGER,
    pct_quiet                                             REAL,

    -- movement: distinguishes accounts that cut at the SAME rate but feel
    -- completely different — 85% near-still versus 41% is two kinds of video.
    motion_mean                                             REAL,
    motion_max                                                REAL,
    pct_near_still                                              REAL,

    -- picture
    mean_luma                                                     REAL,
    luma_stdev                                                      REAL,

    detail_json                                                       TEXT,  -- series, cut times, pauses
    analysed_at                                                         TEXT DEFAULT (datetime('now'))
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_video_timelines_channel ON video_timelines(channel_name)",
]


ADDED_COLUMNS = [
    ("video_timelines", "pct_cuts_on_a_beat", "REAL"),
    ("video_timelines", "est_bpm", "REAL"),
]


def run():
    conn = get_conn()
    conn.execute(TABLE)
    for table, column, coltype in ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            print(f"[migrate_add_video_timelines] added {table}.{column}")
    for idx in INDEXES:
        conn.execute(idx)
    conn.commit()
    conn.close()
    print("[migrate_add_video_timelines] video_timelines ready.")


if __name__ == "__main__":
    run()
