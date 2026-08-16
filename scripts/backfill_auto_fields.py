"""
backfill_auto_fields.py — one-off backfill for the two new regex/auto fields
(we_freq_per_100w, word_economy_ratio) added to video_attributes by
migrate_add_cross_media_attrs.py. These are deterministic (no LLM judgment),
so unlike the Class-field backfill this can run over every video in one pass.

Recomputes ONLY these two columns from each video's existing script text —
does not touch any other column (classified_by included).

Usage:
    python3 backfill_auto_fields.py
"""
from db_init import get_conn
from feature_extraction import extract_auto_features


def run():
    conn = get_conn()
    rows = conn.execute(
        """SELECT v.video_id, v.script FROM videos v
           JOIN video_attributes a ON a.video_id = v.video_id
           WHERE a.we_freq_per_100w IS NULL"""
    ).fetchall()
    n = 0
    for r in rows:
        feats = extract_auto_features(r["script"] or "")
        conn.execute(
            "UPDATE video_attributes SET we_freq_per_100w = ?, word_economy_ratio = ? WHERE video_id = ?",
            (feats["we_freq_per_100w"], feats["word_economy_ratio"], r["video_id"]),
        )
        n += 1
    conn.commit()
    conn.close()
    print(f"Backfilled we_freq_per_100w / word_economy_ratio for {n} video(s).")


if __name__ == "__main__":
    run()
