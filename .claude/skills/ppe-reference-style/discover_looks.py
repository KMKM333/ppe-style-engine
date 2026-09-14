#!/usr/bin/env python3
"""discover_looks.py — find each account's recurring LOOKS from its own footage.

Johnny Harris's ten shot types were found by hand: contact sheets, eyes, cuts.
Every other account has clips filed by account, not by look, so Option B
(Seedance from the account's real clip for THAT kind of shot) can only run for
him. This does the same discovery for every account, from their real videos:

  1. contact sheet per source video (frames every ~8s, real footage);
  2. one vision call per account: name the 4-8 recurring visual treatments,
     each with a `when` rule and the (video, timestamp) frames that show it;
  3. cut an 8s real clip per look at the model's own timestamps, and a still;
  4. write shot_types on that account's render style (created if missing),
     motion_clip + reference_images pointing at the cut footage.

Nothing is generated. The stills and clips are the account's own frames.
Every cut is re-checked: a clip whose middle frame the model does not
classify as that look is dropped, not registered.

Usage:  discover_looks.py [--accounts a,b] [--dry-run] [--limit-videos N]
"""
import argparse, base64, glob, json, os, re, subprocess, sys, time, urllib.error, urllib.request

sys.path.insert(0, "/root/bulk-transcriber")
_ARGS = sys.argv[1:]; sys.argv = [sys.argv[0]]
import app as A

BASE = "https://ppe-style-engine.onrender.com"
OPENAI_KEY = os.environ.get("TRANSCRIBE_API_KEY", "")
VISION_MODEL = os.environ.get("LOOK_MODEL", "gpt-4o")
CACHE = "/root/bulk-transcriber/video_cache"
LF = "/root/bulk-transcriber/lfvids"
OUT = "/root/bulk-transcriber/looks"
os.makedirs(OUT, exist_ok=True)


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def dur(p):
    o = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p]).stdout.strip()
    try: return float(o)
    except ValueError: return 0.0


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower().replace("’", "").replace("'", "")).strip("-")


def source_path(c):
    stem = (c.get("source_file") or "").rsplit(".", 1)[0]
    for d in (CACHE, LF):
        p = os.path.join(d, stem + ".mp4")
        if os.path.exists(p): return p
    return None


def sheet(video, out, every=8.0, cols=6):
    d = dur(video)
    n = max(4, min(24, int(d / every)))
    step = max(1.0, (d - 4) / n)
    ts = [round(2 + i * step, 1) for i in range(n)]
    rows = (n + cols - 1) // cols
    # fps sampling from t=2s: frame k lands at 2 + k*step, matching ts exactly
    sh(["ffmpeg", "-v", "error", "-y", "-ss", "2", "-i", video, "-vf",
        f"fps=1/{step:.3f},scale=320:-2,tile={cols}x{rows}", "-frames:v", "1", out])
    return ts


