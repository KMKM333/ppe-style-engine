"""Add sheet-index rows for accounts whose sheets exist but were never indexed, in the same video order discover() used."""
import sys, json, os, subprocess
sys.argv = [sys.argv[0]]; sys.path.insert(0, "/root/bulk-transcriber")
import app as A, discover_looks as DL
IDX = json.load(open("/tmp/sheet_index.json"))
clips = A._api_get("/api/reference-clips")["clips"]
by = {}
for c in clips: by.setdefault(c["channel_name"] or "?", []).append(c)
for acct in sys.stdin.read().split():
    cs = by.get(acct) or next((v for k, v in by.items() if k.lower().lstrip("@") == acct.lower().lstrip("@")), [])
    srcs = {}
    for c in cs:
        p = DL.source_path(c)
        if p: srcs.setdefault(p, c)
    rows = []
    for i, v in enumerate(list(srcs)[:5]):
        d = DL.dur(v); n = max(4, min(24, int(d / 8))); step = max(1.0, (d - 4) / n)
        rows.append({"sheet": i, "stem": os.path.splitext(os.path.basename(v))[0], "dur": int(d), "n": n, "step": round(step, 3)})
    IDX[acct] = rows; print(acct, rows)
json.dump(IDX, open("/tmp/sheet_index.json", "w"), indent=1)
