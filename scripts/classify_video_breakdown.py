"""
classify_video_breakdown.py

The qualitative counterpart to classify_template.py's structural/rhetorical
rubric — the same way classify_book_template.py adds a chapter/section
breakdown (topics, points, terms, examples) on top of book_attributes' rubric.

Two modes, because short-form and long-form video need different shapes:

- Short-form (Instagram, under ~3 min): no chapters — the breakdown hangs
  directly off the video (1-4 points, a handful of terms/examples). This is
  the original/default behavior, unchanged.
- Long-form (YouTube, 15+ min): gets the same chapter/section treatment as
  books (video_sections, mirroring book_sections), since a 30-40 minute
  video can easily cover 6-10 distinct sub-topics that a single flat list
  would flatten. Use --longform to switch.

This file has three parts per mode:

1. VIDEO_BREAKDOWN_PROMPT / LONGFORM_VIDEO_BREAKDOWN_PROMPT — the fixed
   prompt sent per-video to Claude so every breakdown is produced
   consistently.

2. export_for_breakdown() / export_for_longform_breakdown() — pulls a
   channel's videos (or a specific one) into the prompt. Long-form defaults
   to a much smaller batch limit — scripts run 4,000-6,000+ words vs.
   short-form's ~250, so batching 30 of them into one prompt is impractical.

3. merge_video_breakdown() / merge_longform_video_breakdown() — take the
   JSON Claude returns and write it into videos.summary plus either the
   flat video_points/video_terms/video_examples (short-form) or
   video_sections + the same three tables scoped by section_id (long-form).

Usage:
    python3 classify_video_breakdown.py --export --channel versobooks
    # paste the batch into a chat with Claude, save the JSON reply
    python3 classify_video_breakdown.py --load results.json

    python3 classify_video_breakdown.py --longform --export --channel some_yt_channel --limit 2
    python3 classify_video_breakdown.py --longform --load results.json
"""
import argparse
import json

from db_init import get_conn


VIDEO_BREAKDOWN_PROMPT = """You are extracting the content breakdown of a short-form PPE
(philosophy/politics/economics) explainer video script — the same way a book gets broken
into chapters, key points, terms, and examples in this engine, just scoped to one short
video instead of a whole book. For EACH video below, return one JSON object with exactly
these keys. Do not add commentary outside the JSON array.

- video_id: integer, matching the id given
- summary: string — 1 plain-language sentence describing what the video covers
- points: array of strings — the key claims/arguments the video actually makes, in its
  own logic (usually 1-4 for a short-form script; don't pad this out)
- terms: array of {{"term": string, "definition": string}} — any named concept, bias,
  law, or piece of jargon the video introduces or relies on (empty array if none)
- examples: array of {{"example_title": string (3-7 words, Instagram-title-style),
  "example_text": string (the example/case/anecdote written up in plain language),
  "reinforces_point": string (which point above it supports)}} — any specific named
  person, event, study, or case the video uses to illustrate a point (empty array if
  the video is purely abstract/argumentative with no concrete example)

Return a JSON array, one object per video.

VIDEOS:
{videos_block}
"""


def build_prompt(rows):
    blocks = []
    for r in rows:
        blocks.append(f'--- video_id: {r["video_id"]} ---\nTitle: {r["title"]}\nScript:\n{r["script"]}\n')
    return VIDEO_BREAKDOWN_PROMPT.format(videos_block="\n".join(blocks))


def export_for_breakdown(channel_name=None, video_id=None, limit=30):
    conn = get_conn()
    q = "SELECT v.video_id, v.title, v.script FROM videos v LEFT JOIN channels c ON c.channel_id = v.channel_id WHERE 1=1"
    params = []
    if channel_name:
        q += " AND c.channel_name = ?"
        params.append(channel_name)
    if video_id:
        q += " AND v.video_id = ?"
        params.append(video_id)
    q += " ORDER BY v.video_id LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    if not rows:
        print("No videos found for that filter.")
        return None
    prompt = build_prompt(rows)
    print(prompt)
    return prompt


