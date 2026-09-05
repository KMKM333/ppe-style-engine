"""
derive_section_timing.py — work out where each chapter starts.

Two methods, tried in that order, and the one used is recorded:

  matched   — the chapter's own words located in the cue-level transcript.
              Sections were summarised FROM that transcript in order, so a
              distinctive phrase from a section's summary or first point
              usually appears verbatim in a cue. Trustworthy when it scores.

  allocated — the duration split by each chapter's share of the words. Never
              fails, and on an evenly-paced video lands within a few seconds,
              but it is an estimate and is labelled as one.

Recording which was used matters: a matched boundary can be relied on for
frame sampling, an allocated one wants checking before anything is cut to it.

Usage:
    python3 derive_section_timing.py --video_id 42
    python3 derive_section_timing.py --all
"""
import argparse
import json
import re

from db_init import get_conn

STOP = {"the", "and", "that", "this", "with", "from", "have", "which", "their", "they",
        "what", "when", "where", "were", "been", "into", "about", "these", "those",
        "there", "would", "could", "should", "than", "them", "then", "some", "more"}


def _words(text):
    return [w for w in re.findall(r"[a-z']{4,}", (text or "").lower()) if w not in STOP]


def _match_start(cues, phrases, after_sec):
    """Best cue at or after after_sec whose text shares distinctive words with
    the chapter. Returns (start_sec, confidence) or (None, 0)."""
    want = set()
    for p in phrases:
        want |= set(_words(p)[:14])
    if not want:
        return None, 0.0
    best, best_score = None, 0.0
    for c in cues:
        t = c.get("start")
        if t is None or t < after_sec:
            continue
        got = set(_words(c.get("text")))
        if not got:
            continue
        score = len(want & got) / min(len(want), 8)
        if score > best_score:
            best, best_score = t, score
    return (best, round(min(best_score, 1.0), 2)) if best_score >= 0.5 else (None, round(best_score, 2))


def derive(video_id, conn=None):
    own = conn is None
    conn = conn or get_conn()
    v = conn.execute("SELECT title, duration_sec, timed_transcript_json FROM videos WHERE video_id = ?",
                     (video_id,)).fetchone()
    if not v:
        if own:
            conn.close()
        return {"video_id": video_id, "error": "no such video"}
    sections = conn.execute(
        "SELECT section_id, section_number, section_title, summary FROM video_sections "
        "WHERE video_id = ? ORDER BY section_number, section_id", (video_id,)).fetchall()
    if not sections:
        if own:
            conn.close()
        return {"video_id": video_id, "sections": 0}

    duration = v["duration_sec"] or 0
    cues = []
    if v["timed_transcript_json"]:
        try:
            cues = json.loads(v["timed_transcript_json"]) or []
        except json.JSONDecodeError:
            cues = []
    if cues and not duration:
        duration = max((c.get("start") or 0) for c in cues) + 5

    # weights for the allocated fallback: a chapter with more to say occupies
    # more of the video than one with a single line.
    points = {s["section_id"]: [r["point_text"] for r in conn.execute(
        "SELECT point_text FROM video_points WHERE section_id = ?", (s["section_id"],))]
        for s in sections}
    weights = [max(1, len(_words(s["summary"])) + sum(len(_words(p)) for p in points[s["section_id"]]))
               for s in sections]
    total_w = sum(weights) or 1

    starts, methods, confs = [], [], []
    cursor = 0.0
    for i, s in enumerate(sections):
        if i == 0:
            starts.append(0.0); methods.append("matched" if cues else "allocated"); confs.append(1.0)
            cursor = 0.0
            continue
        st, conf = (None, 0.0)
        if cues:
            st, conf = _match_start(cues, [s["section_title"], s["summary"]] + points[s["section_id"]][:2],
                                    cursor + 1.0)
        if st is None:
            st = round(duration * sum(weights[:i]) / total_w, 2)
            methods.append("allocated")
        else:
            methods.append("matched")
        # Boundaries must move forward. A match that goes backwards is wrong
        # however well it scored, so it is treated as a failure, not obeyed.
        if st <= cursor:
            st = round(cursor + max(1.0, duration / (len(sections) * 4)), 2)
            methods[-1] = "allocated"
            conf = 0.0
        starts.append(round(st, 2)); confs.append(conf); cursor = st

    ends = starts[1:] + [round(duration, 2)]
    for s, st, en, m, cf in zip(sections, starts, ends, methods, confs):
        conn.execute(
            "UPDATE video_sections SET start_sec = ?, end_sec = ?, timing_method = ?, "
            "timing_confidence = ? WHERE section_id = ?", (st, en, m, cf, s["section_id"]))
    conn.commit()
    out = {"video_id": video_id, "title": v["title"], "duration_sec": duration,
           "sections": len(sections), "matched": methods.count("matched"),
           "allocated": methods.count("allocated"),
           "boundaries": [{"n": s["section_number"], "start": st, "end": en, "method": m,
                           "confidence": cf, "title": s["section_title"]}
                          for s, st, en, m, cf in zip(sections, starts, ends, methods, confs)]}
    if own:
        conn.close()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--video_id", type=int)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    conn = get_conn()
    if args.all:
        ids = [r["video_id"] for r in conn.execute(
            "SELECT DISTINCT video_id FROM video_sections ORDER BY video_id")]
        tot_m = tot_a = 0
        for vid in ids:
            r = derive(vid, conn)
            tot_m += r.get("matched", 0); tot_a += r.get("allocated", 0)
            print(f"  video {vid}: {r.get('sections', 0)} chapters, "
                  f"{r.get('matched', 0)} matched / {r.get('allocated', 0)} allocated")
        print(f"\n{len(ids)} video(s): {tot_m} boundaries matched, {tot_a} allocated")
    elif args.video_id:
        print(json.dumps(derive(args.video_id, conn), indent=1))
    else:
        raise SystemExit("--video_id or --all required")
    conn.close()
