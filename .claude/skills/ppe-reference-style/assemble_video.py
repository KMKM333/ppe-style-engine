"""
assemble_video.py — build an actual video file from a production spec.

Runs HERE rather than on the engine because this box is the one with ffmpeg
and the API keys, the same division the frame work already uses: the engine
derives the PLAN (free, inspectable, correctable), this executes it.

Per shot: one image generated from the account's own visual brief, held for
that shot's derived duration. Per beat: one voiceover line, so the audio runs
continuously while the pictures change under it. Then a single ffmpeg concat.

Nothing here impersonates anyone. The format profile says a spoken monologue
is required; the VOICE is whichever TTS voice you choose as your own
(--voice), never a clone of the analysed creator.

Usage:
    python3 assemble_video.py --creation_id 3 --dry-run     # plan + cost, spends nothing
    python3 assemble_video.py --creation_id 3
    python3 assemble_video.py --creation_id 3 --no-audio --size 1024x1792
"""
import argparse, base64, json, os, subprocess, sys, tempfile, urllib.request
from pathlib import Path

LIVE = os.environ.get("PPE_LIVE_URL", "https://ppe-style-engine.onrender.com").rstrip("/")
KEY = os.environ.get("PPE_INGEST_API_KEY", "")
OPENAI_KEY = os.environ.get("TRANSCRIBE_API_KEY", "")          # the key already on this box
OPENAI_BASE = os.environ.get("TRANSCRIBE_API_BASE", "https://api.openai.com/v1").rstrip("/")
IMAGE_MODEL = os.environ.get("ASSEMBLY_IMAGE_MODEL", "gpt-image-1")
TTS_MODEL = os.environ.get("ASSEMBLY_TTS_MODEL", "tts-1")
OUT_DIR = Path(os.environ.get("ASSEMBLY_OUT_DIR", "/root/bulk-transcriber/assembled"))

# Rates per image for gpt-image-1 at a portrait size. The estimator used to
# assume a flat $0.04 — roughly a FIFTH of what the default quality actually
# costs — so a run quoted at $1.95 would have billed about $9, and the first
# attempt drained a $5 balance before finishing. Quality now defaults to the
# cheap tier and the estimate is quoted from the tier actually being used.
IMAGE_USD_BY_QUALITY = {"low": 0.02, "medium": 0.07, "high": 0.19}
TTS_USD_PER_1K_CHARS = 0.015


def engine(path, payload=None):
    req = urllib.request.Request(
        f"{LIVE}{path}", data=json.dumps(payload).encode() if payload is not None else None,
        headers={"X-Ingest-Key": KEY, "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode(errors="replace").replace("\n", " "))


# --- Higgsfield renderer ------------------------------------------------
# Same plan, different hands. The engine's assembly plan already carries a
# subject, a duration and a prompt per shot, so a renderer only has to turn
# those into files — which is why swapping one in is a flag rather than a
# rewrite.
#
# Motion is made from the PANEL WE ALREADY GENERATED, not from a fresh
# text-to-video roll: kling3_0_turbo takes --start-image, so the account's
# measured visual identity survives into the moving clip instead of being
# re-rolled and losing it.
HF_BIN = os.environ.get("HIGGSFIELD_BIN", "higgsfield")
HF_IMAGE_MODEL = os.environ.get("HF_IMAGE_MODEL", "nano_banana_2_lite")
HF_VIDEO_MODEL = os.environ.get("HF_VIDEO_MODEL", "kling3_0_turbo")


def hf(args, timeout=900):
    """Runs the Higgsfield CLI and returns its parsed JSON."""
    out = subprocess.run([HF_BIN] + args + ["--json"], capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:300])
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"unparseable CLI reply: {out.stdout[:200]}")


def _hf_result_url(data):
    """The CLI's result shape varies by model, so look rather than assume."""
    if isinstance(data, dict):
        for k in ("result_url", "url", "output_url"):
            if data.get(k):
                return data[k]
        for k in ("results", "outputs", "media", "data"):
            v = data.get(k)
            if isinstance(v, list) and v:
                got = _hf_result_url(v[0])
                if got:
                    return got
            elif isinstance(v, dict):
                got = _hf_result_url(v)
                if got:
                    return got
    if isinstance(data, str) and data.startswith("http"):
        return data
    return None


def _download(url, out_path):
    with urllib.request.urlopen(url, timeout=600) as r:
        out_path.write_bytes(r.read())
    return out_path


def hf_make_image(prompt, out_path, aspect="9:16"):
    data = hf(["generate", "create", HF_IMAGE_MODEL, "--prompt", prompt,
               "--aspect_ratio", aspect, "--wait"])
    url = _hf_result_url(data)
    if not url:
        raise RuntimeError(f"no image URL in reply: {str(data)[:200]}")
    return _download(url, out_path)


def hf_make_clip(prompt, start_image, seconds, out_path, aspect="9:16"):
    """Animates a panel. Duration is clamped to what the model accepts and
    the real length is trimmed later, so a 2.4s shot does not become 5s of
    video and drift out of sync with its own audio."""
    args = ["generate", "create", HF_VIDEO_MODEL, "--prompt", prompt,
            "--aspect_ratio", aspect, "--duration", str(max(5, int(round(seconds)))),
            "--resolution", "720p", "--wait"]
    if start_image:
        args += ["--start-image", str(start_image)]
    data = hf(args)
    url = _hf_result_url(data)
    if not url:
        raise RuntimeError(f"no video URL in reply: {str(data)[:200]}")
    return _download(url, out_path)


def openai(path, payload, timeout=300, attempts=6):
    """Retries a rate limit or a server error with growing backoff. Image
    generation at 48-shots-per-video reliably trips 429 partway through, and
    stopping there wastes every image already paid for."""
    import time as _t, urllib.error
    for attempt in range(attempts):
        req = urllib.request.Request(
            f"{OPENAI_BASE}{path}", data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 529) or attempt == attempts - 1:
                raise
            # A 429 from the image API is often a per-MINUTE quota, not a
            # momentary spike: after a long generating session it stays on for
            # a while. Backing off in seconds burns every attempt and reports a
            # failure for work that would have succeeded a minute later.
            wait = min(240, 30 * (attempt + 1) ** 2) if e.code == 429 else min(90, 15 * (attempt + 1))
            print(f"    {e.code} — waiting {wait}s", flush=True)
            _t.sleep(wait)
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == attempts - 1:
                raise
            _t.sleep(20)


def make_image(prompt, size, out_path, quality="low"):
    raw = openai("/images/generations",
                 {"model": IMAGE_MODEL, "prompt": prompt, "size": size, "n": 1, "quality": quality})
    data = json.loads(raw)["data"][0]
    if data.get("b64_json"):
        out_path.write_bytes(base64.b64decode(data["b64_json"]))
    else:
        with urllib.request.urlopen(data["url"], timeout=180) as r:
            out_path.write_bytes(r.read())
    return out_path


