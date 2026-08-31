"""
migrate_add_swipe_candidates.py — adds swipe_candidates, the storage for
the mobile input-triage mechanism (v1, single-source only). See
db/schema.sql for column meanings.

Safe to re-run: CREATE TABLE/INDEX IF NOT EXISTS throughout.

Usage:
    python3 migrate_add_swipe_candidates.py
"""
from db_init import get_conn

SWIPE_CANDIDATES_TABLE = """
CREATE TABLE IF NOT EXISTS swipe_candidates (
    candidate_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    source_kind       TEXT NOT NULL,
    source_video_id   INTEGER REFERENCES videos(video_id),
    source_book_id    INTEGER REFERENCES books(book_id),
    hook              TEXT,
    pitch_summary     TEXT,
    terms_json        TEXT,
    examples_json     TEXT,
    status            TEXT DEFAULT 'queued',
    decided_at        TEXT,
    created_at        TEXT DEFAULT (datetime('now'))
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_swipe_candidates_status ON swipe_candidates(status)",
    "CREATE INDEX IF NOT EXISTS idx_swipe_candidates_source_video ON swipe_candidates(source_video_id)",
    "CREATE INDEX IF NOT EXISTS idx_swipe_candidates_source_book ON swipe_candidates(source_book_id)",
]


def run():
    conn = get_conn()
    conn.execute(SWIPE_CANDIDATES_TABLE)
    for stmt in INDEXES:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    print("swipe_candidates table + indexes ensured.")


if __name__ == "__main__":
    run()
