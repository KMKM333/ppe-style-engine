"""
migrate_add_channel_subject.py — a subject for channels that have no profile.

Subject lives on the style profile, but a channel only gets a profile once its
videos are analysed, so on the Books / Short videos / Long videos pages every
channel still waiting for one showed "—" in the Subject column with nowhere to
put the answer. channels.subject is the fallback home: read everywhere as
COALESCE(profile.subject, channels.subject), written by the subject picker on
those pages and by the /api/subjects/backfill derivation.

Safe to re-run.
"""
from db_init import get_conn


def run():
    conn = get_conn()
    have = {r["name"] for r in conn.execute("PRAGMA table_info(channels)")}
    if "subject" not in have:
        conn.execute("ALTER TABLE channels ADD COLUMN subject TEXT")
        print("[migrate_add_channel_subject] + channels.subject")
    conn.commit()
    conn.close()
    print("[migrate_add_channel_subject] ready.")


if __name__ == "__main__":
    run()
