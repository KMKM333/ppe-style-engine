"""
migrate_add_cross_media_attrs.py — one-off migration adding the new
cross-media attribute columns to both video_attributes and book_attributes:

- video_attributes gets 2 new Auto (regex) fields (we_freq_per_100w,
  word_economy_ratio), 12 fields ported from the book rubric so video and
  books share exact field name + value vocabulary, 11 new shared fields
  (also added to book_attributes below), and 6 new video-only fields.
- book_attributes gets the same 11 new shared fields.

Same idempotent ALTER TABLE ADD COLUMN pattern as
migrate_add_style_craft_attrs.py — safe to re-run, skips any column
already present.

Usage:
    python3 migrate_add_cross_media_attrs.py
"""
from db_init import get_conn

VIDEO_NEW_COLUMNS = {
    # new Auto (regex) fields
    "we_freq_per_100w": "REAL",
    "word_economy_ratio": "REAL",
    # ported from the book rubric
    "tone": "TEXT", "emotional_register": "TEXT", "narrative_voice": "TEXT",
    "narrative_density": "TEXT", "counter_argument_engagement": "TEXT",
    "rhetorical_appeal_balance": "TEXT", "prose_rhythm": "TEXT",
    "noun_verb_ratio_style": "TEXT", "syntax_pattern": "TEXT", "pacing": "TEXT",
    "polemical_tone": "TEXT", "narrative_presence": "TEXT",
    # new shared fields (also added to book_attributes)
    "value_promise": "TEXT", "information_density": "TEXT", "curiosity_loop": "INTEGER",
    "relatability_factor": "TEXT", "identity_framing": "INTEGER",
    "contrarian_positioning": "TEXT", "adjective_intensity": "TEXT",
    "punctuation_delivery": "TEXT", "rhythmic_repetition": "INTEGER",
    "vulnerability_depth": "TEXT", "condescension_vs_empowerment": "TEXT",
    # new video-only fields
    "structure_archetype": "TEXT", "shareability_trigger": "TEXT",
    "product_placement": "TEXT", "core_value_reinforcement": "INTEGER",
    "status_signaling": "TEXT", "niche_slang_usage": "TEXT",
}

BOOK_NEW_COLUMNS = {
    "value_promise": "TEXT", "information_density": "TEXT", "curiosity_loop": "INTEGER",
    "relatability_factor": "TEXT", "identity_framing": "INTEGER",
    "contrarian_positioning": "TEXT", "adjective_intensity": "TEXT",
    "punctuation_delivery": "TEXT", "rhythmic_repetition": "INTEGER",
    "vulnerability_depth": "TEXT", "condescension_vs_empowerment": "TEXT",
}


def _add_columns(conn, table, columns):
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    added = []
    for col, sqltype in columns.items():
        if col in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {sqltype}")
        added.append(col)
    return added


def run():
    conn = get_conn()
    video_added = _add_columns(conn, "video_attributes", VIDEO_NEW_COLUMNS)
    book_added = _add_columns(conn, "book_attributes", BOOK_NEW_COLUMNS)
    conn.commit()
    conn.close()
    print(f"video_attributes: added {len(video_added)} column(s)"
          + (f" ({', '.join(video_added)})" if video_added else " — already present"))
    print(f"book_attributes: added {len(book_added)} column(s)"
          + (f" ({', '.join(book_added)})" if book_added else " — already present"))


if __name__ == "__main__":
    run()
