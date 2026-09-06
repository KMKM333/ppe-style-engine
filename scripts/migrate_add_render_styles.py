"""
migrate_add_render_styles.py — a look you can name and reuse.

visual_style_briefs describes what ONE ACCOUNT's pictures look like, derived
from that account's own frames. That is the right shape for "what does
@searchpartynp look like", and the wrong shape for what this table holds.

Two panels generated on 2026-09-06 for the same beat came back in completely
different media — a flat vector poster and a photographic evidence-board
collage — and both were better than anything the pipeline had produced. Neither
belongs to an account: they are recipes, arrived at by prompting, and the
difference between them is the MEDIUM, which is the one thing a paraphrase of
an account's brief kept losing.

So a render style is authored rather than measured, has a name a person picks
it by, and carries the prompt text that actually produces it. `derived_from`
records what it came out of so the provenance is not folklore.

Safe to re-run: CREATE TABLE IF NOT EXISTS.

Usage:
    python3 migrate_add_render_styles.py
"""
from db_init import get_conn

TABLE = """
CREATE TABLE IF NOT EXISTS render_styles (
    style_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    slug              TEXT NOT NULL UNIQUE,   -- how the renderer asks for it
    name                TEXT NOT NULL,
    medium                TEXT,   -- the thing a paraphrase loses: vector? photographic collage?
    prompt_prefix           TEXT,   -- prepended to every panel prompt, verbatim
    palette_json              TEXT, -- ["#c17a3d", ...]
    avoid                       TEXT,
    caption_mode                  TEXT,   -- 'baked' (model draws it) or 'overlay' (we draw it)
    notes                           TEXT,
    derived_from                      TEXT, -- what this was arrived at from
    created_at                          TEXT DEFAULT (datetime('now'))
);
"""


def run():
    conn = get_conn()
    conn.execute(TABLE)
    conn.commit()
    conn.close()
    print("[migrate_add_render_styles] ready.")


if __name__ == "__main__":
    run()