def merge_video_breakdown(json_path):
    with open(json_path) as f:
        results = json.load(f)

    conn = get_conn()
    n_videos = n_points = n_terms = n_examples = 0
    for r in results:
        video_id = r["video_id"]

        if r.get("summary"):
            conn.execute("UPDATE videos SET summary = ? WHERE video_id = ?", (r["summary"], video_id))

        # clear any previous breakdown for this video before re-inserting, so
        # re-running on the same video doesn't duplicate rows
        conn.execute("DELETE FROM video_points WHERE video_id = ?", (video_id,))
        conn.execute("DELETE FROM video_terms WHERE video_id = ?", (video_id,))
        conn.execute("DELETE FROM video_examples WHERE video_id = ?", (video_id,))

        for point_text in r.get("points", []):
            conn.execute("INSERT INTO video_points (video_id, point_text) VALUES (?, ?)", (video_id, point_text))
            n_points += 1

        for term in r.get("terms", []):
            conn.execute(
                "INSERT INTO video_terms (video_id, term, definition) VALUES (?, ?, ?)",
                (video_id, term.get("term"), term.get("definition")),
            )
            n_terms += 1

        for ex in r.get("examples", []):
            conn.execute(
                "INSERT INTO video_examples (video_id, example_title, example_text, reinforces_point) "
                "VALUES (?, ?, ?, ?)",
                (video_id, ex.get("example_title"), ex.get("example_text"), ex.get("reinforces_point")),
            )
            n_examples += 1

        n_videos += 1

    conn.commit()
    conn.close()
    print(f"Merged breakdown for {n_videos} video(s): {n_points} point(s), {n_terms} term(s), {n_examples} example(s).")


# ============================================================
# Long-form (YouTube) mode — chapter-aware, mirrors classify_book_template.py
# ============================================================

LONGFORM_VIDEO_BREAKDOWN_PROMPT = """You are extracting the content breakdown of a long-form PPE
(philosophy/politics/economics) YouTube video script — the same way a book gets broken into
chapters, key points, terms, and examples in this engine. For EACH video below, return one JSON
object with exactly these keys. Do not add commentary outside the JSON array.

- video_id: integer, matching the id given
- summary: string — 1-2 plain-language sentences describing what the video covers
- sections: array of objects, one per distinct chapter/segment of the video, IN ORDER (use
  YouTube's own chapter markers if given; otherwise infer natural segment breaks from the
  content itself), each with:
  - section_number: integer — order within the video (1, 2, 3, ...)
  - section_title: string — the segment's title/heading (from chapter markers if given,
    otherwise a short label you write yourself)
  - summary: string — a 1-2 sentence plain-language soundbite of what this segment covers
  - topics: array of strings — short topic tags for this segment
  - points: array of strings — the key claims/arguments made in this segment, in the video's
    own logic. Pull out every distinct point, not just the headline one.
  - terms: array of {{"term": string, "definition": string}} — any named concept, bias, law, or
    piece of jargon this segment introduces or relies on (empty array if none)
  - examples: array of {{"example_title": string (3-7 words, Instagram-title-style),
    "example_text": string (the example/case/anecdote written up in plain language),
    "reinforces_point": string (which point above it supports)}} — any specific named person,
    event, study, or case this segment uses to illustrate a point (empty array if none)

Return a JSON array, one object per video.

VIDEOS:
{videos_block}
"""


def build_longform_prompt(rows):
    blocks = []
    for r in rows:
        blocks.append(f'--- video_id: {r["video_id"]} ---\nTitle: {r["title"]}\nScript:\n{r["script"]}\n')
    return LONGFORM_VIDEO_BREAKDOWN_PROMPT.format(videos_block="\n".join(blocks))


