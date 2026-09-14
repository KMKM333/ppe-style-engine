"""Final tally: every style, its default look, generatable looks with reference counts, non-generatable looks."""
import sys, json
sys.argv = [sys.argv[0]]; sys.path.insert(0, "/root/bulk-transcriber")
import app as A
styles = sorted(A._api_get("/api/render-styles")["styles"], key=lambda s: s["slug"])
ready, thin, none = [], [], []
for s in styles:
    st = s.get("shot_types"); st = json.loads(st) if isinstance(st, str) else (st or [])
    if not st: continue
    gen = [t for t in st if t.get("generate", True)]
    non = [t for t in st if not t.get("generate", True)]
    d = next((t["key"] for t in st if t.get("default")), "-")
    refs = sum(len(t.get("reference_images") or []) for t in gen)
    clips = sum(1 for t in gen if t.get("motion_clip"))
    print("%-20s default=%-24s gen=%d (%d stills, %d clips)  non-gen=%d" % (s["slug"], d, len(gen), refs, clips, len(non)))
    for t in gen: print("      %-26s %2d stills %s hero=%s  when: %s" % (t["key"], len(t.get("reference_images") or []), "clip" if t.get("motion_clip") else "    ", t.get("hero_ok"), (t.get("when") or "")[:70]))
    for t in non: print("      %-26s (not generated)" % t["key"])
    (ready if refs >= 4 else thin if refs else none).append(s["slug"])
print("\nREADY (4+ real stills):", ", ".join(ready))
print("THIN  (1-3 stills):    ", ", ".join(thin))
print("NONE  (script only):   ", ", ".join(none))
