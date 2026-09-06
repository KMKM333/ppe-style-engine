"""
analyse_visual_style.py — derive an account's visual identity from its own
stored frames.

The PS profile says how an account is cut; the PVS profile says how its words
and pictures relate. Neither says what the pictures LOOK like, so a spec could
call for an "illustration panel" without saying how it should be drawn — and
nothing downstream could reproduce the account's look.

One vision call over a sample of that account's frames, spread across
different videos rather than taken from one, so the brief describes the
account's consistent identity and not a single video's scene. Frames are
already 512px JPEG on disk (see webapp._shot_frame_file), which is the size
the format classifier proved is enough to read composition and text.

Usage:
    python3 analyse_visual_style.py --channel_id 72
    python3 analyse_visual_style.py --channel_id 72 --max_frames 12
"""
import argparse
import json

import llm_client
import re
from db_init import get_conn, PRODUCTION_SPEC_SHOTS_DIR, FORMAT_FRAMES_DIR

MAX_FRAMES = 12          # enough to see the identity; small enough to stay well inside the payload cap
MAX_TOKENS = 2000

PROMPT = """These frames are all from the SAME short-form video account. Describe the visual identity they share, so someone could make a new panel that belongs with them.

Describe only what is actually visible across several frames. If the frames disagree, say so rather than picking one. Do not invent a house style that isn't there.

Return ONLY a JSON object shaped exactly like this, no other text:
{
  "palette": ["#rrggbb", "..."],
  "art_style": "how the pictures are made — photographic, flat vector, 3D render, hand-drawn, screen-recording, archival, collage...",
  "composition": "framing, where the subject sits, crops, margins, aspect handling, use of empty space",
  "typography": "on-screen text: weight, case, placement, size relative to frame, background plates, animation if visible; 'none visible' if there is none",
  "subject_treatment": "what is actually pictured — people, objects, diagrams, screenshots, archival footage — and how they are handled",
  "motion": "what movement is visible between frames — static panels, slow push, whip cuts, camera moves; 'cannot tell from stills' is a valid answer",
  "avoid": "what would immediately read as a DIFFERENT account",
  "image_prompt_template": "a reusable brief for generating one new panel in this style, with {subject} as the placeholder for what the panel should show"
}"""


def _norm(x):
    """Same loose match get_or_create_format_profile uses, so an account
    resolves here exactly as it does there.

    Unescape first: a name scraped from a page keeps its HTML entities, and
    "Barry&#39;s Economics" normalises to barry39seconomics — matching nothing,
    silently, which is how an account with 35 classified inputs came back with
    no frames and no error."""
    import html as _html
    return re.sub(r"[^a-z0-9]", "", _html.unescape(x or "").lower())


