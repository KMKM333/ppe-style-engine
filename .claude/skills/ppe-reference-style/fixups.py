"""Drop the reference stills that the eye check rejected (a presenter's face, a credits card, a wrong shot).
DROP = {slug: {look_key: [still indices]}}. If a look's first still is dropped its clip goes too (it was cut from that frame)."""
import sys, json, re
sys.argv = [sys.argv[0]]; sys.path.insert(0, "/root/bulk-transcriber")
import app as A
DROP = json.loads(sys.stdin.read())
styles = A._api_get("/api/render-styles")["styles"]
for st in styles:
    plan = DROP.get(st["slug"])
    if not plan: continue
    t = st.get("shot_types"); t = json.loads(t) if isinstance(t, str) else (t or [])
    for k in ("palette", "reference_images"):
        v = st.get(k); st[k] = [] if v in (None, "") else (json.loads(v) if isinstance(v, str) else v)
    for look in t:
        idx = plan.get(look["key"])
        if not idx: continue
        pat = re.compile(r"_(%s)_r\d+\.jpg$" % "|".join("%02d" % i for i in idx))
        before = list(look.get("reference_images") or [])
        look["reference_images"] = [u for u in before if not pat.search(u)]
        if 1 in idx:
            look["motion_clip"] = None; look["hero_ok"] = False
        print("%-18s %-24s %d -> %d stills%s" % (st["slug"], look["key"], len(before), len(look["reference_images"]), "  (clip dropped)" if 1 in idx else ""))
    st["shot_types"] = t
    A._api_post("/api/render-styles", st, timeout=60)
print("fixups applied")
