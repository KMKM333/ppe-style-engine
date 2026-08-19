"""db_init.py — creates (or reconnects to) the SQLite database from schema.sql"""
import os
import sqlite3
import time
from pathlib import Path

# PPE_DB_PATH lets a deploy point the live database at a mounted disk
# (e.g. a Render persistent disk) without colliding with schema.sql, which
# ships inside the image at db/schema.sql — that path must stay separate
# from wherever the disk is mounted, since a volume mount hides whatever
# was baked into the image at that same path.
DB_PATH = Path(os.environ.get("PPE_DB_PATH", str(Path(__file__).parent.parent / "db" / "ppe_engine.db")))
SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema.sql"

# Page screenshots for book examples live alongside the DB on the same disk
# (the Render persistent disk in production, db/ locally) so they survive
# deploys the same way the DB does. Layout: BOOK_PAGES_DIR/<book_id>/page_0042.png
BOOK_PAGES_DIR = DB_PATH.parent / "book_pages"

# Full source PDFs, same disk/persistence story as BOOK_PAGES_DIR above.
# Layout: BOOK_FILES_DIR/<book_id>.pdf
BOOK_FILES_DIR = DB_PATH.parent / "book_files"

# Real screenshots of on-screen charts/graphs/tables, captured locally by
# the Instagram Bulk Transcriber (the only place with access to the actual
# video) and uploaded here — same disk/persistence story as BOOK_PAGES_DIR.
# Layout: VIDEO_VISUALS_DIR/<video_id>/visual_<visual_id>.png
VIDEO_VISUALS_DIR = DB_PATH.parent / "video_visuals"


def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # timeout=30 (vs sqlite3's 5s default) + WAL journal mode: batches of
    # long-form videos each spawn their own detached auto_process_video.py
    # subprocess, so several heavy classification/breakdown/visuals/profile
    # merges can be writing concurrently — the 5s default was observed
    # producing "database is locked" 500s on /api/ingest/video mid-batch.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    conn = get_conn()
    try:
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())
        conn.commit()
    except sqlite3.DatabaseError:
        # DB_PATH exists but isn't a valid SQLite file — e.g. a restore that
        # was interrupted mid-write and left the file truncated. Move it
        # aside (never delete) so the app can boot on a fresh DB instead of
        # crash-looping forever, and leave the bad file for manual recovery.
        conn.close()
        quarantined = DB_PATH.with_name(f"{DB_PATH.stem}_corrupt_{int(time.time())}{DB_PATH.suffix}")
        DB_PATH.rename(quarantined)
        print(f"DB at {DB_PATH} was not a valid SQLite file; moved it to {quarantined} and starting fresh.")
        conn = get_conn()
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())
        conn.commit()

    # seed a default scoring_weights row per numeric attribute (weight=1.0)
    numeric_attrs = [
        "word_count", "avg_sentence_len", "sentence_len_variance",
        "you_freq_per_100w", "i_freq_per_100w", "question_count",
        "emdash_count", "quote_count", "jargon_density", "readability_score",
        "filler_count", "beat_count", "number_count", "instruction_verb_count",
        "framework_marker_count", "sentence_rhythm_cv", "closing_paragraph_ratio",
        "lexical_diversity", "punctuation_density", "colloquialism_density",
        "contrast_structure_count", "named_entity_count", "humor_marker_count",
    ]
    categorical_attrs = [
        "title_format", "hook_type", "close_type", "citation_style",
        "domain", "concept_type", "framing", "certainty_register",
        "reveal_placement", "cta_type", "explanation_mechanism", "rhetorical_mode",
    ]
    for a in numeric_attrs:
        conn.execute(
            "INSERT OR IGNORE INTO scoring_weights (attribute, weight, attribute_kind) VALUES (?, 1.0, 'numeric')",
            (a,),
        )
    for a in categorical_attrs:
        conn.execute(
            "INSERT OR IGNORE INTO scoring_weights (attribute, weight, attribute_kind) VALUES (?, 1.0, 'categorical')",
            (a,),
        )
    conn.commit()
    conn.close()
    print(f"DB initialised at {DB_PATH}")


if __name__ == "__main__":
    init_db()
