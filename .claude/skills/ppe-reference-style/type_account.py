"""Type one account's looks by hand from its sheets. Frame index -> exact
timestamp via that sheet's step (2 + k*step), so a pick lands on the frame I
looked at, not an estimate."""
import sys, json, base64, subprocess, time, urllib.error, os
sys.argv = [sys.argv[0]]; sys.path.insert(0, "/root/bulk-transcriber")
import app as A
BASE = "https://ppe-style-engine.onrender.com"
SPEC = json.load(open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/spec.json")) if False else json.load(open("/tmp/spec.json"))
IDX = json.load(open("/tmp/sheet_index.json"))
acct = SPEC["account"]; slug = SPEC["slug"]
rows = {r["sheet"]: r for r in IDX[acct]}
def src(stem):
    for d in ("video_cache","lfvids"):
        p = f"/root/bulk-transcriber/{d}/{stem}.mp4"
        if os.path.exists(p): return p
REV = os.environ.get("REF_REV", "r2")   # asset-key revision: the engine refuses to overwrite a key, so recuts get a new one
def ts(sheet, k):
    """Exact time of frame k on that sheet. New sheets carry a sidecar with the times they were
    sampled at. Old sheets (fps-filter sampler) hold, at slot k, the LAST frame before
    2 + (k + 0.5) * step - proven on a dense strip 2026-09-14 - not 2 + k * step."""
    r = rows[sheet]
    side = f"/root/bulk-transcriber/looks/{slug}_sheet{sheet}.json"
    if os.path.exists(side):
        return float(json.load(open(side))["ts"][k])
    return round(2 + (k + 0.5) * r["step"] - 0.05, 2)
have = set()
try:
    lst = A._api_get("/api/assets"); have = {(it.get("key") if isinstance(it, dict) else it) for it in (lst.get("assets") or lst.get("items") or [])}
except Exception: pass
def up(path, key):
    if key in have: return f"{BASE}/assets/{key}"
    blob = open(path, "rb").read()
    for a in range(4):
        try: A._api_post(f"/api/assets/{key}", {"base64": base64.b64encode(blob).decode()}, timeout=180); return f"{BASE}/assets/{key}"
        except urllib.error.HTTPError as e:
            if e.code == 409: return f"{BASE}/assets/{key}"
            if a == 3: raise
            time.sleep(5*(a+1))
out = f"/tmp/typed_{slug}"; subprocess.run(["rm", "-rf", out]); os.makedirs(out)   # fresh dir: stale stills from an earlier run must not reach the check sheet
crop = SPEC.get("crop", "crop=iw:ih*0.88:0:0")
types = []
for l in SPEC["looks"]:
    refs, clip = [], None
    lc = l.get("crop", crop)
    for i, fr in enumerate(l.get("frames", []), 1):
        sheet, k = fr[0], fr[1]; fc = fr[2] if len(fr) > 2 else lc
        v = src(rows[sheet]["stem"]); t = ts(sheet, k); stem = f"{out}/{l['key']}_{i}"
        subprocess.run(["ffmpeg","-v","error","-y","-ss",str(t),"-i",v,"-frames:v","1","-vf",fc+",scale=in_range=auto:out_range=pc,format=yuvj420p","-q:v","2",stem+".jpg"],check=True)
        if os.path.exists(stem+".jpg") and os.path.getsize(stem+".jpg") > 3000:
            refs.append(up(stem+".jpg", f"ref_{slug}_{l['key']}_{i:02d}_{REV}.jpg"))
            if clip is None:
                subprocess.run(["ffmpeg","-v","error","-y","-ss",str(max(0,t-1)),"-t","8","-i",v,"-vf",fc+",format=yuv420p","-c:v","libx264","-crf","20","-preset","fast","-an",stem+".mp4"],check=True)
                if os.path.exists(stem+".mp4") and os.path.getsize(stem+".mp4") > 4096:
                    clip = up(stem+".mp4", f"ref_{slug}_motion_{l['key']}_{REV}.mp4")
    gen = l.get("generate", True)
    t = {"key": l["key"], "label": l["label"], "when": l["when"], "prompt": l.get("prompt", ""),
         "motion": l.get("motion", "in"), "weight": 1, "default": bool(l.get("default")),
         "reference_images": refs, "motion_clip": clip, "hero_ok": bool(gen and clip), "generate": gen,
         "source": "typed by hand from the contact sheets; frames are the account's own"}
    types.append(t); print("  %-24s %d stills, clip %-3s gen=%s" % (l["key"], len(refs), "yes" if clip else "no", gen))
# check sheet: every still at one size with its name on it (mixed sizes break ffmpeg's tile filter)
mont = f"{out}/_mont"; subprocess.run(["rm","-rf",mont]); os.makedirs(mont)
for j, f in enumerate(sorted(x for x in os.listdir(out) if x.endswith(".jpg"))):
    subprocess.run(["ffmpeg","-v","error","-y","-i",f"{out}/{f}","-vf",
        f"scale=300:300:force_original_aspect_ratio=decrease,pad=300:300:(ow-iw)/2:(oh-ih)/2:color=gray,drawtext=text='{f[:-4]}':fontsize=16:fontcolor=white:box=1:boxcolor=black@0.6:x=4:y=4",
        f"{mont}/{j:02d}.png"])
subprocess.run(f"ffmpeg -v error -y -pattern_type glob -i '{mont}/*.png' -vf 'tile=6x4' -frames:v 1 /tmp/typed_{slug}_sheet.jpg", shell=True)
styles = A._api_get("/api/render-styles")["styles"]
st = next((s for s in styles if s["slug"] == slug), None) or {"slug": slug, "name": acct.lstrip("@"), "palette": [], "aspect": "9:16", "reference_images": [], "notes": ""}
for k in ("palette","reference_images","shot_types"):
    v = st.get(k); st[k] = [] if v in (None,"") else (json.loads(v) if isinstance(v,str) else v)
default = next((t for t in types if t["default"] and t["generate"]), next((t for t in types if t["generate"]), types[0]))
st.update({"shot_types": types, "medium": SPEC["medium"], "prompt_prefix": default["prompt"] or f"In the visual style of {acct}.",
           "caption_mode": SPEC.get("caption_mode", "overlay"), "avoid": SPEC.get("avoid", ""),
           "notes": (st.get("notes") or "") + "\n\n--- 2026-09-14 --- Typed by hand from the contact sheets (automatic cut verification kept almost nothing). " + SPEC.get("note", "")})
A._api_post("/api/render-styles", st, timeout=60)
print("%s: %d looks registered, default=%s" % (slug, len(types), default["key"]))
