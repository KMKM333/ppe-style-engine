"""
migrate_add_style_shot_types.py — a style that is several looks, not one.

@johnnyharris does not have a look; he has about six, cut between on purpose:
evidence photographed on a desk, a parchment map with badges, a dark map with
a route drawing in, a scanned document with highlighter, archival footage, a
chart on paper. A style holding one prompt could reproduce one of them for a
whole video, which is exactly what the desk-only render did — right idiom, but
every shot the same shot.

shot_types_json is a list of {key, label, weight, cues, beats, prompt,
reference_images, motion}. The renderer picks one per shot — cue words first,
then the beat, then whichever is furthest below its share — and uses that
type's prompt and reference images for that panel.

Safe to re-run.
"""
from db_init import get_conn


def run():
    conn = get_conn()
    have = {r["name"] for r in conn.execute("PRAGMA table_info(render_styles)")}
    if "shot_types_json" not in have:
        conn.execute("ALTER TABLE render_styles ADD COLUMN shot_types_json TEXT")
        print("[migrate_add_style_shot_types] + render_styles.shot_types_json")
    conn.commit()
    conn.close()
    print("[migrate_add_style_shot_types] ready.")


if __name__ == "__main__":
    run()
