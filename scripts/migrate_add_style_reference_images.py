"""
migrate_add_style_reference_images.py — known-good pictures a style can show the model.

A style could hold ONE reference video clip. It could not hold a set of still
images, and stills are what OpenAI's edits endpoint takes as visual reference —
up to sixteen per call. So an output that came out right (an Arena render the
user judged correct) had nowhere to live except a screenshot in a chat.

reference_images_json is a list of public image URLs. The renderer puts them
FIRST when it assembles references for a panel, ahead of frames sampled from the
account, so the pictures a person chose always make the cut.

Safe to re-run.
"""
from db_init import get_conn


def run():
    conn = get_conn()
    have = {r["name"] for r in conn.execute("PRAGMA table_info(render_styles)")}
    if "reference_images_json" not in have:
        conn.execute("ALTER TABLE render_styles ADD COLUMN reference_images_json TEXT")
        print("[migrate_add_style_reference_images] + render_styles.reference_images_json")
    conn.commit()
    conn.close()
    print("[migrate_add_style_reference_images] ready.")


if __name__ == "__main__":
    run()
