"""
backfill_cta_heuristic.py — one-off backfill applying the new
feature_extraction.classify_cta() heuristic to every video_attributes row
that has no CTA classification yet (cta_type IS NULL). CTA is now a
heuristic Auto attribute like title_format, not an LLM-only Class
attribute, so this fills in real videos the same way ingest would if run
today — it never overwrites an existing (e.g. LLM-classified) value.

Usage:
    python3 backfill_cta_heuristic.py            # all videos missing cta_type
    python3 backfill_cta_heuristic.py --channel "Philosophyminis"
"""
import argparse

from db_init import get_conn
from feature_extraction import classify_cta


def run(channel_name=None):
    conn = get_conn()
    q = """SELECT v.video_id, v.script FROM videos v
           JOIN video_attributes a ON a.video_id = v.video_id
           WHERE a.cta_type IS NULL"""
    params = []
    if channel_name:
        q += " AND v.channel_id = (SELECT channel_id FROM channels WHERE channel_name = ?)"
        params.append(channel_name)
    rows = conn.execute(q, params).fetchall()

    n = 0
    for r in rows:
        cta = classify_cta(r["script"] or "")
        conn.execute(
            "UPDATE video_attributes SET has_cta=?, cta_type=?, cta_placement=?, cta_count=? WHERE video_id=?",
            (cta["has_cta"], cta["cta_type"], cta["cta_placement"], cta["cta_count"], r["video_id"]),
        )
        n += 1
    conn.commit()
    conn.close()
    print(f"Backfilled CTA heuristic for {n} video(s).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default=None)
    args = ap.parse_args()
    run(args.channel)
