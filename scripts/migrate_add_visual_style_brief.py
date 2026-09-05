"""
migrate_add_visual_style_brief.py — stores the LOOK of a production account.

The PS profile measures how an account is CUT and the PVS profile how its
words and pictures RELATE. Neither says what its pictures look like: palette,
art style, composition, typography. That gap is why a spec could describe a
shot ("illustration panel: the shipyard at full tilt") without saying how it
should be drawn, so nothing could reproduce the account's visual identity.

Derived from that account's own stored frames rather than asserted, and kept
in its own table because it is a different kind of claim from a fingerprint:
descriptive prose and colour values, not numbers a scorer averages.

Safe to re-run: CREATE TABLE IF NOT EXISTS.

Usage:
    python3 migrate_add_visual_style_brief.py
"""
from db_init import get_conn

TABLE = """
CREATE TABLE IF NOT EXISTS visual_style_briefs (
    channel_id        INTEGER PRIMARY KEY REFERENCES channels(channel_id),
    palette_json        TEXT,   -- ["#0e1116", ...] dominant colours, most-used first
    art_style             TEXT, -- how the pictures are made
    composition             TEXT,
    typography                TEXT,
    subject_treatment           TEXT,
    motion                        TEXT,
    avoid                           TEXT,  -- what would read as the wrong account
    image_prompt_template             TEXT, -- reusable brief for generating a panel
    n_frames_analysed                   INTEGER DEFAULT 0,
    analysed_at                           TEXT DEFAULT (datetime('now'))
);
"""


def run():
    conn = get_conn()
    conn.execute(TABLE)
    conn.commit()
    conn.close()
    print("[migrate_add_visual_style_brief] visual_style_briefs ready.")


if __name__ == "__main__":
    run()