def export_for_longform_breakdown(channel_name=None, video_id=None, limit=2):
    """limit defaults to 2, not video-breakdown's 30 — long-form transcripts
    run 4,000-6,000+ words each, so batching many into one prompt is
    impractical."""
    conn = get_conn()
    q = "SELECT v.video_id, v.title, v.script FROM videos v LEFT JOIN channels c ON c.channel_id = v.channel_id WHERE 1=1"
    params = []
    if channel_name:
        q += " AND c.channel_name = ?"
        params.append(channel_name)
    if video_id:
        q += " AND v.video_id = ?"
        params.append(video_id)
    q += " ORDER BY v.video_id LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close()
    if not rows:
        print("No videos found for that filter.")
        return None
    prompt = build_longform_prompt(rows)
    print(prompt)
    return prompt


def merge_longform_video_breakdown(json_path):
    with open(json_path) as f:
        results = json.load(f)
    if isinstance(results, dict):
        results = [results]

    conn = get_conn()
    n_videos = n_sections = n_points = n_terms = n_examples = 0
    for r in results:
        video_id = r["video_id"]

        if r.get("summary"):
            conn.execute("UPDATE videos SET summary = ? WHERE video_id = ?", (r["summary"], video_id))

        # clear any previous breakdown for this video before re-inserting, so
        # re-running on the same video doesn't duplicate rows
        conn.execute("DELETE FROM video_points WHERE video_id = ?", (video_id,))
        conn.execute("DELETE FROM video_terms WHERE video_id = ?", (video_id,))
        conn.execute("DELETE FROM video_examples WHERE video_id = ?", (video_id,))
        conn.execute("DELETE FROM video_sections WHERE video_id = ?", (video_id,))

        for sec in r.get("sections", []):
            cur = conn.execute(
                "INSERT INTO video_sections (video_id, section_number, section_title, summary, topics) "
                "VALUES (?, ?, ?, ?, ?)",
                (video_id, sec.get("section_number"), sec.get("section_title"), sec.get("summary"),
                 ", ".join(sec.get("topics", [])) if sec.get("topics") else None),
            )
            section_id = cur.lastrowid
            n_sections += 1

            for point_text in sec.get("points", []):
                conn.execute(
                    "INSERT INTO video_points (video_id, section_id, point_text) VALUES (?, ?, ?)",
                    (video_id, section_id, point_text),
                )
                n_points += 1

            for term in sec.get("terms", []):
                conn.execute(
                    "INSERT INTO video_terms (video_id, section_id, term, definition) VALUES (?, ?, ?, ?)",
                    (video_id, section_id, term.get("term"), term.get("definition")),
                )
                n_terms += 1

            for ex in sec.get("examples", []):
                conn.execute(
                    "INSERT INTO video_examples (video_id, section_id, example_title, example_text, reinforces_point) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (video_id, section_id, ex.get("example_title"), ex.get("example_text"), ex.get("reinforces_point")),
                )
                n_examples += 1

        n_videos += 1

    conn.commit()
    conn.close()
    print(f"Merged long-form breakdown for {n_videos} video(s): {n_sections} section(s), "
          f"{n_points} point(s), {n_terms} term(s), {n_examples} example(s).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", action="store_true", help="print the prompt for a channel's videos")
    ap.add_argument("--channel", default=None)
    ap.add_argument("--video_id", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--load", default=None, help="path to a JSON file of Claude's breakdown results")
    ap.add_argument("--longform", action="store_true", help="use the chapter-aware YouTube mode")
    args = ap.parse_args()

    if args.longform:
        limit = args.limit if args.limit is not None else 2
        if args.export:
            export_for_longform_breakdown(args.channel, args.video_id, limit)
        elif args.load:
            merge_longform_video_breakdown(args.load)
        else:
            print("Use --longform --export --channel <name> to get a prompt, or --longform --load results.json to merge results.")
    else:
        limit = args.limit if args.limit is not None else 30
        if args.export:
            export_for_breakdown(args.channel, args.video_id, limit)
        elif args.load:
            merge_video_breakdown(args.load)
        else:
            print("Use --export --channel <name> to get a prompt, or --load results.json to merge results.")