def _multipart(fields, files):
    """Builds a multipart/form-data body. files = [(field, filename, mime, bytes)]."""
    import uuid as _uuid
    boundary = "----ppeimg" + _uuid.uuid4().hex
    parts = []
    for k, v in fields:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for field, fname, mime, blob in files:
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                      f"filename=\"{fname}\"\r\nContent-Type: {mime}\r\n\r\n").encode() + blob + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def fetch_reference_frames(urls, out_dir):
    """Downloads the account's own frames once and keeps them: they are the
    reference for every panel, and re-fetching them per image is pointless."""
    out_dir.mkdir(parents=True, exist_ok=True)
    got = []
    for i, u in enumerate(urls):
        f = out_dir / f"ref_{i:02d}.jpg"
        if not (f.exists() and f.stat().st_size > 1024):
            try:
                with urllib.request.urlopen(u, timeout=90) as r:
                    f.write_bytes(r.read())
            except Exception as e:
                print(f"    reference frame {i} failed: {e}", flush=True)
                continue
        got.append(f)
    return got


def make_image_ref(prompt, refs, size, out_path, quality="low", fidelity="low"):
    """Generate a panel WITH the account's real frames as visual reference.

    /images/generations only ever sees the words. The edits endpoint takes
    the frames themselves, so the grade, framing and texture come across
    instead of being re-invented from an adjective.
    """
    import time as _t, urllib.error
    # input_fidelity is the knob that decides how hard the model works to keep
    # the reference's own detail. It was never set, so every run so far used
    # "low" by default. Low costs under a cent per reference image; high about
    # five to seven cents each, roughly the price of the panel itself.
    fields = [("model", IMAGE_MODEL), ("prompt", prompt), ("size", size),
              ("quality", quality), ("n", "1"), ("input_fidelity", fidelity)]
    files = [("image[]", f.name, "image/jpeg", f.read_bytes()) for f in refs]
    body, boundary = _multipart(fields, files)
    req = urllib.request.Request(
        f"{OPENAI_BASE}/images/edits", data=body, method="POST",
        headers={"Authorization": f"Bearer {OPENAI_KEY}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = json.loads(r.read().decode())["data"][0]
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:300]
            except Exception:
                pass
            if e.code not in (429, 500, 502, 503, 529) or attempt == 3:
                raise RuntimeError(f"images/edits {e.code}: {detail}") from e
            _t.sleep(min(90, 15 * (attempt + 1)))
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 3:
                raise
            _t.sleep(20)
    if data.get("b64_json"):
        out_path.write_bytes(base64.b64decode(data["b64_json"]))
    else:
        with urllib.request.urlopen(data["url"], timeout=180) as r:
            out_path.write_bytes(r.read())
    return out_path


def strip_caption_instruction(prompt):
    """Removes the "render this exact text" clause from an enriched prompt.

    The clause exists because nothing else was drawing the caption. Once the
    caption is real type on top, asking the image model for it as well gives
    malformed lettering competing with the real thing.
    """
    import re as _re
    out = _re.sub(r"\n*The on-screen caption reads exactly:.*?(?:\n|$)", "\n", prompt,
                  flags=_re.S | _re.I)
    out = _re.sub(r"Render that text and no other text\.?", "", out, flags=_re.I)
    return out.strip() + "\n\nRender NO text, letters or numbers anywhere in the image."


FONT_DISPLAY = Path(__file__).resolve().parent / "fonts" / "Anton.ttf"
FONT_SERIF = Path("/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf")
CAPTION_LOOK = "anton"   # set from --caption-look at startup


def _ff_escape(t):
    r"""drawtext eats :, ', % and \ — escape before they become syntax."""
    return (t.replace("\\", "\\\\").replace(":", "\\:")
             .replace("'", "\u2019").replace("%", "\\%"))


# Both reference channels animate at 6-12 fps, and their own breakdowns say it
# is deliberate: half the frames to make, half the render time, and it reads as
# classic animation rather than as video. We were outputting 30.
TREATED_FPS = int(os.environ.get("ASSEMBLY_FPS", "12"))

# Movement means something. Toward camera = this matters more; away = less;
# right = forward in time; left = back in time. Ours alternated on shot index,
# which is decoration. This reads the shot's own words for a direction and
# falls back to a gentle push on the beat's opening shot.
# Only EXPLICIT time markers. The first version included "was" and "were",
# which appear in almost any narration, so every shot read as travelling back
# in time and all twelve moved the same way. A direction has to be earned.
_PAST = ("history", "historic", "back then", "used to", "century", "decades ago",
         "originally", "ancient", "in the 18", "in the 19", "years ago", "once upon")
_FUTURE = ("today", "nowadays", "the future", "these days", "modern", "eventually",
           "ends up", "leads to", "from now on")


def motion_for(text, first_in_beat, idx=0):
    """Pick the camera move from what the shot means.

    An explicit time marker wins, because direction-as-time is the one piece
    of this grammar that is unambiguous. Everything else opens a beat with a
    push (a new idea arriving matters) and otherwise drifts, alternating the
    drift so a run of shots does not pulse in unison."""
    t = (text or "").lower()
    past = any(k in t for k in _PAST)
    future = any(k in t for k in _FUTURE)
    if past and not future:
        return "left"
    if future and not past:
        return "right"
    if first_in_beat:
        return "in"
    return "drift" if idx % 2 == 0 else "out"


TEXTURE_DIR = Path(__file__).resolve().parent / "textures"

# The output frame. A render style may carry its own aspect — one account works
# in 16:9 landscape while the rest are vertical, and forcing that look into a
# 9:16 frame keeps the palette and loses the composition.
FRAME_W, FRAME_H = 1080, 1620
ASPECTS = {"9:16": (1080, 1920), "3:4": (1080, 1440), "1:1": (1080, 1080),
           "16:9": (1920, 1080), "4:3": (1440, 1080), "default": (1080, 1620)}


def set_frame(aspect):
    global FRAME_W, FRAME_H
    FRAME_W, FRAME_H = ASPECTS.get((aspect or "").strip(), ASPECTS["default"])
    return FRAME_W, FRAME_H


def _wrap_caption(txt, limit=18):
    """Past `limit` characters, break at the space nearest the middle. drawtext
    has no wrapping, and a five-word caption at 72px ran off both edges."""
    txt = txt.strip()
    if len(txt) <= limit or " " not in txt:
        return txt
    mid = len(txt) // 2
    left = txt.rfind(" ", 0, mid + 1)
    right = txt.find(" ", mid)
    cut = left if (left != -1 and (right == -1 or mid - left <= right - mid)) else right
    return txt[:cut] + "\n" + txt[cut + 1:]


def _caption_filters(caption):
    """The drawtext filters for a caption, shared by the treatment chain and the
    wipe pass so a reveal can draw its caption AFTER the paper slides off."""
    if not caption:
        return []
    txt = _ff_escape(_wrap_caption(caption.strip().upper()))
    fade = "alpha='if(lt(t,0.25),t/0.25,1)'"
    if CAPTION_LOOK == "serif-highlight" and FONT_SERIF.is_file():
        fs = 72
        off = int(fs * 0.42)          # tuck the stroke under the baseline
        return [
            # the highlighter: same text in yellow on a fully opaque yellow box,
            # sat lower and drawn first, so only its lower band shows below the
            # white letters. Opaque, or dark panels ghost the glyphs through it.
            f"drawtext=fontfile={FONT_SERIF}:text='{txt}':"
            f"fontcolor=0xF2D24A:fontsize={fs}:line_spacing=12:"
            f"box=1:boxcolor=0xF2D24A@1.0:boxborderw=6:"
            f"x=(w-text_w)/2:y=h-text_h-190+{off}:{fade}",
            f"drawtext=fontfile={FONT_SERIF}:text='{txt}':"
            f"fontcolor=0xF7F3EA:fontsize={fs}:line_spacing=12:"
            f"x=(w-text_w)/2:y=h-text_h-190:{fade}",
        ]
    if FONT_DISPLAY.is_file():
        return [
            f"drawtext=fontfile={FONT_DISPLAY}:text='{txt}':"
            f"fontcolor=0xF5F0E6:fontsize=76:line_spacing=8:"
            f"box=1:boxcolor=0x1A1A1A@0.62:boxborderw=26:"
            f"x=(w-text_w)/2:y=h-text_h-190:{fade}",
        ]
    return []


def wipe_pass(seg, caption=None):
    """A route or a line 'drawing in': the finished shot is revealed left to
    right from under a paper-coloured cover over 1.3s. Split the stream,
    paint one copy paper, slide it off with overlay — overlay evaluates its
    position per frame, which crop and drawbox did not in testing."""
    tmp_out = seg.with_suffix(".wipe.mp4")
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(seg), "-filter_complex",
                        "[0:v]split[a][b];[b]drawbox=x=0:y=0:w=iw:h=ih:color=0xE9E0C9@1:thickness=fill[p];"
                        "[a][p]overlay=x='W*min(1,0.08+t/1.3)':y=0:eval=frame"
                        + "".join("," + f for f in _caption_filters(caption))
                        + ",format=yuv420p[v]",
                        "-map", "[v]", "-r", str(TREATED_FPS), "-c:v", "libx264", "-crf", "24",
                        "-preset", "medium", "-pix_fmt", "yuv420p", "-an", str(tmp_out)],
                       capture_output=True, text=True)
    if r.returncode == 0 and tmp_out.exists() and tmp_out.stat().st_size > 4096:
        tmp_out.replace(seg)
    else:
        print("    wipe pass failed, keeping the plain shot: " + r.stderr[-160:].strip(), flush=True)


