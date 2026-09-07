#!/usr/bin/env python3
"""
measure_reference.py — measure what a reference clip ACTUALLY contains.

Why this exists
---------------
Until now a style's palette came from asking a model to look at frames and name
hex values. That is the model's impression of the frames, not the frames. It
produced two documented failures:

  * @guijooorge's style recorded royal blue #306ce4 as "the" palette after one
    sample. The very next section generated from the same reference came back
    on a yellow ground at 87% of frame area. The ground is chosen per scene;
    a single sample cannot show that, and a named palette hides it.
  * Written briefs described surface properties — "rough edges, warm palette,
    shallow depth" — that fit a flat vector illustration exactly as well as the
    photographic collage that was meant, and the generator produced the wrong
    medium.

Both are measurement problems. This script measures instead:

  ground colour AND its share of frame area, per scene, so a per-scene colour
  system is visibly different from a fixed brand colour; flatness, so "flat
  vector" and "photographic" are separated by a number rather than an
  adjective; linework weight; saturation; and where captions sit.

It reports VARIANCE, and it refuses to call anything fixed on one sample.

Usage:
    python3 measure_reference.py <clip.mp4|asset-url> [--json out.json]
    python3 measure_reference.py --style guijooorge-flat
    python3 measure_reference.py --channel "@johnnyharris" --limit 6
"""
import argparse, json, os, subprocess, sys, tempfile, urllib.request
import numpy as np

W = 320                 # analysis width; enough for colour and edge structure
SCENE_THRESH = 0.30
K = 6                   # colour clusters per frame
MIN_SHARE = 0.02        # a cluster under 2% of frame is not part of the design


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, **kw)


def duration(path):
    o = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "csv=p=0", path], text=True).stdout.strip()
    try:
        return float(o)
    except ValueError:
        return 0.0


def scene_cuts(path):
    """Cut times inside the clip, so colour can be reported PER SCENE."""
    out = _run(["ffmpeg", "-v", "info", "-i", path, "-vf",
                f"select='gt(scene,{SCENE_THRESH})',showinfo", "-f", "null", "-"],
               text=True).stderr
    ts = []
    for line in out.splitlines():
        if "pts_time:" in line:
            try:
                ts.append(float(line.split("pts_time:")[1].split()[0]))
            except (ValueError, IndexError):
                pass
    return sorted(ts)


def sample_times(path):
    """One frame mid-scene; on a clip with no detected cuts, every 2 seconds."""
    dur = duration(path)
    cuts = scene_cuts(path)
    bounds = [0.0] + cuts + [dur]
    times = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a < 0.4:          # too short to be a real shot
            continue
        times.append(a + (b - a) / 2)
    if len(times) < 2:
        times = [t for t in np.arange(0.5, max(dur - 0.5, 1.0), 2.0)]
    return dur, cuts, times


def frame_raw(path, t):
    """The frame as delivered, bars and all — only for reporting the crop."""
    p = _run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", path, "-frames:v", "1",
              "-vf", f"scale={W}:-2", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
    if not p.stdout:
        return None
    h = len(p.stdout) // (W * 3)
    return (np.frombuffer(p.stdout[:h * W * 3], np.uint8).reshape(h, W, 3)
            if h else None)


def frame(path, t):
    """One frame as an HxWx3 uint8 array, straight out of ffmpeg."""
    p = _run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", path, "-frames:v", "1",
              "-vf", f"scale={W}:-2", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
    buf = p.stdout
    if not buf:
        return None
    h = len(buf) // (W * 3)
    if h == 0:
        return None
    img = np.frombuffer(buf[:h * W * 3], np.uint8).reshape(h, W, 3)
    cropped, _ = letterbox(img)
    return cropped


def merge_near(items, tol=26.0):
    """Fold near-identical colours together.

    Two samples of the same flat yellow come back as #f9bc3d and #f9bc3e. Left
    apart they triple-count one colour, splitting its area across three rows and
    making a reused ground look like three one-off ones.
    """
    out = []
    for it in sorted(items, key=lambda d: -d.get("mean_share", d.get("share", 0))):
        c = np.array([int(it["hex"][i:i + 2], 16) for i in (1, 3, 5)], float)
        for o in out:
            if np.linalg.norm(c - o["_rgb"]) < tol:
                o["mean_share"] = round(o.get("mean_share", 0) + it.get("mean_share", 0), 4)
                o["_n"] += 1
                break
        else:
            it = dict(it); it["_rgb"] = c; it["_n"] = 1
            out.append(it)
    return out


def letterbox(img, max_frac=0.18):
    """Crop uniform padding at the edges, whatever colour it is.

    The first version looked for BLACK bars and found none, because the padding
    on these reels is white: a uniform band across the bottom tenth carrying
    burned-in auto-subtitles. Left in, it counted as design, and OCR read the
    subtitle — a generic sans on a grey pill — and reported it as the account's
    caption style. It is not; the account's typography is the display lettering
    inside the artwork.

    A band is only cropped when its colour actually DIFFERS from the artwork
    behind it. On flat-colour material the top rows are legitimately a uniform
    ground, and cropping those would quietly shrink the ground share that the
    whole colour verdict rests on.
    """
    g = img.mean(axis=2)
    h, w = g.shape
    std = g.std(axis=1)
    lim = int(h * max_frac)

    def band(rows):
        n = 0
        for i in rows:
            if std[i] < 12 and n < lim:
                n += 1
            else:
                break
        return n

    t = band(range(h))
    b = band(range(h - 1, -1, -1))
    core = img[t:h - b] if (t + b) < h else img
    if core.size == 0:
        return img, None
    core_mean = core.reshape(-1, 3).mean(axis=0)

    def differs(sl):
        return np.linalg.norm(sl.reshape(-1, 3).mean(axis=0) - core_mean) > 55

    if t and not differs(img[:t]):
        t = 0
    if b and not differs(img[h - b:]):
        b = 0
    if not (t or b):
        return img, None
    return (np.ascontiguousarray(img[t:h - b]),
            {"top": round(t / h, 3), "bottom": round(b / h, 3)})