def sample_format_frames(conn, channel_id, max_frames=MAX_FRAMES):
    """Fallback source: the P+S frames.

    Shot analysis is the richer source, but an account can be fully analysed
    for P+S and never shot-analysed — which is the normal state for the
    long-form accounts, whose videos arrive as 70-second segments sampled at
    1s. Those frames are already stored and already paid for; refusing to
    look at them and calling the account unanalysable wastes them and leaves
    the renderer drawing generic panels.

    Promotional segments are skipped: their pictures are pricing tables and
    product shots, which is exactly not the look we are describing.
    """
    # format_profiles.channel_id is usually NULL — get_or_create_format_profile
    # resolves an account by name and never writes the link — so joining on it
    # finds nothing. Match the way that function does: on the normalised name.
    ch = conn.execute("SELECT channel_name FROM channels WHERE channel_id = ?",
                      (channel_id,)).fetchone()
    target = _norm(ch["channel_name"]) if ch else ""
    profile_ids = [r["format_profile_id"] for r in conn.execute(
        "SELECT format_profile_id, channel_id, handle, display_name FROM format_profiles")
        if r["channel_id"] == channel_id
        or (target and target in (_norm(r["handle"]), _norm(r["display_name"])))]
    if not profile_ids:
        return []
    ph = ",".join("?" * len(profile_ids))
    inputs = [r["format_input_id"] for r in conn.execute(
        f"""SELECT format_input_id FROM format_inputs
            WHERE format_profile_id IN ({ph}) AND status = 'classified'
              AND COALESCE(excluded, 0) = 0
            ORDER BY ingested_at DESC""", profile_ids)]
    if not inputs:
        return []
    per_input = max(1, max_frames // max(1, min(len(inputs), max_frames)))
    picked = []
    for input_id in inputs:
        frame_dir = FORMAT_FRAMES_DIR / str(input_id)
        if not frame_dir.is_dir():
            continue
        rows = conn.execute(
            "SELECT frame_id FROM format_input_frames WHERE format_input_id = ? AND captured = 1 "
            "ORDER BY frame_number", (input_id,)).fetchall()
        if not rows:
            continue
        step = max(1, len(rows) // (per_input + 1))
        for r in rows[step::step][:per_input]:
            # Frames were PNG before the disk filled and JPEG after, and both
            # still sit on disk — look for either rather than silently finding
            # nothing for the older inputs.
            for ext, mime in (("jpg", "image/jpeg"), ("png", "image/png")):
                f = frame_dir / f"frame_{r['frame_id']}.{ext}"
                if f.is_file():
                    picked.append((mime, f.read_bytes()))
                    break
            if len(picked) >= max_frames:
                return picked
    return picked


def sample_frames(conn, channel_id, max_frames=MAX_FRAMES):
    """Frames spread across the channel's inputs, newest input first, taking a
    few from each rather than many from one — a single video's frames describe
    that video, not the account."""
    inputs = [r["input_id"] for r in conn.execute(
        "SELECT input_id FROM production_spec_inputs WHERE channel_id = ? ORDER BY ingested_at DESC",
        (channel_id,))]
    if not inputs:
        return []
    per_input = max(1, max_frames // max(1, min(len(inputs), max_frames)))
    picked = []
    for input_id in inputs:
        shot_dir = PRODUCTION_SPEC_SHOTS_DIR / str(input_id)
        if not shot_dir.is_dir():
            continue
        rows = conn.execute(
            "SELECT shot_id FROM production_spec_shots WHERE input_id = ? AND frame_captured = 1 "
            "ORDER BY shot_number", (input_id,)).fetchall()
        if not rows:
            continue
        # Spread within the video too: the opening frame is often a title card.
        step = max(1, len(rows) // (per_input + 1))
        for r in rows[step::step][:per_input]:
            for ext, mime in (("jpg", "image/jpeg"), ("png", "image/png")):
                f = shot_dir / f"shot_{r['shot_id']}.{ext}"
                if f.is_file():
                    picked.append((mime, f.read_bytes()))
                    break
            if len(picked) >= max_frames:
                return picked
    return picked


def analyse(channel_id, max_frames=MAX_FRAMES):
    conn = get_conn()
    ch = conn.execute("SELECT channel_name FROM channels WHERE channel_id = ?", (channel_id,)).fetchone()
    if not ch:
        conn.close()
        raise ValueError(f"no channel with id {channel_id}")
    images = sample_frames(conn, channel_id, max_frames)
    source = "shot analysis"
    if not images:
        images = sample_format_frames(conn, channel_id, max_frames)
        source = "P+S frames"
    if not images:
        conn.close()
        raise ValueError(f"no captured frames for '{ch['channel_name']}' — "
                         f"shot-analyse or P+S-analyse some videos first")
    print(f"[analyse_visual_style] {ch['channel_name']}: {len(images)} frames from {source}")

    data = llm_client.generate_json_with_images(PROMPT, images, max_tokens=MAX_TOKENS)
    palette = data.get("palette") or []
    conn.execute(
        """INSERT INTO visual_style_briefs
           (channel_id, palette_json, art_style, composition, typography, subject_treatment,
            motion, avoid, image_prompt_template, n_frames_analysed, analysed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(channel_id) DO UPDATE SET
             palette_json=excluded.palette_json, art_style=excluded.art_style,
             composition=excluded.composition, typography=excluded.typography,
             subject_treatment=excluded.subject_treatment, motion=excluded.motion,
             avoid=excluded.avoid, image_prompt_template=excluded.image_prompt_template,
             n_frames_analysed=excluded.n_frames_analysed, analysed_at=datetime('now')""",
        (channel_id, json.dumps(palette), data.get("art_style"), data.get("composition"),
         data.get("typography"), data.get("subject_treatment"), data.get("motion"),
         data.get("avoid"), data.get("image_prompt_template"), len(images)),
    )
    conn.commit()
    conn.close()
    print(f"[analyse_visual_style] {ch['channel_name']}: brief written from {len(images)} frame(s).")
    return data


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel_id", type=int, required=True)
    ap.add_argument("--max_frames", type=int, default=MAX_FRAMES)
    args = ap.parse_args()
    analyse(args.channel_id, args.max_frames)