def treat_shot(img, duration, caption, out_path, idx, palette=None,
               motion="drift", fps=None, no_texture=False, look="illustrated"):
    """One still -> one moving, graded, captioned clip.

    Three things separate the current output from the reference account, and
    none of them are the panel itself: nothing moves, nothing is graded, and
    the captions were drawn BY the image model, which is why they came out
    malformed. All three are ffmpeg work, and none of them cost anything.
    """
    fps = fps or TREATED_FPS
    d = max(0.4, float(duration or 2.5))
    frames = max(6, int(round(d * fps)))
    # The move is chosen by what the shot MEANS, not by its index. Kept small
    # on purpose: the reference breakdowns describe planes sitting close
    # together with the camera drifting gently, "like frosted glass" —
    # obvious movement pulls attention to the animation instead of the point.
    if motion == "wipe":
        # a route or a line "drawing in": the closest the free route gets is a
        # left-to-right reveal of the whole panel over paper, on a gentle push
        z = "min(zoom+0.00030,1.06)"
        xs, ys = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif motion == "none":
        z, xs, ys = "1.0", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif motion == "in":
        z = "min(zoom+0.00060,1.12)"
        xs, ys = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif motion == "out":
        z = "if(lte(zoom,1.0),1.12,max(zoom-0.00060,1.0))"
        xs, ys = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    elif motion == "right":
        z = "min(zoom+0.00020,1.06)"
        xs = f"iw/2-(iw/zoom/2)+(on/{frames})*70"
        ys = "ih/2-(ih/zoom/2)"
    elif motion == "left":
        z = "min(zoom+0.00020,1.06)"
        xs = f"iw/2-(iw/zoom/2)-(on/{frames})*70"
        ys = "ih/2-(ih/zoom/2)"
    else:
        z = "min(zoom+0.00025,1.05)"
        xs = "iw/2-(iw/zoom/2)"
        ys = f"ih/2-(ih/zoom/2)-(on/{frames})*30"
    chain = [
        f"scale={int(FRAME_H)}:-2",
        f"zoompan=z='{z}':d={frames}:x='{xs}':y='{ys}':s={FRAME_W}x{FRAME_H}:fps={fps}",
        # Grade moved to the layered pass below, where it can be selective.
        # A blanket desaturation was wrong: in the reference frames the SUBJECT
        # keeps its full colour (a blue coat, tan breeches) and only what
        # surrounds it goes grey.

        "noise=alls=4:allf=t",
    ]
    # motion == "wipe" is applied as a second pass at the call site (wipe_pass):
    # neither crop nor drawbox re-evaluated size/position per frame in testing;
    # overlay of a split copy does.
    if caption and motion != "wipe":
        # wipe shots get their caption in wipe_pass, after the reveal, or the
        # paper cover slides over the words as well
        chain += _caption_filters(caption)
    chain.append("format=yuv420p")
    # --- layered pass -------------------------------------------------------
    # The published collage breakdown builds depth out of separate planes that
    # the camera moves BETWEEN, and finishes with a specific grade: a radial
    # spotlight, then a vignette on top of it, then paper textures. Both the
    # spotlight and the vignette, deliberately — "by combining the background
    # and the vignette we've made the roll-off way smoother", because the grey
    # the ramp leaves in the corners is what the vignette then has room to work
    # on. Either alone is worse.
    #
    # We have one flat panel, not cut-out elements, so the planes here are the
    # TEXTURES: two of them, drifting at different rates over the picture. That
    # is the other technique in the same channel's timeline breakdown — layers
    # sitting close together so the parallax reads as "quiet frosted glass"
    # rather than as an effect.
    tex_a = TEXTURE_DIR / "paper_fibre.png"
    tex_b = TEXTURE_DIR / "paper_age.png"
    use_tex = tex_a.is_file() and tex_b.is_file() and not no_texture
    base = ",".join(chain)
    if not use_tex:
        cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(img), "-t", f"{d:.3f}",
               "-vf", base, "-r", str(fps)]
    else:
        # Each texture creeps at its own rate and in its own direction, so the
        # two never move as one sheet — that difference IS the parallax.
        # Built from the reference FRAMES, not from the narration describing
        # them. The narration says "black and white, radial ramp, vignette",
        # which taken literally produces a dark desaturated tunnel. The frames
        # show something else:
        #
        #   * colour is SELECTIVE — the subject keeps it, the surround loses it
        #   * the ground ends up LIGHT and warm, not dark. The ramp and vignette
        #     darken, and then the paper textures in overlay lift it back; he
        #     says himself the texture is "brighter than everything else".
        #     Doing only the first half is what made ours murky.
        #
        # Every blend input is gbrp — three identical colour planes — because
        # blending a gray-format layer against colour corrupts chroma and came
        # back heavily green.
        warm = "colorbalance=rs=0.03:rm=0.025:bm=-0.025"
        if look == "cartographic":
            # The maps look is the other pole: dark slate ground, cool
            # desaturated landmass, fine light linework kept crisp.
            grade = "eq=saturation=0.45:contrast=1.16:gamma=0.92," \
                    "colorbalance=rs=-0.04:gm=0.03:bm=0.05:bh=0.04"
            fc = (
                f"[0:v]{base},{grade},format=gbrp[pic];"
                f"[1:v]format=gray,scale=1400:-1,loop=loop=-1:size=1,"
                f"crop={FRAME_W}:{FRAME_H}:'40+18*t/{max(d,0.001):.3f}':'30+12*t/{max(d,0.001):.3f}',"
                f"format=gbrp[t1];"
                f"[pic][t1]blend=all_mode=softlight:all_opacity=0.12[x1];"
                f"[x1]vignette=angle=PI/4,format=yuv420p[v]"
            )
        else:
            # The reference grade — desaturate, ramp, vignette, textures — exists
            # to BUILD a collage out of raw photographs. Our illustrated panels
            # arrive already designed and already graded by the image model, so
            # the same treatment can only subtract from them: side by side, the
            # untreated panel had a stronger red, a richer terracotta and a
            # cleaner ground, and the treated one was hazier at every setting we
            # tried. Applying a construction grade to a finished picture is the
            # mistake; the panel's own colour is the thing worth protecting.
            #
            # So this pass adds only what the panel cannot have on its own:
            # movement, a whisper of tooth, and the faintest edge falloff.
            fc = (
                f"[0:v]{base},format=gbrp[pic];"
                f"[1:v]format=gray,scale=1400:-1,loop=loop=-1:size=1,"
                f"crop={FRAME_W}:{FRAME_H}:'40+20*t/{max(d,0.001):.3f}':'40+14*t/{max(d,0.001):.3f}',"
                f"format=gbrp[t1];"
                f"[pic][t1]blend=all_mode=softlight:all_opacity=0.07[x1];"
                f"[x1]vignette=angle=PI/9,format=yuv420p[v]"
            )
        cmd = ["ffmpeg", "-y", "-loop", "1", "-i", str(img),
               "-loop", "1", "-i", str(tex_a), "-loop", "1", "-i", str(tex_b),
               "-t", f"{d:.3f}", "-filter_complex", fc, "-map", "[v]", "-r", str(fps)]
    # Film grain is nearly incompressible, so without a quality target the
    # treated cut ballooned to 81 MB for 60 seconds.
    cmd += ["-c:v", "libx264", "-crf", "24", "-preset", "medium",
            "-pix_fmt", "yuv420p", "-an", str(out_path)]
    r = subprocess.run(cmd, capture_output=True, timeout=900)
    if r.returncode != 0 and use_tex:
        # Never lose a shot to the texture pass — fall back to the plain chain.
        print(f"      texture pass failed, plain: {r.stderr.decode()[-200:]}", flush=True)
        subprocess.run(["ffmpeg", "-y", "-loop", "1", "-i", str(img), "-t", f"{d:.3f}",
                        "-vf", base, "-r", str(fps), "-c:v", "libx264", "-crf", "24",
                        "-preset", "medium", "-pix_fmt", "yuv420p", "-an", str(out_path)],
                       capture_output=True, check=True, timeout=900)
    elif r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, cmd, r.stdout, r.stderr)
    return out_path


