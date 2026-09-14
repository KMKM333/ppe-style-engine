"""Every account: a creator-on-camera look is never generated, and no style
defaults to one. The verifier already refuses to keep such cuts; this makes
the selector refuse to assign them."""
import sys, json, re
sys.argv=[sys.argv[0]]; sys.path.insert(0,"/root/bulk-transcriber")
import app as A
FACE = re.compile(r"talking[-_ ]?head|on[-_ ]camera|vlog|interview|presenter|face[-_ ]cam|to[-_ ]camera", re.I)
styles = A._api_get("/api/render-styles")["styles"]
rows = []
for st in styles:
    t = st.get("shot_types"); t = json.loads(t) if isinstance(t, str) else (t or [])
    if not t: continue
    for k in ("palette","reference_images"):
        v = st.get(k); st[k] = [] if v in (None,"") else (json.loads(v) if isinstance(v,str) else v)
    changed = False; nongen = []
    for x in t:
        if FACE.search(x.get("key","")) or FACE.search(x.get("label","")):
            if x.get("generate", True) is not False or x.get("hero_ok"):
                x["generate"] = False; x["hero_ok"] = False; changed = True
            nongen.append(x["key"])
    gen = [x for x in t if x.get("generate", True) is not False]
    d = next((x for x in t if x.get("default")), None)
    if gen and (d is None or d.get("generate", True) is False):
        for x in t: x["default"] = False
        # prefer the generatable look with the most real references
        best = max(gen, key=lambda x: (len(x.get("reference_images") or []), 1 if x.get("motion_clip") else 0))
        best["default"] = True; changed = True
        d = best
    if changed:
        st["shot_types"] = t
        A._api_post("/api/render-styles", st, timeout=60)
    n_refs = sum(len(x.get("reference_images") or []) for x in gen)
    n_clips = sum(1 for x in gen if x.get("motion_clip"))
    rows.append((st["slug"], len(t), len(gen), (d or {}).get("key"), n_refs, n_clips, ",".join(nongen)))
print("%-20s looks gen default              refs clips  non-generatable" % "style")
for r in sorted(rows): print("%-20s %5d %3d %-20s %4d %5d  %s" % r)
