"""americanbaron and business.stick, typed by hand from their sheets."""
import sys, json, base64, subprocess, time, urllib.error, os, glob
sys.argv = [sys.argv[0]]; sys.path.insert(0, "/root/bulk-transcriber")
import app as A
BASE = "https://ppe-style-engine.onrender.com"

def src(stem):
    for d in ("video_cache", "lfvids"):
        p = f"/root/bulk-transcriber/{d}/{stem}.mp4"
        if os.path.exists(p): return p
def dur(p): return float(subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",p],capture_output=True,text=True).stdout or 0)
def sheet_ts(p, k):
    d = dur(p); n = max(4, min(24, int(d/8))); step = max(1.0, (d-4)/n); return round(2 + (k + 0.5)*step - 0.05, 2)
have = set()
try:
    lst = A._api_get("/api/assets"); have = {(it.get("key") if isinstance(it, dict) else it) for it in (lst.get("assets") or lst.get("items") or [])}
except Exception: pass
def up(path, key):
    if key in have: return f"{BASE}/assets/{key}"
    blob = open(path,"rb").read()
    for a in range(4):
        try: A._api_post(f"/api/assets/{key}", {"base64": base64.b64encode(blob).decode()}, timeout=180); return f"{BASE}/assets/{key}"
        except urllib.error.HTTPError as e:
            if e.code == 409: return f"{BASE}/assets/{key}"
            if a == 3: raise
            time.sleep(5*(a+1))
def save_style(slug, name, medium, types, prefix, note, avoid="", caption_mode="overlay"):
    styles = A._api_get("/api/render-styles")["styles"]
    st = next((s for s in styles if s["slug"] == slug), None) or {"slug": slug, "name": name, "palette": [], "aspect": "9:16", "reference_images": [], "notes": ""}
    for k in ("palette","reference_images","shot_types"):
        if isinstance(st.get(k), str):
            try: st[k] = json.loads(st[k] or "[]")
            except Exception: st[k] = []
    st.update({"medium": medium, "shot_types": types, "prompt_prefix": prefix, "caption_mode": caption_mode, "avoid": avoid,
               "notes": (st.get("notes") or "") + "\n\n--- 2026-09-14 --- " + note})
    A._api_post("/api/render-styles", st, timeout=60)

# ---- @americanbaron: one look, the creator on camera. Not generatable. ----
clips = [c for c in A._api_get("/api/reference-clips")["clips"] if (c["channel_name"] or "") == "@americanbaron"]
stems = sorted({(c["source_file"] or "").rsplit(".",1)[0] for c in clips})
refs = []
for i, stem in enumerate(stems[:3], 1):
    p = src(stem)
    if not p: continue
    still = f"/tmp/ab_{i}.jpg"
    subprocess.run(["ffmpeg","-v","error","-y","-ss",str(sheet_ts(p,2)),"-i",p,"-frames:v","1","-q:v","2",still],check=True)
    refs.append(up(still, f"ref_americanbaron_on-camera_{i:02d}_r2.jpg"))
save_style("americanbaron", "americanbaron",
    "the creator on camera — phone-shot acting sketches, one performer, real locations, yellow serif captions",
    [{"key":"on-camera","label":"The creator on camera","weight":1,"default":True,"motion":"none",
      "when":"every shot — this account is one performer acting sketches to a phone camera.",
      "prompt":"", "reference_images":refs, "hero_ok":False, "generate":False,
      "source":"typed by hand from the sheets: every frame is the creator himself"}],
    "NOT GENERATABLE: this account is the creator himself on camera. Do not generate panels for it; use it as a script or cadence source only.",
    "One look, not five: the namer split SCENES (window, desk, laptop) of one performer on camera. Nothing here is generatable — the style IS his face and delivery. Typed as a single non-generatable look; usable as a script/cadence source only.")
print("  americanbaron: 1 look, generate=False, %d stills" % len(refs))

# ---- @business.stick: one medium, four ways a shot is built. All generatable. ----
clips = [c for c in A._api_get("/api/reference-clips")["clips"] if (c["channel_name"] or "") == "@business.stick"]
stems = sorted({(c["source_file"] or "").rsplit(".",1)[0] for c in clips})
vids = [src(s) for s in stems if src(s)]
v0, v1 = vids[0], (vids[1] if len(vids) > 1 else vids[0])
HEAD = ("Warm hand-drawn cartoon in the style of @business.stick: stick-figure characters with round white heads and simple "
        "dot eyes, thin black limbs, dressed in period clothes; full painted scenes in warm sepia, ochre and dusty blue with "
        "soft indoor light; clean black outlines, gentle shading, storybook framing. ")
NOT = "NOT THIS: no photography, no 3D, no flat vector, no realistic faces, no text unless the look is title-banner."
LOOKS = [
 {"key":"character-in-scene","label":"Character in a full scene","default":True,"motion":"in",
  "when":"the default — a character doing something in a place: a room, a street, a workshop.",
  "prompt":HEAD+"ONE stick-figure character mid-action inside a fully painted period interior or street, props that tell the story, the character about a third of frame height. "+NOT,
  "frames":[(v0,0),(v0,1),(v1,1)]},
 {"key":"character-closeup","label":"Character close-up, mid-line","motion":"none",
  "when":"a character says or feels the key line — a reaction, a pitch, a decision.",
  "prompt":HEAD+"ONE stick-figure character from the chest up, filling the frame, an expressive face on the round head, a hand gesture, the scene behind soft and simple. "+NOT,
  "frames":[(v0,2),(v0,3),(v1,3)]},
 {"key":"crowd-wide","label":"Crowd or wide shot","motion":"in",
  "when":"many people, a market, an audience, a public place — scale and numbers.",
  "prompt":HEAD+"A wide painted scene with many small stick-figure characters — shoppers, an audience, a crowd — under a big architectural space, one or two figures in front for focus. "+NOT,
  "frames":[(v0,5),(v1,6)]},
 {"key":"title-banner","label":"Scene with a title banner","motion":"none",
  "when":"a headline moment — a name, a date, a verdict — that deserves a sign in the scene.",
  "prompt":HEAD+"A painted scene where a large hand-lettered BANNER or sign carries two or three words in bold capitals, characters small beneath it. The banner text is the caption. ",
  "frames":[(v0,5),(v1,4)]},
]
types = []
os.makedirs("/tmp/bs", exist_ok=True)
for l in LOOKS:
    refs, clip = [], None
    for i, (v, k) in enumerate(l["frames"], 1):
        t = sheet_ts(v, k); stem = f"/tmp/bs/{l['key']}_{i}"
        subprocess.run(["ffmpeg","-v","error","-y","-ss",str(t),"-i",v,"-frames:v","1","-vf","crop=iw:ih*0.88:0:0","-q:v","2",stem+".jpg"],check=True)
        refs.append(up(stem+".jpg", f"ref_business-stick_{l['key']}_{i:02d}_r2.jpg"))
        if i == 1:
            subprocess.run(["ffmpeg","-v","error","-y","-ss",str(max(0,t-1)),"-t","8","-i",v,"-vf","crop=iw:ih*0.88:0:0","-c:v","libx264","-crf","20","-preset","fast","-an",stem+".mp4"],check=True)
            clip = up(stem+".mp4", f"ref_business-stick_motion_{l['key']}_r2.mp4")
    t = {k: l[k] for k in ("key","label","when","prompt","motion")}
    t.update({"weight":1,"default":bool(l.get("default")),"reference_images":refs,"motion_clip":clip,"hero_ok":True,
              "source":"typed by hand from the sheets; frames are the account's own"})
    types.append(t); print("  business.stick %-20s %d stills, clip %s" % (l["key"], len(refs), "yes" if clip else "no"))
subprocess.run("ffmpeg -v error -y -pattern_type glob -i '/tmp/bs/*.jpg' -vf 'scale=300:-2,tile=5x2' -frames:v 1 /tmp/bs_sheet.jpg", shell=True)
save_style("business-stick", "business.stick", "warm hand-drawn cartoon, stick-figure characters in full painted period scenes",
    types, LOOKS[0]["prompt"], "Four looks typed by hand (the namer split six 'character somewhere' variants): character-in-scene (default), character-closeup, crowd-wide, title-banner. Real stills + 8s clips, caption band cropped.",
    avoid="photography, 3D render, flat vector, realistic faces, gradients, neon", caption_mode="overlay")
print("  business.stick: 4 looks registered")