def make_speech(text, voice, out_path):
    raw = openai("/audio/speech", {"model": TTS_MODEL, "voice": voice, "input": text})
    out_path.write_bytes(raw)
    return out_path


def probe_seconds(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, timeout=60).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def assign_shot_types(shots, style):
    """Which of a style's looks each shot gets.

    A style may be several looks cut between on purpose. Cue words in the
    shot's own text decide first (a country wants a map, a number a chart);
    the beat it sits in decides next; failing both, the type furthest below
    its share, so the mix stays close to the weights. Runs before the
    estimate so a dry run shows the mix and the cost counts real references.
    """
    types = (style or {}).get("shot_types") or []
    if isinstance(types, str):
        try:
            types = json.loads(types)
        except Exception:
            types = []
    type_for, counts = {}, {}
    if not types:
        return types, type_for, counts
    counts = {t["key"]: 0 for t in types}
    wsum = sum(float(t.get("weight", 1)) for t in types) or 1.0
    for s_ in shots:
        # Only the shot's OWN words. image_prompt is the engine's account-wide
        # survey, identical on every shot and full of the very cue words
        # (maps, archival, illustrate...) that would send them all one way.
        text = " ".join(str(s_.get(k) or "") for k in
                        ("subject", "voiceover", "narration", "line", "caption")).lower()
        beat = str(s_.get("beat_role") or s_.get("beat_name") or s_.get("beat") or "").lower()
        chosen, why = None, ""
        import re as _re
        def _hits(c):
            c = c.lower().strip()
            if not c:
                return False
            if c[0].isalnum() and c[-1].isalnum():
                # whole words only: "rate" must not fire on "illustrate"
                return _re.search(r"\b" + _re.escape(c) + r"\b", text) is not None
            return c in text
        for t in types:
            hit = next((c for c in (t.get("cues") or []) if _hits(c)), None)
            if hit:
                chosen, why = t, "cue " + repr(hit)
                break
        if not chosen and beat:
            for t in types:
                if beat in [str(x).lower() for x in (t.get("beats") or [])]:
                    chosen, why = t, "beat " + repr(beat)
                    break
        if not chosen:
            done = sum(counts.values())
            chosen = max(types, key=lambda t: float(t.get("weight", 1)) / wsum
                         - ((counts[t["key"]] / done) if done else 0.0))
            why = "share"
        counts[chosen["key"]] += 1
        type_for[id(s_)] = chosen
        print("  shot %2d -> %-18s (%s)" % (s_["shot"], chosen["key"], why), flush=True)
    print("  mix: " + ", ".join("%s %d" % (k, v) for k, v in counts.items()), flush=True)
    return types, type_for, counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--creation_id", type=int, required=True)
    ap.add_argument("--voice", default="onyx", help="OpenAI TTS voice — YOUR chosen voice, not a clone")
    ap.add_argument("--size", default="1024x1536", help="portrait for short-form")
    ap.add_argument("--quality", default="low", choices=["low", "medium", "high"],
                    help="image quality — low is ~10x cheaper than high and fine for panels")
    ap.add_argument("--max-images", type=int, default=0,
                    help="cap total images; panels are reused within a beat to stay under it")
    ap.add_argument("--budget", type=float, default=3.0,
                    help="refuse to start if the estimate exceeds this, in USD")
    ap.add_argument("--renderer", default="openai", choices=["openai", "higgsfield"],
                    help="who makes the pictures")
    ap.add_argument("--input-fidelity", choices=["low", "high"], default="low",
                    help="how closely OpenAI preserves reference-image detail; high costs ~6x per reference")
    ap.add_argument("--caption-look", choices=["anton", "serif-highlight"], default="anton",
                    help="anton = condensed sans on a dark box (unchanged default); "
                         "serif-highlight = serif caps with a yellow highlighter stroke, "
                         "the treatment seen across @johnnyharris")
    ap.add_argument("--reference-frames", type=int, default=0, metavar="N",
                    help="use N of the account's OWN frames as visual reference "
                         "(image edit) instead of generating from the text prompt alone")
    ap.add_argument("--style", default=None, metavar="SLUG",
                    help="a saved render style (see /production/styles). Its prompt prefix is "
                         "prepended to every panel prompt and its caption mode is applied, so "
                         "the LOOK travels with the style instead of being re-described.")
    ap.add_argument("--look", choices=["illustrated", "archival", "cartographic"], default=None,
                    help="preset for the two combinations that tested well. 'illustrated' = "
                         "text-prompted panels with the model's own integrated lettering, "
                         "treated. 'archival' = the account's real frames as reference with "
                         "captions drawn as type, treated. Which one is right depends on the "
                         "MATERIAL, not the account's prestige.")
    ap.add_argument("--no-caption-overlay", action="store_true",
                    help="apply treatment but do NOT draw captions — for panels that already "
                         "have their text composed into the image")
    ap.add_argument("--no-texture", action="store_true",
                    help="skip the drifting paper-texture planes and the spotlight/vignette pair")
    ap.add_argument("--static", action="store_true",
                    help="treatment without camera movement — isolates grade/grain from motion")
    ap.add_argument("--treatment", action="store_true",
                    help="Ken Burns motion, colour grade, grain, vignette, and captions "
                         "drawn as real type rather than by the image model")
    ap.add_argument("--plain-prompts", action="store_true",
                    help="fetch the bare prompt instead of the enriched one — for A/C comparison")
    ap.add_argument("--motion", action="store_true",
                    help="higgsfield only: animate each panel into a clip instead of holding a still")
    ap.add_argument("--no-audio", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    global CAPTION_LOOK
    CAPTION_LOOK = getattr(args, "caption_look", "anton")
    # Both presets tested well; they differ only in how the caption is made.
    # Integrated lettering belongs on an illustrated panel, where it is part of
    # the composition. Drawn type belongs over photographic archive, where
    # there is no composition to belong to. Applying the second rule to the
    # first kind of picture is what made an earlier render worse.
    style = None
    if args.style:
        try:
            style = engine(f"/api/render-styles/{args.style}")["style"]
        except Exception as e:
            sys.exit(f"could not load style '{args.style}': {e}")
        # A style knows which caption treatment its material wants — integrated
        # lettering on an illustrated panel, drawn type over photographic
        # material. Getting that backwards is what made an earlier render worse,
        # so the style decides it rather than the caller remembering.
        args.treatment = True
        if style.get("caption_mode") == "baked":
            args.no_caption_overlay = True
        if style.get("aspect"):
            w, h = set_frame(style["aspect"])
            # The image request has to match, or a landscape look arrives as a
            # vertical crop of itself.
            args.size = {"16:9": "1536x1024", "4:3": "1536x1024", "1:1": "1024x1024"}.get(
                style["aspect"], args.size)
            print(f"  frame: {w}x{h} ({style['aspect']}) from style", flush=True)
    if args.look == "illustrated":
        args.treatment = True
        args.no_caption_overlay = True
    elif args.look == "cartographic":
        args.treatment = True
        args.no_caption_overlay = True
    elif args.look == "archival":
        args.treatment = True
        if not args.reference_frames:
            args.reference_frames = 3

    if not KEY:
        sys.exit("PPE_INGEST_API_KEY not set")

    plan = engine(f"/api/creations/{args.creation_id}/assembly-plan?refs={max(args.reference_frames or 0, 4)}"
                  + ("?plain=1" if args.plain_prompts else ""))
    if not plan.get("ok"):
        sys.exit(plan.get("error"))
    shots = plan["shots"]
    _types, _type_for, _type_counts = assign_shot_types(shots, style)

    # Reuse one panel across a beat's shots when capped: the pictures repeat
    # while the audio runs on, which is what the cheaper end of this format
    # does anyway, and it is the only honest way to hit a budget.
    if args.max_images and len(shots) > args.max_images:
        beats = sorted({s["beat"] for s in shots})
        per_beat = max(1, args.max_images // max(1, len(beats)))
        keep = {}
        for b in beats:
            for i, s in enumerate([x for x in shots if x["beat"] == b]):
                keep[id(s)] = i % per_beat
        for s in shots:
            s["_img_key"] = f"b{s['beat']}_{keep[id(s)]}"
    else:
        for s in shots:
            s["_img_key"] = f"s{s['shot']:03d}"
    n_images = len({s["_img_key"] for s in shots})
    # Assets persist between runs on purpose — a run that dies at shot 30 of 48
    # must not buy the first 30 again. The estimate has to know that too, or the
    # budget guard refuses a re-render whose images are all already on disk and
    # whose real cost is zero. Count only what would actually be bought.
    _suffix = "" if args.renderer == "openai" else f"_{args.renderer}" + ("_motion" if args.motion else "")
    if args.plain_prompts:
        _suffix += "_plain"
    if getattr(args, "treatment", False):
        _suffix += "_treated"
    if getattr(args, "reference_frames", 0):
        _suffix += f"_ref{args.reference_frames}"
    if getattr(args, "style", None):
        _suffix += f"_{args.style}"
    if getattr(args, "no_caption_overlay", False):
        _suffix += "_nocap"
    if getattr(args, "static", False):
        _suffix += "_static"
    _work = OUT_DIR / f"work_{args.creation_id}{_suffix}"
    _have = {k for k in {sh["_img_key"] for sh in shots}
             if (_work / f"{k}.png").exists() and (_work / f"{k}.png").stat().st_size > 2048}
    n_cached = len(_have)
    n_images = max(0, n_images - n_cached)

    vo = [s for s in shots if s.get("voiceover")]
    chars = sum(len(s["voiceover"]) for s in vo)
    if args.renderer == "higgsfield":
        # Higgsfield bills in credits, not dollars, so the dollar estimate is
        # not meaningful here — report the unit that is actually spent and let
        # the budget guard apply only to the renderer it was measured for.
        per_image = 0.0
        cost = 0.0
    else:
        per_image = IMAGE_USD_BY_QUALITY[args.quality]
        # Reference images are billed as input tokens on top of each panel: about
        # 700 tokens at low fidelity, about 6,000 at high, at $10 per million.
        # Left out, the estimate quoted $0.25 for a run that cost about $1.
        _nref = int(getattr(args, "reference_frames", 0) or 0)
        if _nref and _type_for:
            # types send only their own pictures, so count those, not the cap
            _per = [min(len(_type_for[id(x)].get("reference_images") or []), _nref)
                    for x in shots if id(x) in _type_for]
            _nref = round(sum(_per) / len(_per), 1) if _per else _nref
        _ref_usd = n_images * _nref * (0.06 if getattr(args, "input_fidelity", "low") == "high" else 0.007)
        _ref_note = (", %s refs/panel = $%.2f" % (_nref, _ref_usd)) if _nref else ""
        cost = (n_images * per_image + _ref_usd
                + (0 if args.no_audio else chars / 1000 * TTS_USD_PER_1K_CHARS))

    print(f"{plan['title']}  [{plan['mode']}]  {plan['profile']} · {plan['account']}")
    print(f"  {plan['total_shots']} shots, ~{plan['runtime_sec']}s, "
          f"visual brief: {'yes' if plan['has_visual_brief'] else 'NO'}"
          + (f"  |  style: {style['name']} ({style.get('medium','')[:40]})" if style
             else "  — panels will be generic" if not plan['has_visual_brief'] else ""))
    _t = plan.get("tempo")
    if _t:
        print(f"  cutting to {_t['bpm']} BPM ({_t['beat_sec']}s/beat) — this account puts "
              f"{_t['pct_cuts_on_a_beat']}% of its cuts on the beat across {_t['n_videos']} videos")
    if args.renderer == "higgsfield":
        n_clips = n_images if args.motion else 0
        print(f"  renderer: higgsfield ({HF_IMAGE_MODEL}"
              f"{' + ' + HF_VIDEO_MODEL if args.motion else ''})")
        print(f"  will make: {n_images} panel(s)" + (f" and animate all {n_clips}" if args.motion else "")
              + f"{'' if args.no_audio else f', {chars} chars of speech via OpenAI'}")
        print(f"  spends Higgsfield CREDITS — check `higgsfield workspace status` for your balance")
    else:
        print(f"  estimated cost: ${cost:.2f}  ({n_images} images @ ${per_image:.2f} {args.quality}"
              f"{'' if args.no_audio else f', {chars} chars of speech'}{_ref_note})"
              + (f"  [{n_cached} panel(s) already made — not charged again]" if n_cached else ""))
    if n_images < len(shots):
        print(f"  (capped at {args.max_images}: {len(shots)} shots share {n_images} panels)")
    if not plan["has_visual_brief"]:
        print("  ! run the visual-style analysis on this account first, or the panels")
        print("    will not look like it.")
    if cost > args.budget and not args.dry_run:
        print(f"\n  REFUSED: ${cost:.2f} is over the --budget of ${args.budget:.2f}.")
        print(f"  Lower it with --max-images, or raise --budget deliberately.")
        return
    if args.dry_run:
        for s in shots[:4]:
            print(f"\n  shot {s['shot']} (beat {s['beat']}, {s['duration_sec']}s)")
            # Show what will ACTUALLY be sent. Printing the untransformed prompt
            # made a dry run agree with itself and disagree with the render.
            _p = s["image_prompt"] or ""
            if style and style.get("prompt_prefix"):
                _p = (style["prompt_prefix"].strip() + "\n\nSubject: "
                      + ((s.get("subject") or "").strip() or _p))
            print(f"    prompt : {_p[:230]}")
            if s.get("caption"):
                print(f"    caption: {s['caption']}")
            if s.get("voiceover"):
                print(f"    voice  : {s['voiceover'][:90]}")
        print(f"\n  (dry run — nothing generated, nothing spent)")
        return
    if not OPENAI_KEY:
        sys.exit("TRANSCRIBE_API_KEY (the OpenAI key) is not set on this box")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "" if args.renderer == "openai" else f"_{args.renderer}" + ("_motion" if args.motion else "")
    if args.plain_prompts:
        suffix += "_plain"
    if args.treatment:
        suffix += "_treated"
    if args.reference_frames:
        suffix += f"_ref{args.reference_frames}"
    if args.style:
        suffix += f"_{args.style}"
    if args.no_caption_overlay:
        suffix += "_nocap"
    if args.static:
        suffix += "_static"
    final = OUT_DIR / f"creation_{args.creation_id}{suffix}.mp4"
    # Generated assets are KEPT, not thrown away with a temp dir: every image
    # is money already spent, and a run that dies at shot 30 of 48 must resume
    # rather than buy the first 30 again.
    tmp = OUT_DIR / f"work_{args.creation_id}{suffix}"
    tmp.mkdir(parents=True, exist_ok=True)
    if True:
        # --- audio first: real speech length beats the spec's estimate ------
        beat_audio = {}
        if not args.no_audio:
            for s in vo:
                a = tmp / f"beat_{s['beat']}.mp3"
                if a.exists() and a.stat().st_size > 1024:
                    print(f"  speech for beat {s['beat']}: already made", flush=True)
                else:
                    print(f"  speech for beat {s['beat']}…", flush=True)
                    make_speech(s["voiceover"], args.voice, a)
                beat_audio[s["beat"]] = (a, probe_seconds(a))

        # --- the account's own frames, as visual reference ------------------
        refs = []
        if args.reference_frames:
            # Pictures a person attached to the style come first — an output
            # already judged correct is a stronger reference than a frame
            # sampled blind — then frames from the account fill the rest.
            _style_imgs = (style or {}).get("reference_images") or []
            if isinstance(_style_imgs, str):
                try:
                    _style_imgs = json.loads(_style_imgs)
                except Exception:
                    _style_imgs = []
            urls = (list(_style_imgs) + (plan.get("reference_frames") or []))[:args.reference_frames]
            if _style_imgs:
                print(f"  reference: {min(len(_style_imgs), args.reference_frames)} style picture(s) "
                      f"+ account frames, {len(urls)} total", flush=True)
            if urls:
                refs = fetch_reference_frames(urls, tmp / "refs")
                print(f"  reference: {len(refs)} real frame(s) from {plan.get('account')}", flush=True)
            else:
                print("  ! no reference frames available for this account — "
                      "falling back to the text prompt alone", flush=True)

        # --- what best-of-N judges a panel against ---------------------------
        # Only the style carries measured targets, and only the measurement loop
        # writes them. Without them there is nothing to select on, so the flag
        # degrades to plain single-shot generation rather than guessing.
        # account frames are shared; each type brings its own pictures
        _account_urls = (plan.get("reference_frames") or []) if args.reference_frames else []
        _refs_by_type = {}

        # --- one image per shot ---------------------------------------------
        made = set()
        last_good = None
        import time as _t_mod
        _last_call = [None]
        for s in shots:
            img = tmp / f"{s['_img_key']}.png"
            if s["_img_key"] in made:
                s["_img"] = img
                last_good = img
                continue
            made.add(s["_img_key"])
            if img.exists() and img.stat().st_size > 2048:
                print(f"  image {s['shot']}/{len(shots)}: already made", flush=True)
            elif args.renderer == "higgsfield":
                print(f"  panel {s['shot']}/{len(shots)} via {HF_IMAGE_MODEL}…", flush=True)
                hf_make_image(s["image_prompt"], img)
            else:
                prompt = s["image_prompt"] or ""
                _t = _type_for.get(id(s))
                if _t:
                    # this shot's look: its own prompt and its own pictures
                    body = (s.get("subject") or "").strip() or prompt
                    prompt = ((_t.get("prompt") or style.get("prompt_prefix") or "").strip()
                              + "\n\nSubject: " + body)
                    if style.get("avoid"):
                        prompt += "\n\nAVOID: " + style["avoid"]
                    if args.reference_frames:
                        if _t["key"] not in _refs_by_type:
                            # a type's own pictures only. Topping up from frames sampled
                            # blind across the account hands a chart panel seven talking
                            # heads at 480p, which blends the type away.
                            _u = list(_t.get("reference_images") or [])[:args.reference_frames]
                            _refs_by_type[_t["key"]] = fetch_reference_frames(_u, tmp / ("refs_" + _t["key"]))
                        refs = _refs_by_type[_t["key"]]
                elif style and style.get("prompt_prefix"):
                    # A named style REPLACES the account's look rather than
                    # sitting in front of it. image_prompt is already enriched
                    # with the spec's own account brief — for this spec that is
                    # a photographic webcam setup — so prefixing a flat-vector
                    # style onto it hands the model two contradictory mediums
                    # and it picks one. `subject` carries the shot's content
                    # without any look attached, which is the clean seam:
                    # style supplies the medium, subject supplies the content.
                    body = (s.get("subject") or "").strip() or prompt
                    prompt = style["prompt_prefix"].strip() + "\n\nSubject: " + body
                    if style.get("avoid"):
                        prompt += "\n\nAVOID: " + style["avoid"]
                if args.treatment and not args.no_caption_overlay:
                    # Strip the "render this text" clause ONLY when we are going
                    # to draw the caption ourselves. On an illustrated panel the
                    # model's own integrated lettering reads better than an
                    # overlaid box — that rule came from an account compositing
                    # type over photographic archive, which is different
                    # material — so when the overlay is off, leave it be.
                    prompt = strip_caption_instruction(prompt)
                # Pace the requests. Firing 22 image calls back to back is the
                # worst pattern against a per-minute cap: it trips the limit,
                # and the retries then hammer the same window. A deliberate gap
                # between requests keeps a long run inside the allowance instead
                # of racing it. IMAGE_GAP_SEC=0 disables.
                _gap = float(os.environ.get("IMAGE_GAP_SEC", "14"))
                if _gap > 0 and _last_call[0] is not None:
                    _wait = _gap - (_t_mod.time() - _last_call[0])
                    if _wait > 0:
                        _t_mod.sleep(_wait)
                _last_call[0] = _t_mod.time()
                try:
                    if refs:
                        print(f"  image {s['shot']}/{len(shots)} (ref)…", flush=True)
                        make_image_ref(prompt, refs, args.size, img, args.quality,
                                       getattr(args, "input_fidelity", "low"))
                    else:
                        print(f"  image {s['shot']}/{len(shots)}…", flush=True)
                        make_image(prompt, args.size, img, args.quality)
                except Exception as exc:
                    # One panel the API would not hand over must not cost the
                    # whole cut. A rate limit late in a long session is the
                    # common case, and a shot that borrows its neighbour reads
                    # as a held frame — which is a real editing move — where a
                    # dead render reads as nothing at all.
                    print(f"  image {s['shot']}/{len(shots)} FAILED "
                          f"({type(exc).__name__}) — borrowing the previous panel", flush=True)
                    made.discard(s["_img_key"])
                    if last_good is not None:
                        s["_img"] = last_good
                        continue
                    raise
            s["_img"] = img
            last_good = img

            if args.renderer == "higgsfield" and args.motion:
                clip = tmp / f"{s['_img_key']}.mp4"
                if clip.exists() and clip.stat().st_size > 4096:
                    print(f"    clip: already made", flush=True)
                else:
                    print(f"    animating with {HF_VIDEO_MODEL}…", flush=True)
                    hf_make_clip(s["image_prompt"], img, s["duration_sec"] or 3, clip)
                s["_clip"] = clip

        # --- fit each beat's shots to its real spoken length ----------------
        # Speech never lands exactly on the planned duration, so the picture
        # has to absorb the difference. Where the account cuts to a beat, the
        # shots are stretched in WHOLE BEATS and the remainder is given to the
        # last shot — otherwise dividing the spoken length evenly would undo
        # the quantisation the plan just did and put every cut off the grid.
        beat_sec = (plan.get("tempo") or {}).get("beat_sec")
        for beat, (path, secs) in beat_audio.items():
            in_beat = [s for s in shots if s["beat"] == beat]
            if not (in_beat and secs):
                continue
            if beat_sec and secs > beat_sec * len(in_beat):
                total_beats = max(len(in_beat), int(round(secs / beat_sec)))
                per, extra = divmod(total_beats, len(in_beat))
                for i, s in enumerate(in_beat):
                    n = per + (1 if i >= len(in_beat) - extra else 0)
                    s["duration_sec"] = round(n * beat_sec, 3)
                drift = round(secs - sum(s["duration_sec"] for s in in_beat), 3)
                if abs(drift) > 0.01:
                    in_beat[-1]["duration_sec"] = round(in_beat[-1]["duration_sec"] + drift, 3)
            else:
                each = round(secs / len(in_beat), 3)
                for s in in_beat:
                    s["duration_sec"] = each

        silent = tmp / "silent.mp4"
        if any(s.get("_clip") for s in shots):
            # Motion path: trim each clip to the shot's own length, then join.
            # The model only makes 5-second clips, so untrimmed they would
            # each overrun and the picture would drift away from its audio.
            parts = []
            for s in shots:
                if not s.get("_clip"):
                    continue
                d = s["duration_sec"] or 2.5
                cut = tmp / f"cut_{s['shot']:03d}.mp4"
                if not cut.exists():
                    subprocess.run(["ffmpeg", "-y", "-i", str(s["_clip"]), "-t", f"{d}",
                                    "-vf", "scale=1080:-2,format=yuv420p", "-r", "30",
                                    "-an", str(cut)], capture_output=True, check=True, timeout=600)
                parts.append(cut)
            clist = tmp / "clips.txt"
            with open(clist, "w") as f:
                for c in parts:
                    f.write(f"file '{c}'\n")
            subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(clist),
                            "-c", "copy", str(silent)], capture_output=True, check=True, timeout=900)
        elif args.treatment:
            # Each still becomes its own moving, graded, captioned clip, then
            # they are joined. The concat demuxer cannot do per-shot filtering,
            # which is why the untreated path holds a dead frame.
            parts = []
            seen_beats = set()
            for i, s in enumerate(shots):
                seg = tmp / f"treated_{s['shot']:03d}.mp4"
                first = s.get("beat") not in seen_beats
                seen_beats.add(s.get("beat"))
                mv = "none" if args.static else motion_for(
                    " ".join(str(x) for x in (s.get("caption"), s.get("voiceover"))), first, i)
                try:
                    _tt = _type_for.get(id(s))
                except NameError:
                    _tt = None
                if _tt and _tt.get("motion") and not args.static:
                    mv = _tt["motion"]      # e.g. "wipe" for a route or a line drawing in
                if not (seg.exists() and seg.stat().st_size > 4096):
                    print(f"  treating shot {s['shot']}/{len(shots)} [{mv}]…", flush=True)
                    cap = None if args.no_caption_overlay else s.get("caption")
                    treat_shot(s["_img"], s["duration_sec"], cap, seg, i, motion=mv,
                               no_texture=args.no_texture,
                               look=(args.look or "illustrated"))
                    if mv == "wipe":
                        wipe_pass(seg, cap)
                parts.append(seg)
            tlist = tmp / "treated.txt"
            with open(tlist, "w") as f:
                for c in parts:
                    f.write(f"file '{c}'\n")
            subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(tlist),
                            "-c", "copy", str(silent)], capture_output=True, check=True, timeout=900)
        else:
            concat = tmp / "concat.txt"
            with open(concat, "w") as f:
                for s in shots:
                    d = s["duration_sec"] or 2.5
                    f.write(f"file '{s['_img']}'\nduration {d}\n")
                f.write(f"file '{shots[-1]['_img']}'\n")   # ffmpeg needs the last frame repeated
            subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                            "-vf", "scale=1080:-2,format=yuv420p", "-r", "30", str(silent)],
                           capture_output=True, check=True, timeout=900)

        if beat_audio:
            alist = tmp / "audio.txt"
            with open(alist, "w") as f:
                for beat in sorted(beat_audio):
                    f.write(f"file '{beat_audio[beat][0]}'\n")
            voice_track = tmp / "voice.mp3"
            subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(alist),
                            "-c", "copy", str(voice_track)], capture_output=True, check=True, timeout=300)
            subprocess.run(["ffmpeg", "-y", "-i", str(silent), "-i", str(voice_track),
                            "-c:v", "copy", "-c:a", "aac", "-shortest", str(final)],
                           capture_output=True, check=True, timeout=900)
        else:
            subprocess.run(["ffmpeg", "-y", "-i", str(silent), "-c", "copy", str(final)],
                           capture_output=True, check=True, timeout=900)

    secs = probe_seconds(final)
    # The spec chooses a runtime and writes to it; the voice then runs to its
    # own length and the pictures stretch to match. Saying how far apart they
    # landed is the only way to know whether writing-to-length is working —
    # a 46s spec that renders at 95s destroyed its own cut rhythm silently.
    planned = plan.get("runtime_sec")
    if planned:
        drift = secs - planned
        flag = "" if abs(drift) <= max(4.0, planned * 0.12) else "   <-- OVERRUN"
        print(f"  runtime: {secs:.1f}s actual vs {planned:.1f}s planned "
              f"({drift:+.1f}s){flag}", flush=True)
        if not (40 <= secs <= 120):
            print(f"  ! {secs:.1f}s is outside the 40-120s target band", flush=True)
    print(f"\ndone: {final}  ({secs:.1f}s, {final.stat().st_size/1e6:.1f} MB)")

    # Tell the engine, so the video appears there rather than only on this
    # box. A failure to record must not look like a failure to build.
    watch_url = os.environ.get("ASSEMBLY_PUBLIC_URL", "http://100.116.54.94:5001") + f"/videos/{final.name}"
    try:
        r = engine(f"/api/creations/{args.creation_id}/assembled", {
            "url": watch_url, "path": str(final), "duration_sec": round(secs, 1),
            "n_shots": len(shots), "cost_usd": round(cost, 2), "tool": "assemble_video.py",
            # Name the build, so variants of one spec sit side by side on the
            # site instead of overwriting each other.
            "variant": ("plain prompts" if args.plain_prompts else "enriched prompts")
                       + (f", {plan['tempo']['bpm']:g} BPM beat-aligned" if plan.get("tempo") else "")
                       + (f", {args.renderer}" if args.renderer != "openai" else "")
                       + (", motion" if args.motion else ""),
        })
        print(f"  recorded on the engine as video creation {r.get('video_creation_id')}")
    except Exception as exc:  # noqa: BLE001
        print(f"  (built fine, but could not record it on the engine: {exc})")


if __name__ == "__main__":
    main()