def vision(messages, timeout=180):
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps({"model": VISION_MODEL, "temperature": 0.2,
                         "response_format": {"type": "json_object"},
                         "messages": messages}).encode(),
        headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(json.loads(r.read().decode())["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(20 * (attempt + 1)); continue
            raise


def img_part(path):
    b = base64.b64encode(open(path, "rb").read()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}", "detail": "low"}}


DISCOVER = (
    "You are a picture editor cataloguing ONE creator's recurring visual treatments from contact "
    "sheets of their real videos. Each sheet is one video; frames run left-to-right, top-to-bottom, "
    "at the timestamps listed. Name the 4 to 8 RECURRING looks — a look is a way a shot is built "
    "(e.g. 'talking head at desk', 'screen recording with highlight', 'flat-colour illustration with "
    "one figure', 'archival photo on black', 'map with badges', 'chart on paper'). Ignore one-offs. "
    "For each look give: key (kebab-case), label, `when` (the editorial condition for using it, one "
    "sentence), a 2-3 sentence physical description of what is in frame (medium, ground, colour, "
    "how figures/text appear), motion (one of: none, push, pan, wipe, cut-heavy), and `examples`: "
    "2-4 objects {video: <sheet index 0-based>, t: <timestamp seconds from that sheet's list>} "
    "pointing at frames that clearly show it. Mark the most common look default:true. Return JSON: "
    '{"looks":[...]}'
)
CHECK = (
    "This single frame is from a creator's video. Which ONE of these looks is it, or none? "
    "Return JSON {\"key\": <key or \"none\">, \"confidence\": 0-1}. Looks: "
)


def discover(account, clips, limit_videos):
    srcs = {}
    for c in clips:
        p = source_path(c)
        if p: srcs.setdefault(p, c)
    vids = list(srcs)[:limit_videos]
    if not vids:
        print(f"  {account}: no source videos on disk — skipped"); return None
    parts = [{"type": "text", "text": DISCOVER}]
    meta = []
    for i, v in enumerate(vids):
        out = os.path.join(OUT, f"{slug(account)}_sheet{i}.jpg")
        ts = sheet(v, out)
        meta.append({"video": v, "ts": ts})
        parts.append({"type": "text", "text": f"SHEET {i}: timestamps {ts}"})
        parts.append(img_part(out))
    data = vision([{"role": "user", "content": parts}])
    looks = data.get("looks") or []
    print(f"  {account}: {len(looks)} looks from {len(vids)} videos")
    return looks, meta


def cut_and_verify(account, looks, meta):
    """An 8s clip and a still per look, at the model's own timestamps; each
    clip's middle frame is re-classified and dropped if it does not match."""
    menu = "; ".join(f"{l['key']}: {l['label']}" for l in looks)
    kept = {}
    for l in looks:
        for ex in (l.get("examples") or [])[:3]:
            try:
                v = meta[int(ex["video"])]["video"]; t = float(ex["t"])
            except (KeyError, IndexError, ValueError, TypeError):
                continue
            t0 = max(0.0, t - 1.0)
            stem = f"{slug(account)}_{l['key']}_{int(t)}"
            clip = os.path.join(OUT, stem + ".mp4"); still = os.path.join(OUT, stem + ".jpg")
            sh(["ffmpeg", "-v", "error", "-y", "-ss", f"{t0}", "-t", "8", "-i", v,
                "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-an", clip])
            sh(["ffmpeg", "-v", "error", "-y", "-ss", f"{t0+3}", "-i", v, "-frames:v", "1", "-q:v", "2", still])
            if not (os.path.exists(still) and os.path.getsize(still) > 3000):
                continue
            chk = vision([{"role": "user", "content": [{"type": "text", "text": CHECK + menu}, img_part(still)]}])
            if chk.get("key") == l["key"] and float(chk.get("confidence") or 0) >= 0.6:
                kept.setdefault(l["key"], []).append((clip, still, v, t))
                if len(kept[l["key"]]) >= 2: break
            else:
                for f in (clip, still):
                    try: os.remove(f)
                    except OSError: pass
    return kept


def upload(path, key):
    blob = open(path, "rb").read()
    for attempt in range(4):
        try:
            A._api_post(f"/api/assets/{key}", {"base64": base64.b64encode(blob).decode()}, timeout=180)
            return f"{BASE}/assets/{key}"
        except urllib.error.HTTPError as e:
            if e.code == 409: return f"{BASE}/assets/{key}"
            if attempt == 3: raise
            time.sleep(5 * (attempt + 1))


def write_style(account, looks, kept):
    styles = A._api_get("/api/render-styles")["styles"]
    sl = slug(account)
    st = next((s for s in styles if s["slug"] == sl), None)
    if st is None:
        st = {"slug": sl, "name": account.lstrip("@"), "medium": "", "prompt_prefix": "", "palette": [],
              "caption_mode": "overlay", "aspect": "9:16", "avoid": "", "notes": "", "reference_images": []}
    for k in ("palette", "reference_images", "shot_types"):
        if isinstance(st.get(k), str):
            try: st[k] = json.loads(st[k] or "[]")
            except Exception: st[k] = []
    types = []
    for l in looks:
        ex = kept.get(l["key"], [])
        t = {"key": l["key"], "label": l.get("label"), "when": l.get("when"), "weight": 1,
             "motion": {"none": "none", "push": "in", "pan": "right", "wipe": "wipe"}.get(l.get("motion"), "in"),
             "prompt": l.get("description") or "", "default": bool(l.get("default")),
             "reference_images": [], "hero_ok": bool(ex), "source": "discovered from the account's own videos"}
        for i, (clip, still, v, ts) in enumerate(ex, 1):
            t["reference_images"].append(upload(still, f"ref_{sl}_{l['key']}_{i:02d}.jpg"))
            if i == 1:
                t["motion_clip"] = upload(clip, f"ref_{sl}_motion_{l['key']}.mp4")
                t["motion_clip_source"] = f"{os.path.basename(v)} @{int(ts)}s"
        types.append(t)
    st["shot_types"] = types
    if not st.get("prompt_prefix"):
        d = next((l for l in looks if l.get("default")), looks[0] if looks else None)
        st["prompt_prefix"] = (d or {}).get("description") or ""
        st["medium"] = "; ".join(l.get("label", "") for l in looks)[:200]
    st["notes"] = (st.get("notes") or "") + (
        f"\n\n--- {time.strftime('%Y-%m-%d')} looks discovered from the account's own videos: "
        + ", ".join(f"{l['key']}({len(kept.get(l['key'], []))} real clips)" for l in looks))
    A._api_post("/api/render-styles", st, timeout=60)
    return sl, types


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--accounts", default="")
    ap.add_argument("--limit-videos", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(_ARGS)
    clips = A._api_get("/api/reference-clips")["clips"]
    by = {}
    for c in clips: by.setdefault(c["channel_name"] or "?", []).append(c)
    want = [x.strip() for x in a.accounts.split(",") if x.strip()] or sorted(by, key=lambda k: -len(by[k]))
    want = [w for w in want if "johnnyharris" not in w.lower()]     # his are done by hand
    summary = {}
    for acct in want:
        cs = by.get(acct) or next((v for k, v in by.items() if k.lower().lstrip("@") == acct.lower().lstrip("@")), [])
        if not cs: print(f"  {acct}: no clips"); continue
        try:
            r = discover(acct, cs, a.limit_videos)
            if not r: continue
            looks, meta = r
            if a.dry_run:
                for l in looks: print(f"     - {l['key']:<28} {l.get('when','')[:70]}")
                continue
            kept = cut_and_verify(acct, looks, meta)
            sl, types = write_style(acct, looks, kept)
            summary[acct] = {t["key"]: len(t["reference_images"]) for t in types}
            print(f"     -> style '{sl}': " + ", ".join(f"{k}({n})" for k, n in summary[acct].items()), flush=True)
        except Exception as e:
            print(f"  {acct}: FAILED {type(e).__name__}: {str(e)[:120]}", flush=True)
    json.dump(summary, open(os.path.join(OUT, "summary.json"), "w"), indent=1)
    print("\ndone:", len(summary), "accounts")


if __name__ == "__main__":
    main()
