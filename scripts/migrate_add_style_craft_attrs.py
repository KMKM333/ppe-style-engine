"""
migrate_add_style_craft_attrs.py — one-off migration adding the 18 new
"Style & Craft" columns to book_attributes on an existing database (SQLite's
CREATE TABLE IF NOT EXISTS in db_init.py is a no-op once the table already
exists, so new columns need an explicit ALTER TABLE ADD COLUMN pass here).

Safe to re-run: skips any column that's already present.

Usage:
    python3 migrate_add_style_craft_attrs.py
"""
from db_init import get_conn

NEW_COLUMNS = [
    "diction", "syntax_pattern", "pacing", "emotional_register", "narrative_voice",
    "sensory_language_density", "narrative_distance", "figurative_language_density",
    "prose_rhythm", "argumentative_density", "abstraction_concreteness_balance",
    "noun_verb_ratio_style", "jargon_accessibility", "cognitive_metaphor_domain",
    "hedging_vs_assertion", "polemical_tone", "narrative_presence", "rhetorical_questioning",
]


def run():
    conn = get_conn()
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(book_attributes)").fetchall()}
    added = []
    for col in NEW_COLUMNS:
        if col in existing:
            continue
        conn.execute(f"ALTER TABLE book_attributes ADD COLUMN {col} TEXT")
        added.append(col)
    conn.commit()
    conn.close()
    if added:
        print(f"Added {len(added)} column(s): {', '.join(added)}")
    else:
        print("All columns already present — nothing to do.")


if __name__ == "__main__":
    run()
