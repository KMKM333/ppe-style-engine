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
    return np.frombuffer(buf[:h * W * 3], np.uint8).reshape(h, W, 3)


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


def measure(path):
    dur, cuts, times = sample_times(path)
    frames = []
    for t in times:
        img = frame(path, t)
        if img is None:
            continue
        frames.append({"t": round(float(t), 2), "clusters": clusters(img),
                       "flatness": flatness(img), "linework": linework(img),
                       "caption": caption_band(img)})
    if not frames:
        return {"error": "no frames decoded", "file": path}

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
    if agg.get("cuts_per_min"):
        L.append("PACING: %.0f cuts per minute measured across the reference clips."
                 % agg["cuts_per_min"])
    return "\n".join(L)


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
