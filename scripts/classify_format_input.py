"""
classify_format_input.py — the Production Inputs (P+S) classifier: reads a
video's FRAMES and its TRANSCRIPT in a single call and answers how the two
relate.

Why one call rather than two: every axis here is relational. "Do the visuals
carry the joke or illustrate the script?" cannot be answered from frames
alone or transcript alone — neither contains the relationship. Two separate
passes stitched together would each be answering a question they can't see
the other half of, so the joint call isn't an optimisation, it's the whole
mechanism.

This is also the only route for a silent format. An illustrated-comic
account has no audio, so transcription returns nothing and the library
pipeline can build no profile at all — but the words are drawn INTO the
panels. The vision pass reads them out, which recovers a "script" where
transcription found silence. Hence on_screen_text below: for those accounts
it IS the script.

Frames arrive downscaled AND JPEG-encoded from the transcriber. That is
deliberate: format analysis needs composition, layout and
legible text, not fine detail, and full-size frames make the payload large
enough that the API answers with an EMPTY BODY rather than a size error —
the failure that broke the production-spec pipeline until batches were
capped by bytes. Measured on real frames: PNG at this width is ~445KB each
(10 = 4.34MB, still over the cap), JPEG ~74KB (10 = 0.72MB). So the whole
video's frames fit in ONE call, which is exactly what the relational
analysis requires.

Runs as a detached subprocess launched by webapp.py's
/api/format/inputs/<id>/classify route.

Usage:
    python3 classify_format_input.py --format_input_id 1
"""
import argparse
import json
import traceback

import llm_client
from db_init import get_conn, FORMAT_FRAMES_DIR

MAX_TOKENS = 4096
MAX_ATTEMPTS = 3          # empty replies happen; see classify_production_spec_shots
RETRY_BACKOFF_SEC = 5

# Must stay in step with FORMAT_AXES in webapp.py / migrate_add_format_profiles.py.
AXES = {
    "verbal_channel": ["Spoken monologue", "On-screen text", "Borrowed audio", "Caption only", "Mixed"],
    "verbal_authorship": ["Original", "Borrowed", "Hybrid"],
    "visual_role": ["Illustrate the script", "Carry meaning alone", "Decorative", "Are the content"],
    "audio_role": ["Primary", "Borrowed hook", "Ambient", "Silent"],
    "coupling": ["Beat-synced", "Loose", "Independent"],
}

PROMPT = """You are analysing ONE short-form video to describe its FORMAT — specifically how its
visuals and its words relate to each other. You are given frames sampled evenly across the
video, in order, plus whatever spoken transcript exists.

{transcript_block}

Answer strictly about the relationship between picture and language. You are NOT summarising
the topic, and NOT judging quality.

Return a raw JSON object with exactly these keys:

"on_screen_text": string — any words you can READ in the frames (captions, titles, lettering,
  subtitles), joined with " / ". Empty string if none. READ THESE CAREFULLY IN EVERY VIDEO, not
  only silent ones: in this kind of short-form content the script usually lives in the burned-in
  subtitles, and the audio is often secondary. Transcribe what you can actually read rather than
  describing it.

"text_audio_relation": one of "same" / "differs" / "no_audio" / "no_on_screen_text" — how the
  on-screen words relate to the spoken transcript. This is the single most useful cross-check
  you can give, so weigh it before answering the axes below:
    - "same": the captions are a transcription of the speech (burned-in subtitles of their own
      script). Strong evidence for Original authorship.
    - "differs": the words on screen are NOT what is being said. That usually means the audio is
      borrowed (a meme/song/clip) while the on-screen writing is the creator's own — strong
      evidence for Hybrid or Borrowed authorship. Say what differs in the note.
    - "no_audio" / "no_on_screen_text": one side is absent.
  When the two sources disagree about what the "script" is, trust the ON-SCREEN text as the
  creator's own writing unless there is clear evidence otherwise.

"readings": an object with exactly these five keys. Each value is an object
  {{"value": <one of the allowed values>, "note": <one sentence of evidence from what you saw>}}.

- verbal_channel — where the words live. One of: {verbal_channel}
  (If the script is carried by burned-in subtitles that mirror the speech, that is still
   "Spoken monologue" — the words originate as speech. Choose "On-screen text" when the writing
   is the primary carrier and the audio is absent, incidental or borrowed. Choose "Mixed" when
   both genuinely carry different parts of the meaning.)
- verbal_authorship — who wrote the words. One of: {verbal_authorship}
  (Choose "Borrowed" or "Hybrid" if the audio is a recognisable pre-existing meme/song/clip
   rather than the creator speaking their own script. If the spoken audio is borrowed but the
   on-screen titles are the creator's own, that is "Hybrid". text_audio_relation="differs" is
   your strongest clue here — captions that don't match the speech usually mean exactly this.)
- visual_role — what the visuals do. One of: {visual_role}
  ("Are the content" means the images are not illustrating a script that exists separately —
   they ARE the piece, as in a drawn comic. "Carry meaning alone" means the point lands in the
   image and would be lost without it. "Decorative" means removing them would cost little.)
- audio_role — the role of sound. One of: {audio_role}
  (If there is no transcript AND the frames look like a silent comic/cartoon, answer "Silent".)
- coupling — how tightly visuals track the words. One of: {coupling}
  ("Beat-synced" = cuts/panels land with the words. "Independent" = visuals don't follow the
   argument at all.)

Use ONLY the allowed values, copied exactly. Base every note on something actually visible in
the frames or present in the transcript — if the evidence is weak, say so in the note rather
than inventing it.

Return ONLY the JSON object, no commentary, no markdown fence."""


