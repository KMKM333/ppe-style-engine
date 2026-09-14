"""Type Guijooorge's five looks from his own footage, verified by eye on the sheets."""
import sys, json, base64, subprocess, time, urllib.error, os
sys.argv = [sys.argv[0]]; sys.path.insert(0, "/root/bulk-transcriber")
import app as A
BASE = "https://ppe-style-engine.onrender.com"
G1 = "/root/bulk-transcriber/gjvids/gj001.mp4"            # sheet0: 12 frames at 2 + k*step
G2 = "/root/bulk-transcriber/video_cache/05ac5b87352bfc60eafb.mp4"   # sheet1
d1 = float(subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","csv=p=0",G1],capture_output=True,text=True).stdout)
step1 = max(1.0, (d1 - 4) / 12); ts1 = [round(2 + (i + 0.5)*step1 - 0.05, 2) for i in range(12)]
print("gj001 %.0fs, sheet frames at %s" % (d1, ts1))
NOT = ("NOT THIS: no photography, no 3D, no gradients, no texture or grain, no sketch lines, no realistic "
       "detail, no more than about three colours in frame, no text or letters unless the look is hand-lettering.")
HEAD = ("Flat vector illustration in the style of @guijooorge: one saturated flat colour fills the whole "
        "background, a DIFFERENT colour per scene; against it black, white and one accent; hard clean "
        "edges, geometric shapes, figures faceless. ")
LOOKS = [
 {"key":"figure-in-scene","label":"One figure in a scene","default":True,"motion":"in",
  "when":"a person is doing something — the default whenever the sentence has a human subject.",
  "prompt":HEAD+"ONE flat faceless figure, small in frame, isolated on the ground colour, mid-action, with at most one prop or one flat shape of environment. "+NOT,
  "frames":[(G1, ts1[0]), (G1, ts1[7]), (G1, ts1[11] if len(ts1)>11 else ts1[-1])]},
 {"key":"object-symbol","label":"Object as symbol","motion":"in",
  "when":"an abstract idea stands for a thing — money, power, a system, a place — and no person is the subject.",
  "prompt":HEAD+"ONE object centred and isolated on the ground colour as a symbol — a hand, a building, a coin, a machine — built from simple geometric shapes, no figure. "+NOT,
  "frames":[(G1, ts1[1]), (G1, ts1[5]), (G1, ts1[9])]},
 {"key":"big-numeral","label":"Big numeral with figure for scale","motion":"in",
  "when":"a number or percentage is the point of the sentence.",
  "prompt":HEAD+"A single number or percentage in huge bold flat type filling most of the frame, in the accent colour, with one tiny faceless figure standing beside it for scale. "+NOT,
  "frames":[(G1, ts1[3])]},
 {"key":"hand-lettering","label":"Hand-lettered word","motion":"none",
  "when":"one word carries the beat — a reaction, a verdict, a label.",
  "prompt":HEAD+"ONE short word painted across the ground in loose brush-script lettering in the accent colour, nothing else in frame. ",
  "frames":[(G1, ts1[4])]},
 {"key":"assembling-stack","label":"Objects assembling piece by piece","motion":"wipe",
  "when":"a list, an accumulation, or a step-by-step build-up — several things that add up.",
  "prompt":HEAD+"Three to six flat objects arranged as a cluster on the ground colour, each on its own coloured block, the set reading as things that have been stacked up one by one. "+NOT,
  "frames":[(G2, 4.0), (G2, 7.0)]},
]
have = set()
try:
    lst = A._api_get("/api/assets"); have = {(it.get("key") if isinstance(it, dict) else it) for it in (lst.get("assets") or lst.get("items") or [])}
except Exception: pass
def up(path, key):
    if key in have: return f"{BASE}/assets/{key}"
    blob = open(path, "rb").read()
    for attempt in range(4):
        try:
            A._api_post(f"/api/assets/{key}", {"base64": base64.b64encode(blob).decode()}, timeout=180); return f"{BASE}/assets/{key}"
        except urllib.error.HTTPError as e:
            if e.code == 409: return f"{BASE}/assets/{key}"
            if attempt == 3: raise
            time.sleep(5*(attempt+1))
os.makedirs("/tmp/gjlooks", exist_ok=True)
types = []
for l in LOOKS:
    refs, clip_url = [], None
    for i, (v, t) in enumerate(l["frames"], 1):
        stem = f"/tmp/gjlooks/{l['key']}_{i}"
        # crop the burned-in caption band (bottom 12%) and any pad: the look, not the subtitle
        subprocess.run(["ffmpeg","-v","error","-y","-ss",f"{t}","-i",v,"-frames:v","1","-vf","crop=iw:ih*0.88:0:0","-q:v","2",stem+".jpg"],check=True)
        refs.append(up(stem+".jpg", f"ref_guijooorge_{l['key']}_{i:02d}_r2.jpg"))
        if i == 1:
            subprocess.run(["ffmpeg","-v","error","-y","-ss",f"{max(0,t-1)}","-t","8","-i",v,"-vf","crop=iw:ih*0.88:0:0","-c:v","libx264","-crf","20","-preset","fast","-an",stem+".mp4"],check=True)
            clip_url = up(stem+".mp4", f"ref_guijooorge_motion_{l['key']}_r2.mp4")
    t = {k: l[k] for k in ("key","label","when","prompt","motion")}
    t.update({"weight":1,"default":bool(l.get("default")),"reference_images":refs,"motion_clip":clip_url,"hero_ok":True,
              "source":"his own videos, frames chosen by eye on the contact sheets"})
    types.append(t); print("  %-18s %d stills, clip %s" % (l["key"], len(refs), "yes" if clip_url else "no"))
# contact sheet of every registered still so the picks can be checked once more
subprocess.run("ffmpeg -v error -y -pattern_type glob -i '/tmp/gjlooks/*.jpg' -vf 'scale=300:-2,tile=5x2' -frames:v 1 /tmp/gjlooks_sheet.jpg", shell=True)
st = next(x for x in A._api_get("/api/render-styles")["styles"] if x["slug"] == "guijooorge-flat")
for k in ("palette","reference_images","shot_types"):
    if isinstance(st.get(k), str):
        try: st[k] = json.loads(st[k] or "[]")
        except Exception: st[k] = []
st["shot_types"] = types
st["cadence"] = {"cuts_per_min": 22.1, "median_shot_sec": 2.33, "avg_shot_sec": 3.15, "measured_from": "gj001.mp4 timeline analysis 2026-09-14"}
st["notes"] = (st.get("notes") or "") + "\n\n--- 2026-09-14 --- Five looks typed from his own footage (figure-in-scene default, object-symbol, big-numeral, hand-lettering, assembling-stack), each with real stills and an 8s motion clip, caption band cropped. Cadence measured: 22 cuts/min, median shot 2.3s. Option B: Seedance gets his clip for the shot's look as video_references and the subject in the prompt — no panel."
A._api_post("/api/render-styles", st, timeout=60)
c = next(x for x in A._api_get("/api/render-styles")["styles"] if x["slug"] == "guijooorge-flat")
tt = c["shot_types"]; tt = json.loads(tt) if isinstance(tt, str) else tt
print("\nstyle guijooorge-flat: %d looks, cadence %s" % (len(tt), (c.get("cadence") or {}).get("cuts_per_min", "not stored")))
