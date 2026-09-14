"""Null.histories retyped by hand from its sheets: five illustrated looks, no talking head."""
import sys, json, base64, subprocess, time, urllib.error, os
sys.argv = [sys.argv[0]]; sys.path.insert(0, "/root/bulk-transcriber")
import app as A
BASE = "https://ppe-style-engine.onrender.com"
clips = [c for c in A._api_get("/api/reference-clips")["clips"] if (c["channel_name"] or "") == "@Null.histories"]
def src(stem):
    for d in ("video_cache", "lfvids"):
        p = f"/root/bulk-transcriber/{d}/{stem}.mp4"
        if os.path.exists(p): return p
stems = sorted({(c["source_file"] or "").rsplit(".",1)[0] for c in clips})
vids = [p for p in (src(s) for s in stems) if p]
print("source videos:", [os.path.basename(v) for v in vids])
def dur(p): return float(subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",p],capture_output=True,text=True).stdout or 0)
def ts(p, k):
    d = dur(p); n = max(4, min(24, int(d/8))); step = max(1.0, (d-4)/n); return round(2 + (k + 0.5)*step - 0.05, 2)
# sheet0 = the famine video (24 frames, 6 per row); sheet1 = the Paladin video (4 frames)
v0 = vids[0]; v1 = vids[1] if len(vids) > 1 else vids[0]
print("v0 %.0fs (%d frames)  v1 %.0fs" % (dur(v0), max(4, min(24, int(dur(v0)/8))), dur(v1)))
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
HEAD = ("Hand-inked storybook illustration in the style of @Null.histories: simple stick-figure people with round white heads, "
        "dot eyes and thin black limbs, in period clothes; full scenes in a warm sepia, rust and ochre palette with soft "
        "watercolour shading; thin ink outlines, gentle paper texture, cinematic vertical framing. ")
NOT = "NOT THIS: no photography, no 3D, no flat vector, no realistic faces, no neon, no text."
# frame indices read off sheet0 (row-major, 6 per row) and sheet1
LOOKS = [
 {"key":"group-in-scene","label":"Group of figures in a scene","default":True,"motion":"in",
  "when":"the default — several people in a place: soldiers marching, workers in a field, a crowd at a gate.",
  "prompt":HEAD+"Three to eight stick-figure people in a fully painted historical scene — a field, a street, a camp, a factory — mid-action, the setting doing half the storytelling. "+NOT,
  "frames":[(v0,0),(v0,2),(v0,8)]},
 {"key":"single-figure","label":"One figure, close","motion":"none",
  "when":"one person's moment — a decision, a plea, exhaustion, a reaction.",
  "prompt":HEAD+"ONE stick-figure character large in frame, from the waist up or kneeling, an expressive face on the round head, the scene behind simple and soft. "+NOT,
  "frames":[(v0,4),(v0,13),(v0,16)]},
 {"key":"object-subject","label":"An object as the subject","motion":"in",
  "when":"a thing carries the point — grain, a coin, a book, a stalk of wheat, a ledger.",
  "prompt":HEAD+"A single object centred and large — a sack of grain, a coin on a ledger, a stalk of wheat, an old book — painted with care, one or two small figures beside it for scale. "+NOT,
  "frames":[(v0,9),(v0,18),(v1,1)]},
 {"key":"map-diagram","label":"Map or diagram on the wall","motion":"wipe",
  "when":"geography, movement between places, or a sequence laid out as a diagram — arrows, years, steps.",
  "prompt":HEAD+"A large hand-painted MAP or DIAGRAM filling the frame — arrows drawn across it, small flags or year labels as illegible marks — with one or two figures pointing at it. "+NOT,
  "frames":[(v0,15),(v0,11)]},
 {"key":"crowd-vortex","label":"Crowd swirl / scale device","motion":"in",
  "when":"scale, mass, a whole population — many people arranged in a spiral, a ring, a spreading pattern.",
  "prompt":HEAD+"Dozens of tiny stick-figure people arranged in a spiral or a vortex spreading across the frame, seen from above, a single figure at the centre. "+NOT,
  "frames":[(v0,19),(v0,10)]},
]
os.makedirs("/tmp/nh", exist_ok=True); types = []
for l in LOOKS:
    refs, clip = [], None
    for i, (v, k) in enumerate(l["frames"], 1):
        t = ts(v, k); stem = f"/tmp/nh/{l['key']}_{i}"
        subprocess.run(["ffmpeg","-v","error","-y","-ss",str(t),"-i",v,"-frames:v","1","-q:v","2",stem+".jpg"],check=True)
        refs.append(up(stem+".jpg", f"ref_null-histories_{l['key']}_{i:02d}_r2.jpg"))
        if i == 1:
            subprocess.run(["ffmpeg","-v","error","-y","-ss",str(max(0,t-1)),"-t","8","-i",v,"-c:v","libx264","-crf","20","-preset","fast","-an",stem+".mp4"],check=True)
            clip = up(stem+".mp4", f"ref_null-histories_motion_{l['key']}_r2.mp4")
    t = {k: l[k] for k in ("key","label","when","prompt","motion")}
    t.update({"weight":1,"default":bool(l.get("default")),"reference_images":refs,"motion_clip":clip,"hero_ok":True,
              "source":"typed by hand from the sheets; frames are the account's own"})
    types.append(t); print("  %-16s %d stills, clip %s" % (l["key"], len(refs), "yes" if clip else "no"))
subprocess.run("ffmpeg -v error -y -pattern_type glob -i '/tmp/nh/*.jpg' -vf 'scale=260:-2,tile=7x2' -frames:v 1 /tmp/nh_sheet.jpg", shell=True)
st = next(x for x in A._api_get("/api/render-styles")["styles"] if x["slug"] == "null-histories")
for k in ("palette","reference_images","shot_types"):
    v = st.get(k); st[k] = [] if v in (None, "") else (json.loads(v) if isinstance(v, str) else v)
st.update({"shot_types": types, "prompt_prefix": LOOKS[0]["prompt"], "caption_mode": "overlay",
           "medium": "hand-inked stick-figure storybook, warm sepia/rust palette, full painted scenes",
           "avoid": "photography, 3D render, flat vector, realistic faces, neon, gradients, legible text",
           "notes": (st.get("notes") or "") + "\n\n--- 2026-09-14 --- Retyped by hand: the namer registered a 'talking-head' default with zero stills, but every frame of this account is illustrated. Five generatable looks from the sheets, real stills + 8s clips."})
A._api_post("/api/render-styles", st, timeout=60)
print("null-histories: %d looks registered, default=%s" % (len(types), types[0]["key"]))