def hexof(c):
    return "#%02x%02x%02x" % tuple(int(round(x)) for x in c)


def hsv_of(rgb):
    r, g, b = [x / 255.0 for x in rgb]
    mx, mn = max(r, g, b), min(r, g, b)
    v = mx
    s = 0.0 if mx == 0 else (mx - mn) / mx
    return s, v


def clusters(img):
    """Dominant colours with the share of frame area each one covers."""
    from sklearn.cluster import MiniBatchKMeans
    px = img.reshape(-1, 3).astype(np.float32)
    if len(px) > 40000:
        px = px[np.random.default_rng(0).choice(len(px), 40000, replace=False)]
    # a frame of two flat colours must not be forced into K clusters
    n_distinct = len(np.unique((px // 8).astype(np.uint8), axis=0))
    k = int(min(K, max(2, n_distinct)))
    km = MiniBatchKMeans(n_clusters=k, n_init=4, random_state=0).fit(px)
    lab, cnt = np.unique(km.labels_, return_counts=True)
    out = []
    for l, n in zip(lab, cnt):
        share = n / len(px)
        if share < MIN_SHARE:
            continue
        c = km.cluster_centers_[l]
        s, v = hsv_of(c)
        out.append({"hex": hexof(c), "rgb": [int(x) for x in c],
                    "share": round(float(share), 4),
                    "sat": round(float(s), 3), "val": round(float(v), 3)})
    return sorted(out, key=lambda d: -d["share"])


def flatness(img):
    """Share of the frame covered by its few most common quantised colours.

    This is what separates 'flat vector' from 'photographic' objectively: flat
    art puts most of the frame inside a handful of exact colours, photography
    spreads it across thousands.
    """
    q = (img // 24 * 24).reshape(-1, 3)
    _, cnt = np.unique(q, axis=0, return_counts=True)
    cnt = np.sort(cnt)[::-1]
    return {"top3": round(float(cnt[:3].sum() / len(q)), 3),
            "top8": round(float(cnt[:8].sum() / len(q)), 3),
            "distinct": int(len(cnt))}


def linework(img):
    """How much of the frame is dark, hard edge — i.e. thick black outlines."""
    g = img.mean(axis=2)
    gx = np.abs(np.diff(g, axis=1))
    gy = np.abs(np.diff(g, axis=0))
    strong = ((gx > 60).sum() + (gy > 60).sum()) / (gx.size + gy.size)
    dark = (g < 60).mean()
    return {"edge_density": round(float(strong), 4), "dark_area": round(float(dark), 4)}


def caption_band(img):
    """Where text sits, from horizontal high-frequency energy per row.

    Lettering makes far more horizontal detail than flat shapes do, so on this
    kind of material the rows carrying type stand out clearly. Reported as a
    band with a confidence, never as a certainty — there is no OCR here.
    """
    g = img.mean(axis=2)
    e = np.abs(np.diff(g, axis=1)) > 45
    row = e.mean(axis=1)
    if row.max() <= 0:
        return None
    thr = max(row.mean() * 2.2, 0.06)
    rows = np.where(row > thr)[0]
    if len(rows) < 3:
        return None
    h = img.shape[0]
    return {"top": round(float(rows.min() / h), 3), "bottom": round(float(rows.max() / h), 3),
            "centre": round(float(rows.mean() / h), 3),
            "strength": round(float(row[rows].mean()), 3)}


# ---------------------------------------------------------------- OCR ----
# The band detector below infers text from horizontal-frequency energy, which
# cannot tell lettering from any other busy detail and cannot read a word. OCR
# replaces the guess with the actual caption: its words, its casing, its box,
# and the plate behind it — the plate being exactly the thing a reference clip
# reproduced for free that no written brief had ever described.

OCR_W = 720          # tesseract needs real pixels; 320 is too small to read
OCR_CONF = 55.0
# Where text SITS separates burned-in subtitles from an account's own display
# lettering far better than how big it is. Measured: @guijooorge's subtitle words
# all land at 90-95% frame height at 1.8-3.3% cap height, while @Barry's Economics
# sets "SEARCH THE BURRY INSIDE" at 34-45% height in caps. A cap-height rule
# called the second one a subtitle; a position rule does not.
SUBTITLE_BAND = 0.85    # text centred below this is almost certainly a subtitle


def _ppm(img, path):
    """Write a frame where tesseract can read it, without adding PIL."""
    h, w = img.shape[:2]
    with open(path, "wb") as f:
        f.write(b"P6\n%d %d\n255\n" % (w, h))
        f.write(img.tobytes())


def ocr_frame(path_video, t, tmpd):
    """Words, boxes and confidence for one frame."""
    p = _run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", path_video, "-frames:v", "1",
              "-vf", f"scale={OCR_W}:-2", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"])
    if not p.stdout:
        return None
    h = len(p.stdout) // (OCR_W * 3)
    if h == 0:
        return None
    img = np.frombuffer(p.stdout[:h * OCR_W * 3], np.uint8).reshape(h, OCR_W, 3)
    img, bars = letterbox(img)
    img = np.ascontiguousarray(img)
    ppm = os.path.join(tmpd, "f.ppm")
    _ppm(img, ppm)
    # psm 11 = sparse text: caption words sit alone on a flat ground, they are
    # not paragraphs, and the page-layout modes mangle them.
    r = _run(["tesseract", ppm, "stdout", "--psm", "11", "-l", "eng", "tsv"], text=True)
    words = []
    for line in r.stdout.splitlines()[1:]:
        c = line.split("\t")
        if len(c) < 12:
            continue
        txt = c[11].strip()
        try:
            conf = float(c[10])
        except ValueError:
            continue
        if not txt or conf < OCR_CONF or len(txt) < 2:
            continue
        words.append({"text": txt, "conf": conf, "x": int(c[6]), "y": int(c[7]),
                      "w": int(c[8]), "h": int(c[9])})
    if not words:
        return None
    # Burned-in auto-subtitles and the account's display lettering are different
    # objects and must not be averaged together. Cap height separates them
    # cleanly: the subtitle on these reels measures ~4.7% of frame height, the
    # display type many times that. Only display type describes a style.
    H = img.shape[0]
    disp = [w for w in words if (w["y"] + w["h"] / 2) / H < SUBTITLE_BAND]
    subs = [w for w in words if (w["y"] + w["h"] / 2) / H >= SUBTITLE_BAND]
    return {"img": img, "words": words, "display": disp, "subtitle": subs, "bars": bars}


def read_captions(path_video, times, tmpd):
    """Aggregate what the lettering actually is, across sampled frames."""
    frames, subs_only, H, W = [], [], None, None
    for t in times:
        o = ocr_frame(path_video, t, tmpd)
        if not o:
            continue
        img = o["img"]
        if o["subtitle"]:
            subs_only.append(" ".join(x["text"] for x in o["subtitle"]))
        words = o["display"]
        if not words:
            continue
        H, W = img.shape[0], img.shape[1]
        x0 = min(w["x"] for w in words); x1 = max(w["x"] + w["w"] for w in words)
        y0 = min(w["y"] for w in words); y1 = max(w["y"] + w["h"] for w in words)
        txt = " ".join(w["text"] for w in words)
        letters = [ch for ch in txt if ch.isalpha()]
        caps = sum(1 for ch in letters if ch.isupper()) / max(len(letters), 1)

        # Plate: the pixels inside the text box that are NOT glyph. If they sit
        # on one tight colour, the type is on a solid plate; if they scatter, it
        # is set straight onto the artwork.
        box = img[max(y0 - 4, 0):min(y1 + 4, H), max(x0 - 6, 0):min(x1 + 6, W)]
        plate = glyph = None
        if box.size:
            g = box.mean(axis=2)
            lo, hi = np.percentile(g, [25, 75])
            bg = box[g >= hi] if (g >= hi).sum() > (g <= lo).sum() else box[g <= lo]
            fg = box[g <= lo] if (g >= hi).sum() > (g <= lo).sum() else box[g >= hi]
            if len(bg) > 20:
                plate = {"hex": hexof(bg.mean(axis=0)),
                         "uniform": round(float(1.0 - min(bg.std(axis=0).mean() / 60.0, 1.0)), 3)}
            if len(fg) > 20:
                glyph = hexof(fg.mean(axis=0))
            # stroke weight: glyph pixels as a share of the text box
            weight = round(float(min((g <= lo).mean(), (g >= hi).mean()) * 2), 3)
        else:
            weight = None

        frames.append({
            "t": round(float(t), 2), "text": txt, "words": len(words),
            "conf": round(float(np.mean([w["conf"] for w in words])), 1),
            "caps_ratio": round(caps, 2),
            "box": {"top": round(y0 / H, 3), "bottom": round(y1 / H, 3),
                    "left": round(x0 / W, 3), "right": round(x1 / W, 3),
                    "height": round((y1 - y0) / H, 3), "width": round((x1 - x0) / W, 3)},
            "cap_height": round(float(np.median([w["h"] for w in words]) / H), 3),
            "plate": plate, "glyph": glyph, "stroke": weight,
        })
    return frames, subs_only


def summarise_ocr(frames, dur):
    if not frames:
        return None
    caps = float(np.mean([f["caps_ratio"] for f in frames]))
    centres = [(f["box"]["top"] + f["box"]["bottom"]) / 2 for f in frames]
    lefts = [f["box"]["left"] for f in frames]
    rights = [f["box"]["right"] for f in frames]
    plates = [f["plate"] for f in frames if f["plate"]]
    uniform = float(np.mean([p["uniform"] for p in plates])) if plates else 0.0
    # centred if the margins match on both sides
    margin_gap = float(np.mean([abs(l - (1 - r)) for l, r in zip(lefts, rights)]))
    align = ("centred" if margin_gap < 0.06 else
             "left-set" if np.mean(lefts) < 0.15 else "varies")
    texts = [f["text"] for f in frames]
    return {
        "frames_with_text": len(frames),
        "mean_confidence": round(float(np.mean([f["conf"] for f in frames])), 1),
        "casing": ("ALL CAPS" if caps > 0.9 else "mostly caps" if caps > 0.6
                   else "sentence case"),
        "caps_ratio": round(caps, 2),
        "words_on_screen": round(float(np.mean([f["words"] for f in frames])), 1),
        "centre_height": round(float(np.mean(centres)), 3),
        "band": "lower third" if np.mean(centres) > 0.62 else
                "upper third" if np.mean(centres) < 0.38 else "centre band",
        "position_stable": round(float(np.std(centres)), 3) < 0.08,
        "alignment": align,
        "cap_height_pct": round(float(np.mean([f["cap_height"] for f in frames])) * 100, 1),
        "line_width_pct": round(float(np.mean([f["box"]["width"] for f in frames])) * 100, 1),
        "plate": ({"hex": plates[0]["hex"], "uniformity": round(uniform, 2)}
                  if plates and uniform > 0.55 else None),
        "glyph_colour": frames[0]["glyph"],
        "stroke_weight": round(float(np.mean([f["stroke"] for f in frames
                                              if f["stroke"] is not None])), 3)
                          if any(f["stroke"] is not None for f in frames) else None,
        "changes_per_min": (round(len(set(texts)) / dur * 60, 1) if dur else None),
        "samples": texts[:6],
    }


def visual_attributes(img):
    """Texture, depth, shape and colour structure — beyond palette and letters.

    OCR localises the type; these describe the SURFACE the type sits on. Each is
    a measurement with a stated proxy, not an adjective: the point is that
    "textured", "flat", "geometric" and "soft" stop being words that fit any
    material and become numbers that separate one from another.
    """
    g = img.mean(axis=2).astype(np.float32)
    h, w = g.shape
    a = {}

    # TEXTURE — high-frequency residual after a 3x3 box blur. Grain and paper
    # tooth survive it; clean vector fills do not.
    k = np.ones((3, 3), np.float32) / 9.0
    pad = np.pad(g, 1, mode="edge")
    blur = sum(pad[i:i + h, j:j + w] * k[i, j] for i in range(3) for j in range(3))
    resid = np.abs(g - blur)
    edges = resid > 18                      # real edges, not grain
    grain = float(resid[~edges].mean()) if (~edges).any() else 0.0
    a["grain"] = round(grain, 2)
    a["grain_class"] = ("clean — no texture" if grain < 1.2 else
                        "light grain" if grain < 3.0 else
                        "visible grain or paper tooth" if grain < 6.0 else
                        "heavy texture / photographic detail")

    # DEPTH 1 — gradients. A flat fill has a constant interior; a ramped fill
    # drifts steadily without ever crossing an edge.
    inner = ~edges
    gx = np.abs(np.diff(g, axis=1))[:, :w - 1]
    slow = (gx > 0.8) & (gx < 6) & inner[:, :w - 1]
    a["gradient_area"] = round(float(slow.mean()), 4)
    a["fills"] = ("flat, constant colour" if slow.mean() < 0.06 else
                  "some ramping" if slow.mean() < 0.16 else "gradient-filled")

    # DEPTH 2 — drop shadows: a dark band sitting just below a bright edge.
    dy = np.diff(g, axis=0)
    down_dark = (dy < -25)[:h - 3]
    below = g[2:h - 1] < g[1:h - 2]
    a["shadow_score"] = round(float((down_dark & below[:len(down_dark)]).mean()), 4)
    a["depth"] = ("no shadowing — single flat plane" if a["shadow_score"] < 0.012 else
                  "light offset shadowing" if a["shadow_score"] < 0.03 else
                  "layered with visible shadows")

    # SHAPE — orientation of edges. Geometric artwork piles onto horizontal and
    # vertical; organic drawing spreads across the diagonals.
    ex = np.diff(g, axis=1)[:h - 1, :]
    ey = np.diff(g, axis=0)[:, :w - 1]
    mag = np.hypot(ex, ey)
    m = mag > 20
    if m.sum() > 50:
        ang = (np.degrees(np.arctan2(ey[m], ex[m])) + 180) % 180
        near_axis = float((((ang < 12) | (ang > 168)) | ((ang > 78) & (ang < 102))).mean())
        a["axis_aligned"] = round(near_axis, 3)
        a["shape_language"] = ("strictly geometric — horizontals and verticals"
                               if near_axis > 0.55 else
                               "geometric with curves" if near_axis > 0.35 else
                               "organic, freely angled")
        a["edge_sharpness"] = round(float(mag[m].mean()), 1)
    else:
        a["axis_aligned"] = None; a["shape_language"] = "too few edges to judge"
        a["edge_sharpness"] = None

    # COLOUR STRUCTURE — hue families and temperature.
    px = img.reshape(-1, 3).astype(np.float32)
    mx = px.max(axis=1); mn = px.min(axis=1)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)
    col = px[sat > 0.25]
    if len(col) > 100:
        r, gg, b = col[:, 0], col[:, 1], col[:, 2]
        mxc = col.max(axis=1); mnc = col.min(axis=1); d = np.maximum(mxc - mnc, 1)
        hue = np.where(mxc == r, (gg - b) / d % 6,
                       np.where(mxc == gg, (b - r) / d + 2, (r - gg) / d + 4)) * 60
        hist, _ = np.histogram(hue % 360, bins=12, range=(0, 360))
        a["hue_families"] = int((hist > len(col) * 0.06).sum())
        warm = float(((hue < 90) | (hue > 300)).mean())
        a["warm_ratio"] = round(warm, 3)
        a["temperature"] = ("warm" if warm > 0.65 else "cool" if warm < 0.35 else "split")
    else:
        a["hue_families"] = 0; a["warm_ratio"] = None; a["temperature"] = "near-neutral"
    return a


def _mean_attrs(rows):
    """Average the numbers; take the majority verdict on the words."""
    out = {}
    for k in rows[0]:
        vals = [r[k] for r in rows if r.get(k) is not None]
        if not vals:
            out[k] = None
        elif isinstance(vals[0], str):
            out[k] = max(set(vals), key=vals.count)
        else:
            out[k] = round(float(np.mean(vals)), 4)
    return out


def measure(path):
    dur, cuts, times = sample_times(path)
    raw = frame_raw(path, (times[0] if times else 1.0))
    bars = letterbox(raw)[1] if raw is not None else None
    frames = []
    for t in times:
        img = frame(path, t)
        if img is None:
            continue
        frames.append({"t": round(float(t), 2), "clusters": clusters(img),
                       "flatness": flatness(img), "linework": linework(img),
                       "caption": caption_band(img), "attrs": visual_attributes(img)})
    if not frames:
        return {"error": "no frames decoded", "file": path}

    with tempfile.TemporaryDirectory() as _td:
        _disp, _subs = read_captions(path, times, _td)
        ocr = summarise_ocr(_disp, dur) or {}
        subs = [t for t in _subs if t]
        # Reported, never silently dropped: on an account that genuinely sets its
        # captions low, this band IS the caption style, and the operator needs to
        # see it to make that call.
        ocr["bottom_band_text"] = ({"samples": subs[:3], "frames": len(subs),
                                    "reading": "below %d%% frame height — burned-in "
                                    "subtitle unless this account sets captions low"
                                    % int(SUBTITLE_BAND * 100)} if subs else None)
        if not _disp:
            ocr["display_type"] = None
            ocr["note"] = ("No display lettering found above the subtitle band."
                           if subs else "No text found at all.")

    grounds = [f["clusters"][0] for f in frames if f["clusters"]]
    gh = [g["hex"] for g in grounds]
    distinct_grounds = len(set(gh))
    # The royal-blue lesson, as a computed verdict rather than an impression.
    if len(frames) < 2:
        verdict = "UNKNOWN — one usable frame. Do not state a palette from this."
    elif distinct_grounds == 1:
        verdict = ("FIXED — every sampled scene shares one ground colour. "
                   "Still only %d scenes; say so." % len(frames))
    elif distinct_grounds >= max(2, len(frames) * 0.6):
        verdict = ("PER-SCENE — the ground changes shot to shot (%d different "
                   "grounds across %d scenes). Write it as a rule, not a hex value."
                   % (distinct_grounds, len(frames)))
    else:
        verdict = ("MIXED — %d grounds across %d scenes; a small set is reused."
                   % (distinct_grounds, len(frames)))

    pool = {}
    for f in frames:
        for c in f["clusters"]:
            e = pool.setdefault(c["hex"], {"hex": c["hex"], "seen": 0, "area": 0.0,
                                           "sat": c["sat"], "val": c["val"]})
            e["seen"] += 1
            e["area"] += c["share"]
    for e in pool.values():
        e["mean_share"] = round(e["area"] / len(frames), 4)
        e["in_scenes"] = "%d/%d" % (e["seen"], len(frames))
        del e["area"]
    palette = sorted(pool.values(), key=lambda d: -d["mean_share"])

    caps = [f["caption"] for f in frames if f["caption"]]
    cap = None
    if caps:
        cap = {"present_in": "%d/%d" % (len(caps), len(frames)),
               "centre_height": round(float(np.mean([c["centre"] for c in caps])), 3),
               "spread": round(float(np.std([c["centre"] for c in caps])), 3)}

    return {
        "file": os.path.basename(path), "duration": round(dur, 2),
        "letterbox": bars,
        "scenes_detected": len(cuts) + 1, "frames_measured": len(frames),
        "cuts_per_min": round(len(cuts) / dur * 60, 1) if dur else None,
        "ground_verdict": verdict,
        "grounds": [{"t": f["t"], "hex": f["clusters"][0]["hex"],
                     "share": f["clusters"][0]["share"]} for f in frames if f["clusters"]],
        "palette": palette,
        "flatness": {k: round(float(np.mean([f["flatness"][k] for f in frames])), 3)
                     for k in ("top3", "top8", "distinct")},
        "linework": {k: round(float(np.mean([f["linework"][k] for f in frames])), 4)
                     for k in ("edge_density", "dark_area")},
        "mean_saturation": round(float(np.mean(
            [sum(c["sat"] * c["share"] for c in f["clusters"]) for f in frames])), 3),
        "caption": cap,
        "attrs": _mean_attrs([f["attrs"] for f in frames]),
        "ocr": ocr,
        "frames": frames,
    }


def medium_from(m):
    """Name the medium from numbers, not adjectives.

    'Rough edges, warm palette, shallow depth' described a photographic collage
    and produced a vector illustration, because those words fit both. Flatness
    does not fit both.
    """
    f, lw = m["flatness"], m["linework"]
    if f["top3"] >= 0.75:
        return ("flat vector illustration — solid colour fills"
                + (", thick dark outlines" if lw["edge_density"] > 0.02
                   else ", black linework and solid black shapes" if lw["dark_area"] > 0.12
                   else ", minimal linework"),
                "top 3 colours cover %.0f%% of frame" % (f["top3"] * 100))
    if f["top3"] >= 0.45:
        return ("flat illustration with texture or grain — mostly solid fills, "
                "broken up", "top 3 colours cover %.0f%%" % (f["top3"] * 100))
    if f["top8"] >= 0.45:
        return ("illustrated with photographic elements or heavy texture",
                "top 8 colours cover only %.0f%%" % (f["top8"] * 100))
    return ("photographic — continuous tone, no flat fills",
            "top 8 colours cover %.0f%%, %d distinct" % (f["top8"] * 100, f["distinct"]))


def write_brief(agg):
    """The paste-ready block. Written from measurements, in the shape that worked."""
    L = []
    med, why = medium_from(agg)
    L.append("MEDIUM: %s. [%s]" % (med, why))
    L.append("")
    v = agg["ground_verdict"]
    # The prose must be gated on the MEASUREMENTS, not just the verdict.
    # Written unconditionally, "one saturated flat colour fills the whole
    # background, never a textured ground" was emitted for archival collage —
    # material whose dominant tone covers 40% of frame at 0.29 saturation and
    # whose whole point is texture. That is the same generic-language failure
    # that made a written brief produce the wrong medium in the first place.
    ex = agg["ground_examples"]
    mean_ground = sum(g["share"] for g in ex[:5]) / max(len(ex[:5]), 1)
    flat = agg["flatness"]["top3"] >= 0.75 and mean_ground >= 0.60
    cols = ", ".join("%s (%.0f%%)" % (g["hex"], g["share"] * 100) for g in ex[:5])
    if flat:
        fill, rule = ("one saturated flat colour fills the whole background",
                      " Never a gradient, never a textured ground, never more than "
                      "about three colours in frame.")
    else:
        fill, rule = ("one colour dominates the frame, covering roughly %.0f%% of it"
                      % (mean_ground * 100), "")
    if v.startswith("PER-SCENE"):
        L.append("COLOUR SYSTEM: %s, a DIFFERENT one per scene, chosen for the beat — "
                 "not a fixed brand colour. Measured grounds: %s. Against it: %s.%s"
                 % (fill, cols, agg["against_ground"], rule))
    elif v.startswith("FIXED"):
        g = ex[0]
        L.append("COLOUR SYSTEM: a single constant ground, %s, covering ~%.0f%% of frame "
                 "in every scene measured. Against it: %s.%s"
                 % (g["hex"], g["share"] * 100, agg["against_ground"], rule))
    else:
        L.append("COLOUR SYSTEM: %s %s. Measured grounds: %s. Against it: %s."
                 % (v, fill, cols, agg["against_ground"]))
    L.append("")
    if agg["mean_saturation"] > 0.55:
        L.append("Colour is fully saturated — flat and loud, not muted.")
    elif agg["mean_saturation"] < 0.25:
        L.append("Colour is desaturated throughout; saturation ~%.2f measured."
                 % agg["mean_saturation"])
    if agg["linework"]["edge_density"] > 0.02 or agg["linework"]["dark_area"] > 0.12:
        L.append("LINEWORK: dark outlines and solid black shapes — %.1f%% of the "
                 "frame is strong edge and %.0f%% is near-black."
                 % (agg["linework"]["edge_density"] * 100,
                    agg["linework"]["dark_area"] * 100))
    cap = agg.get("caption")
    if cap:
        pos = ("lower third" if cap["centre_height"] > 0.62 else
               "upper third" if cap["centre_height"] < 0.38 else "centre band")
        L.append("CAPTIONS: type appears in %s, sitting in the %s "
                 "(centre at %.0f%% height%s)."
                 % (cap["present_in"], pos, cap["centre_height"] * 100,
                    ", consistent" if cap["spread"] < 0.08 else ", position varies"))
    a = agg.get("attrs") or {}
    if a:
        L.append("")
        mat = ["SURFACE: %s" % a.get("grain_class", "?"),
               "fills are %s" % a.get("fills", "?"),
               a.get("depth", "?"),
               a.get("shape_language", "?")]
        L.append("; ".join(mat) + ".")
        if a.get("hue_families"):
            L.append("Colour is %s, built from about %d hue famil%s — do not widen it."
                     % (a.get("temperature", "?"), round(a["hue_families"]),
                        "y" if round(a["hue_families"]) == 1 else "ies"))
        if a.get("edge_sharpness"):
            L.append("Edges are hard and clean [contrast %.0f across an edge]."
                     % a["edge_sharpness"] if a["edge_sharpness"] > 45 else
                     "Edges are soft [contrast %.0f across an edge]." % a["edge_sharpness"])

    t = agg.get("type") or {}
    if t.get("casing"):
        L.append("")
        bits = ["TYPOGRAPHY: display lettering is %s" % t["casing"].lower(),
                "set in the %s" % t.get("band", "?"),
                "%s" % t.get("alignment", "?"),
                "cap height about %.0f%% of frame height" % (t.get("cap_height_pct") or 0),
                "around %.0f words on screen at once" % (t.get("words_on_screen") or 0)]
        L.append(", ".join(bits) + ".")
        if t.get("plate"):
            L.append("Type sits on a SOLID PLATE %s [uniformity %.2f], glyphs %s. "
                     "The plate is part of the design — reproduce it."
                     % (t["plate"]["hex"], t["plate"]["uniformity"],
                        t.get("glyph_colour") or "?"))
        else:
            L.append("Type is set straight onto the artwork with no plate behind it.")
        if t.get("stroke_weight") and t["stroke_weight"] > 0.45:
            L.append("Letterforms are heavy — glyph strokes fill %.0f%% of the type box."
                     % (t["stroke_weight"] * 100))
        L.append("Read from the clips: %s [OCR confidence %.0f%%]."
                 % ("; ".join(repr(x) for x in (t.get("samples") or [])[:3]),
                    t.get("mean_confidence") or 0))
    elif t.get("bottom_band"):
        L.append("")
        L.append("TYPOGRAPHY: no display lettering found. The only text is in the "
                 "bottom band (%s) and reads as burned-in subtitles, NOT the "
                 "account's own captions — do not copy it into the style."
                 % t["bottom_band"]["in_clips"])

    if agg.get("cuts_per_min"):
        L.append("")
        L.append("PACING: %.0f cuts per minute measured across the reference clips."
                 % agg["cuts_per_min"])
    return "\n".join(L)


def _merge_ocr(rows):
    """Combine typography across clips, keeping display type and the bottom band
    apart — they are different objects and averaging them describes neither."""
    rows = [r for r in rows if r]
    disp = [r for r in rows if r.get("display_type", "x") is not None and r.get("casing")]
    bottom = [r["bottom_band_text"] for r in rows if r.get("bottom_band_text")]
    out = {"clips_with_display_type": "%d/%d" % (len(disp), len(rows))}
    if disp:
        caps = [r["caps_ratio"] for r in disp]
        plates = [r["plate"] for r in disp if r.get("plate")]
        out.update({
            "casing": max(set(r["casing"] for r in disp),
                          key=[r["casing"] for r in disp].count),
            "caps_ratio": round(float(np.mean(caps)), 2),
            "cap_height_pct": round(float(np.mean([r["cap_height_pct"] for r in disp])), 1),
            "line_width_pct": round(float(np.mean([r["line_width_pct"] for r in disp])), 1),
            "band": max(set(r["band"] for r in disp), key=[r["band"] for r in disp].count),
            "alignment": max(set(r["alignment"] for r in disp),
                             key=[r["alignment"] for r in disp].count),
            "words_on_screen": round(float(np.mean([r["words_on_screen"] for r in disp])), 1),
            "stroke_weight": round(float(np.mean([r["stroke_weight"] for r in disp
                                                  if r.get("stroke_weight")])), 3)
                              if any(r.get("stroke_weight") for r in disp) else None,
            "plate": ({"hex": plates[0]["hex"],
                       "uniformity": round(float(np.mean([p["uniformity"] for p in plates])), 2)}
                      if plates else None),
            "glyph_colour": next((r.get("glyph_colour") for r in disp if r.get("glyph_colour")), None),
            "mean_confidence": round(float(np.mean([r["mean_confidence"] for r in disp])), 1),
            "samples": [t for r in disp for t in (r.get("samples") or [])][:6],
        })
    if bottom:
        out["bottom_band"] = {"in_clips": "%d/%d" % (len(bottom), len(rows)),
                              "samples": [t for b in bottom for t in b["samples"]][:4],
                              "reading": bottom[0]["reading"]}
    return out


def aggregate(results):
    """Combine clips. n=1 is reported as n=1, never smoothed into a fact."""
    good = [r for r in results if not r.get("error")]
    if not good:
        return {"error": "nothing measurable"}
    grounds = [g for r in good for g in r["grounds"]]
    gh = [g["hex"] for g in grounds]
    n_sc = len(grounds)
    distinct = len(set(gh))
    if len(good) < 2:
        note = ("SINGLE CLIP — %d scenes from one clip. Enough to describe this clip, "
                "NOT enough to state the account's system. Measure at least 3 clips "
                "from different videos before writing a style." % n_sc)
    else:
        note = "%d clips, %d scenes." % (len(good), n_sc)
    # "how many distinct grounds" moves with the merge tolerance. "does ONE
    # ground dominate" is the question that actually separates a fixed brand
    # colour from a colour chosen per scene, and it is stable.
    merged = merge_near([{"hex": h, "mean_share": 1.0 / max(n_sc, 1)} for h in gh])
    distinct = len(merged)
    top_frac = max((m["mean_share"] for m in merged), default=0.0)
    if n_sc < 2:
        verdict = "UNKNOWN — %d scene(s). Do not state a palette." % n_sc
    elif top_frac >= 0.8:
        verdict = ("FIXED — one ground colour carries %.0f%% of %d scenes."
                   % (top_frac * 100, n_sc))
    elif top_frac < 0.5:
        verdict = ("PER-SCENE — %d distinct grounds across %d scenes, the most common "
                   "covering only %.0f%% of them. Write it as a RULE, not a hex value."
                   % (distinct, n_sc, top_frac * 100))
    else:
        verdict = ("MIXED — %d grounds across %d scenes; one is reused in %.0f%%."
                   % (distinct, n_sc, top_frac * 100))

    pool = {}
    for r in good:
        for c in r["palette"]:
            e = pool.setdefault(c["hex"], {"hex": c["hex"], "sat": c["sat"],
                                           "val": c["val"], "share": 0.0, "clips": 0})
            e["share"] += c["mean_share"]; e["clips"] += 1
    for e in pool.values():
        e["mean_share"] = round(e["share"] / len(good), 4); del e["share"]
        e["in_clips"] = "%d/%d" % (e["clips"], len(good)); del e["clips"]
    palette = merge_near(sorted(pool.values(), key=lambda d: -d["mean_share"]))
    for e in palette:
        n = e.pop("_n", 1)
        if n > 1:
            e["merged_from"] = n     # this row absorbed n near-identical samples
        e.pop("_rgb", None)
    palette.sort(key=lambda d: -d["mean_share"])

    ground_hexes = set(gh)
    non_ground = [p for p in palette if p["hex"] not in ground_hexes][:4]
    def role(p):
        if p["val"] < 0.25: return "black linework"
        if p["val"] > 0.85 and p["sat"] < 0.15: return "white"
        return "accent %s" % p["hex"]
    against = ", ".join(role(p) for p in non_ground) or "no consistent secondary colours"

    seen, ex = set(), []
    for g in sorted(grounds, key=lambda d: -d["share"]):
        if g["hex"] in seen:
            continue
        gc = np.array([int(g["hex"][i:i + 2], 16) for i in (1, 3, 5)], float)
        if any(np.linalg.norm(gc - np.array([int(e["hex"][i:i + 2], 16)
                                             for i in (1, 3, 5)], float)) < 26.0 for e in ex):
            continue
        seen.add(g["hex"]); ex.append(g)

    cpm = [r["cuts_per_min"] for r in good if r.get("cuts_per_min")]
    caps = [r["caption"] for r in good if r.get("caption")]
    agg = {
        "clips_measured": len(good), "scenes": n_sc, "note": note,
        "ground_verdict": verdict, "ground_examples": ex, "palette": palette,
        "against_ground": against,
        "flatness": {k: round(float(np.mean([r["flatness"][k] for r in good])), 3)
                     for k in ("top3", "top8", "distinct")},
        "linework": {k: round(float(np.mean([r["linework"][k] for r in good])), 4)
                     for k in ("edge_density", "dark_area")},
        "mean_saturation": round(float(np.mean([r["mean_saturation"] for r in good])), 3),
        "cuts_per_min": round(float(np.mean(cpm)), 1) if cpm else None,
        "caption": ({"present_in": "%d/%d clips" % (len(caps), len(good)),
                     "centre_height": round(float(np.mean([c["centre_height"] for c in caps])), 3),
                     "spread": round(float(np.mean([c["spread"] for c in caps])), 3)}
                    if caps else None),
        "attrs": _mean_attrs([r["attrs"] for r in good if r.get("attrs")])
                 if any(r.get("attrs") for r in good) else None,
        "type": _merge_ocr([r.get("ocr") for r in good]),
        "letterbox": next((r.get("letterbox") for r in good if r.get("letterbox")), None),
        "per_clip": [{k: r[k] for k in ("file", "ground_verdict", "grounds",
                                        "frames_measured")} for r in good],
    }
    agg["brief"] = write_brief(agg)
    return agg


def compare(ref, out):
    """Did the generated video actually land on the reference?

    This is the step that closes the loop. Judging a render by eye is how a
    style ended up recording one sample\'s blue as the palette; the same
    measurements that describe the reference can score the output against it,
    so "better" is a number and not an impression.
    """
    rows, verdict = [], []

    def cmp(label, a, b, tol, fmt="%.2f", higher_worse=None):
        d = abs(a - b)
        ok = d <= tol
        rows.append((label, fmt % a, fmt % b, ("match" if ok else "OFF")))
        if not ok:
            verdict.append("%s: reference %s, output %s" % (label, fmt % a, fmt % b))
        return ok

    cmp("flatness (top3)", ref["flatness"]["top3"], out["flatness"]["top3"], 0.12)
    cmp("saturation", ref["mean_saturation"], out["mean_saturation"], 0.15)
    cmp("near-black area", ref["linework"]["dark_area"], out["linework"]["dark_area"], 0.12)
    cmp("ground share", sum(g["share"] for g in ref["ground_examples"][:5]) / 5.0,
        sum(g["share"] for g in out["ground_examples"][:5]) / 5.0, 0.15)

    # palette overlap: did the output reach ANY of the reference's grounds?
    def rgb(h): return np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)], float)
    rg = [rgb(g["hex"]) for g in ref["ground_examples"][:8]]
    hits = [g for g in out["ground_examples"][:8]
            if any(np.linalg.norm(rgb(g["hex"]) - r) < 60 for r in rg)]
    rows.append(("grounds reaching reference", "%d" % len(rg),
                 "%d" % len(hits), "match" if hits else "OFF"))
    if not hits:
        verdict.append("output reached none of the reference's ground colours")

    rv, ov = ref["ground_verdict"].split(" ")[0], out["ground_verdict"].split(" ")[0]
    rows.append(("colour system", rv, ov, "match" if rv == ov else "OFF"))
    if rv != ov:
        verdict.append("colour system: reference is %s, output is %s" % (rv, ov))

    print("\n%-28s %-14s %-14s %s" % ("", "REFERENCE", "OUTPUT", ""))
    print("-" * 72)
    for r in rows:
        print("%-28s %-14s %-14s %s" % r)
    print("-" * 72)
    if verdict:
        print("\nNOT YET MATCHED — carry these into the next prompt:")
        for v in verdict:
            print("  * %s" % v)
    else:
        print("\nMATCHED on every measure. Write the style from the OUTPUT.")
    return not verdict