def _mark(input_id, status, error=None):
    conn = get_conn()
    conn.execute(
        "UPDATE format_inputs SET status = ?, classification_error = ? WHERE format_input_id = ?",
        (status, error, input_id),
    )
    conn.commit()
    conn.close()
    if error:
        print(f"[classify_format_input] format_input_id={input_id} {status}: {error}")


def build_prompt(transcript):
    if transcript and transcript.strip():
        block = ("TRANSCRIPT (the spoken audio):\n---\n" + transcript.strip()[:8000] + "\n---")
    else:
        # Absence of a transcript is itself evidence, not a gap to apologise
        # for — say so plainly so the model reads the frames for the words
        # instead of assuming the audio simply failed to attach.
        block = ("TRANSCRIPT: none — this video has no spoken audio, or none was captured. "
                 "Treat that as meaningful rather than as a gap: in this kind of content the "
                 "script usually lives in the burned-in subtitles, so read the words out of the "
                 "frames and treat those as the script.")
    return PROMPT.format(transcript_block=block, **{k: " / ".join(v) for k, v in AXES.items()})


def classify(format_input_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM format_inputs WHERE format_input_id = ?", (format_input_id,)
    ).fetchone()
    if not row:
        conn.close()
        print(f"[classify_format_input] format_input_id={format_input_id} not found")
        return
    frames = conn.execute(
        "SELECT frame_id, frame_number FROM format_input_frames WHERE format_input_id = ? "
        "AND captured = 1 ORDER BY frame_number",
        (format_input_id,),
    ).fetchall()
    conn.close()

    if not frames:
        _mark(format_input_id, "needs_review", "No frames captured for this input.")
        return

    frame_dir = FORMAT_FRAMES_DIR / str(format_input_id)
    images = []
    for f in frames:
        path = frame_dir / f"frame_{f['frame_id']}.jpg"
        if not path.is_file():
            _mark(format_input_id, "needs_review", f"Missing frame file for frame_id={f['frame_id']}.")
            return
        images.append(("image/jpeg", path.read_bytes()))

    prompt = build_prompt(row["transcript"])
    result, last_error = None, None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = llm_client.generate_json_with_images(prompt, images, max_tokens=MAX_TOKENS)
        except Exception as e:  # noqa: BLE001
            last_error, result = e, None
        if isinstance(result, dict) and isinstance(result.get("readings"), dict):
            break
        if result is not None:
            last_error = f"expected an object with 'readings', got {type(result).__name__}"
            result = None
        if attempt < MAX_ATTEMPTS:
            import time
            print(f"[classify_format_input] attempt {attempt}/{MAX_ATTEMPTS} failed ({last_error}); retrying")
            time.sleep(RETRY_BACKOFF_SEC * attempt)

    if not isinstance(result, dict) or not isinstance(result.get("readings"), dict):
        _mark(format_input_id, "needs_review",
              f"Anthropic API call failed after {MAX_ATTEMPTS} attempts: {last_error}")
        traceback.print_exc()
        return

    readings = result["readings"]
    conn = get_conn()
    stored = 0
    for axis, allowed in AXES.items():
        entry = readings.get(axis)
        if not isinstance(entry, dict):
            continue
        value = entry.get("value")
        if value not in allowed:
            # Don't quietly coerce an unexpected value into a real one — a
            # wrong-but-plausible reading is worse than a missing one, since
            # it feeds the aggregate and looks measured.
            print(f"[classify_format_input] axis {axis}: '{value}' not in vocabulary, skipping")
            continue
        conn.execute(
            "INSERT OR REPLACE INTO format_input_readings (format_input_id, axis, value, note) "
            "VALUES (?, ?, ?, ?)",
            (format_input_id, axis, value, (entry.get("note") or "").strip()),
        )
        stored += 1
    relation = (result.get("text_audio_relation") or "").strip().lower()
    if relation not in ("same", "differs", "no_audio", "no_on_screen_text"):
        relation = None   # same rule as the axes: don't invent a value
    conn.execute(
        "UPDATE format_inputs SET on_screen_text = ?, text_audio_relation = ?, status = 'classified', "
        "classification_error = NULL WHERE format_input_id = ?",
        ((result.get("on_screen_text") or "").strip(), relation, format_input_id),
    )
    conn.commit()
    profile_id = row["format_profile_id"]
    conn.close()

    if stored == 0:
        _mark(format_input_id, "needs_review", "No axis reading matched the allowed vocabulary.")
        return

    print(f"[classify_format_input] format_input_id={format_input_id} classified ({stored}/{len(AXES)} axes).")
    if profile_id:
        aggregate_profile(profile_id)


