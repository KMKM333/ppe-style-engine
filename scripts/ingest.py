"""
ingest.py — loads a CSV/XLSX of transcribed videos (columns: Title, Script,
[optional] URL, Duration, PostedAt) for one channel into the DB, and runs
auto-feature extraction on each row immediately.

Usage:
    python3 ingest.py --file transcripts_philosophyminis.csv \
                       --channel "Philosophyminis" --platform Instagram --length_band A

CSV must have at least columns: Title, Script
"""
import argparse
import sqlite3
import pandas as pd
from pathlib import Path

from db_init import get_conn, init_db
from feature_extraction import extract_auto_features
from similarity import normalize_hash


def get_or_create_channel(conn, name, platform, length_band):
    row = conn.execute("SELECT channel_id FROM channels WHERE channel_name = ?", (name,)).fetchone()
    if row:
        return row["channel_id"]
    cur = conn.execute(
        "INSERT INTO channels (channel_name, platform, typical_length_band) VALUES (?, ?, ?)",
        (name, platform, length_band),
    )
    conn.commit()
    return cur.lastrowid


def ingest_file(file_path, channel_name, platform, length_band):
    conn = get_conn()
    channel_id = get_or_create_channel(conn, channel_name, platform, length_band)

    if str(file_path).endswith(".csv"):
        df = pd.read_csv(file_path)
    else:
        df = pd.read_excel(file_path)

    required = {"Title", "Script"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Input file is missing required columns: {missing}")

    # Content-hash dedup: re-ingesting a file you've already ingested (the
    # same batch run twice, or overlapping batches) would otherwise insert
    # every video a second time with no warning. content_hash is a
    # normalized-text hash, so this catches exact/near-identical re-imports
    # regardless of which channel they were ingested under.
    existing_hashes = {
        row["content_hash"] for row in conn.execute("SELECT content_hash FROM videos")
    }

    n_inserted = 0
    n_skipped = 0
    for _, row in df.iterrows():
        title = str(row["Title"]).strip()
        script = str(row["Script"]).strip()
        if not script or script.lower() == "nan":
            continue  # skip rows with no transcript yet

        chash = normalize_hash(script)
        if chash in existing_hashes:
            n_skipped += 1
            continue
        existing_hashes.add(chash)  # also catches duplicate rows within this same file

        url = row.get("URL") if "URL" in df.columns else None
        duration = row.get("Duration") if "Duration" in df.columns else None
        posted_at = row.get("PostedAt") if "PostedAt" in df.columns else None

        cur = conn.execute(
            "INSERT INTO videos (channel_id, title, script, url, duration_sec, posted_at, content_hash, media_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (channel_id, title, script, url, duration, posted_at, chash, platform),
        )
        video_id = cur.lastrowid

        feats = extract_auto_features(script, title)
        cols = ", ".join(feats.keys())
        placeholders = ", ".join(["?"] * len(feats))
        conn.execute(
            f"INSERT INTO video_attributes (video_id, {cols}) VALUES (?, {placeholders})",
            (video_id, *feats.values()),
        )
        n_inserted += 1

    conn.commit()
    conn.close()
    skip_note = f", skipped {n_skipped} already-ingested duplicate(s)" if n_skipped else ""
    print(f"Ingested {n_inserted} videos for channel '{channel_name}' (channel_id={channel_id}){skip_note}")
    return n_inserted, n_skipped


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--channel", required=True)
    ap.add_argument("--platform", default="Instagram")
    ap.add_argument("--length_band", default="A")
    args = ap.parse_args()

    if not Path(get_conn.__module__).exists:
        pass
    init_db()  # safe to call repeatedly, uses IF NOT EXISTS
    ingest_file(args.file, args.channel, args.platform, args.length_band)
