"""db_init.py — creates (or reconnects to) the SQLite database from schema.sql"""
import os
import sqlite3
from pathlib import Path

# PPE_DB_PATH lets a deploy point the live database at a mounted disk
# (e.g. a Render persistent disk) without colliding with schema.sql, which
# ships inside the image at db/schema.sql — that path must stay separate
# from wherever the disk is mounted, since a volume mount hides whatever
# was baked into the image at that same path.
DB_PATH = Path(os.environ.get("PPE_DB_PATH", str(Path(__file__).parent.parent / "db" / "ppe_engine.db")))
SCHEMA_PATH = Path(__file__).parent.parent / "db" / "schema.sql"


def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
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