def aggregate_profile(format_profile_id):
    """Rolls every classified input's readings up into the profile: per axis,
    the most common value wins, and the note records the split so a mixed
    account reads as mixed rather than as whichever value happened to lead.

    Only overwrites axes that have at least one classified reading, so a
    preliminary value survives until something real replaces it."""
    conn = get_conn()
    inputs = [r["format_input_id"] for r in conn.execute(
        "SELECT format_input_id FROM format_inputs WHERE format_profile_id = ? AND status = 'classified'",
        (format_profile_id,),
    )]
    if not inputs:
        conn.close()
        return

    placeholders = ",".join("?" * len(inputs))
    for axis in AXES:
        rows = conn.execute(
            f"SELECT value FROM format_input_readings WHERE axis = ? AND format_input_id IN ({placeholders})",
            [axis] + inputs,
        ).fetchall()
        if not rows:
            continue
        counts = {}
        for r in rows:
            counts[r["value"]] = counts.get(r["value"], 0) + 1
        total = sum(counts.values())
        top, n = max(counts.items(), key=lambda kv: kv[1])
        if len(counts) == 1:
            note = f"Consistent across all {total} analysed input(s)."
        else:
            spread = ", ".join(f"{v} x{c}" for v, c in sorted(counts.items(), key=lambda kv: -kv[1]))
            note = f"{n} of {total} inputs. Mixed: {spread}."
        conn.execute(
            "INSERT OR REPLACE INTO format_profile_attributes (format_profile_id, axis, value, note, source) "
            "VALUES (?, ?, ?, ?, 'classified')",
            (format_profile_id, axis, top, note),
        )

    conn.execute(
        "UPDATE format_profiles SET n_inputs_analysed = ?, status = 'confirmed' WHERE format_profile_id = ?",
        (len(inputs), format_profile_id),
    )
    conn.commit()
    conn.close()
    print(f"[classify_format_input] format_profile_id={format_profile_id} aggregated from {len(inputs)} input(s).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--format_input_id", type=int, required=True)
    args = ap.parse_args()
    classify(args.format_input_id)