def fetch(src, tmpd):
    if src.startswith("http"):
        dst = os.path.join(tmpd, os.path.basename(src.split("?")[0]))
        urllib.request.urlretrieve(src, dst)
        return dst
    return src


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("clips", nargs="*", help="clip files or URLs")
    ap.add_argument("--style", help="measure the reference clip of this render style")
    ap.add_argument("--channel", help="measure registered reference clips for this account")
    ap.add_argument("--limit", type=int, default=4, help="clips to measure for --channel")
    ap.add_argument("--json", help="write full measurements here")
    ap.add_argument("--compare", nargs=2, metavar=("REFERENCE", "OUTPUT"),
                    help="measure both and score the output against the reference")
    a = ap.parse_args()

    if a.compare:
        with tempfile.TemporaryDirectory() as tmpd:
            pair = []
            for s_ in a.compare:
                q = fetch(s_, tmpd)
                print("  measuring %s ..." % os.path.basename(q), flush=True)
                pair.append(aggregate([measure(q)]))
        return 0 if compare(pair[0], pair[1]) else 2

    srcs = list(a.clips)
    if a.style or a.channel:
        sys.argv = [sys.argv[0]]
        sys.path.insert(0, "/root/bulk-transcriber")
        import app as A
        if a.style:
            st = next((s for s in A._api_get("/api/render-styles")["styles"]
                       if s["slug"] == a.style), None)
            if not st or not st.get("reference_url"):
                print("no reference clip on style %r" % a.style); return 1
            srcs.append(st["reference_url"])
        if a.channel:
            cl = A._api_get("/api/reference-clips")["clips"]
            want = a.channel.lower().lstrip("@")
            hit = [c for c in cl if (c["channel_name"] or "").lower().lstrip("@") == want]
            if not hit:
                print("no registered clips for %r" % a.channel); return 1
            # spread across DIFFERENT source videos — three clips from one video
            # is still one video, and the n>=2 rule is about videos.
            bysrc, picked = {}, []
            for c in hit:
                bysrc.setdefault(c["source_file"], []).append(c)
            for k in sorted(bysrc):
                picked.append(bysrc[k][0])
            srcs += [c["url"] for c in picked[:a.limit]]
            print("%d clips for %s across %d videos; measuring %d"
                  % (len(hit), a.channel, len(bysrc), min(a.limit, len(picked))))
    if not srcs:
        ap.error("give clips, --style or --channel")

    with tempfile.TemporaryDirectory() as tmpd:
        results = []
        for s in srcs:
            p = fetch(s, tmpd)
            print("  measuring %s ..." % os.path.basename(p), flush=True)
            results.append(measure(p))
    agg = aggregate(results)
    if a.json:
        json.dump({"aggregate": agg, "clips": results}, open(a.json, "w"), indent=2,
                  default=float)
        print("\nfull measurements -> %s" % a.json)

    print("\n" + "=" * 72)
    print("MEASURED  %s" % agg.get("note", ""))
    print("=" * 72)
    print("  ground     : %s" % agg["ground_verdict"])
    for g in agg["ground_examples"][:6]:
        print("               %s  %.0f%% of frame" % (g["hex"], g["share"] * 100))
    print("  flatness   : top3 %.0f%%  top8 %.0f%%  (%d distinct)"
          % (agg["flatness"]["top3"] * 100, agg["flatness"]["top8"] * 100,
             agg["flatness"]["distinct"]))
    print("  linework   : %.1f%% strong edge, %.0f%% near-black"
          % (agg["linework"]["edge_density"] * 100, agg["linework"]["dark_area"] * 100))
    print("  saturation : %.2f" % agg["mean_saturation"])
    if agg.get("cuts_per_min"): print("  pacing     : %.0f cuts/min" % agg["cuts_per_min"])
    if agg.get("caption"): print("  captions   : %s, centre %.0f%% height"
                                 % (agg["caption"]["present_in"],
                                    agg["caption"]["centre_height"] * 100))
    print("\n  palette (area-weighted, across clips):")
    for p in agg["palette"][:8]:
        print("    %s  %5.1f%% mean area   in %s clips  sat %.2f"
              % (p["hex"], p["mean_share"] * 100, p["in_clips"], p["sat"]))
    print("\n" + "-" * 72)
    print("BRIEF — paste into the style's prompt_prefix:")
    print("-" * 72)
    print(agg["brief"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
