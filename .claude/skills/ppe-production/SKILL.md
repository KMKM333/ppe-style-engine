---
name: ppe-production
description: Drive the PPE Style Engine — analyse creator accounts, generate production specs from Library creations, and assemble videos from them. Use whenever the user mentions PPE, a profile code (A.*, BK.*, C.*, PS.*, PVS.*), a production spec, a Studio Creation, importing videos for analysis, or asks to edit or cut something in a creator's style.
---

# PPE Style Engine

A three-layer system for making short-form video in a measured creator's style.
It measures real accounts, writes specs against those measurements, and
assembles video from the specs.

## Read the live state FIRST — always

**Never state a profile code, an account's coverage, or what exists from memory
or from this file.** All of it changes whenever videos are imported. Start every
task by fetching:

```bash
curl -s https://ppe-style-engine.onrender.com/api/skill/context
```

That returns the current voice profiles, editing profiles, format profiles,
which accounts have a visual brief, recent creations and specs, and
`ready_to_generate` — the accounts complete enough to build from. It needs no
key. **This file describes the workflow; that endpoint describes the world.**
If the two ever disagree, the endpoint is right.

## The layers, and what each decides

| Layer | Codes | Measures | Answers |
|---|---|---|---|
| Input Library | `A.*` `BK.*` `C.*` | ~90 writing attributes | what a piece SAYS, in whose voice |
| Editing | `PS.*` | shot lengths, shot-type mix | how it is CUT |
| Script + editing | `PVS.*` | 5 relational axes | how words and pictures RELATE |
| Visual brief | per account | palette, art style, typography | what the pictures LOOK like |

A profile is only as good as its evidence. `n_videos_analysed` / `n_inputs_analysed`
is in the context response — say it out loud when recommending one, and prefer
an account with 10 analysed videos over one with 1.

## The pipeline

```
Library input ─/transform─▶ Studio Creation ─/production/transform─▶ spec ─assemble─▶ video
```

### 1. Script — `/transform`
Pick an input and a target voice profile. Produces a Studio Creation.
POST only; a page load must never generate.

### 2. Spec — `/production/transform`
One creation, and ONE style source. The two modes are different transforms,
not variants:

- **Editing only** (+ `PS.*`) — the script stays exactly as written. The
  profile governs only the cut. Each beat carries `script_excerpt`: the
  creation's own sentences, verbatim, every sentence in exactly one beat.
- **Script + editing** (+ `PVS.*`) — the creation becomes INFORMATION. The
  target account supplies the writing rhythm AND the editing, because a
  creator's cadence and their cutting are one style. The creation's own voice
  is deliberately dropped; each beat returns `script_lines` in the target's
  cadence.

Beat boundaries fall where the writing turns. Each beat's duration comes from
how long its words take; its shot count from that duration and the profile's
measured average shot length.

### 3. Video — `assemble_video.py` on the transcriber box
The engine derives the plan (free, inspectable); the VPS executes it, because
that is where ffmpeg and the API keys are.

```bash
ssh root@95.217.16.211 'cd /root/bulk-transcriber && \
  export $(grep -o "^export [A-Z_]*=.*" run.sh | sed "s/^export //" | tr -d "\"'"'"'" | xargs) && \
  ./venv/bin/python3 assemble_video.py --creation_id N --dry-run'
```

**Always dry-run first.** It prints the shot count, whether a visual brief
exists, and the cost. Drop `--dry-run` to build. Defaults are deliberately
cheap: `low` quality (~$0.02/image), a `--budget` of $3 that refuses to start
above it, `--max-images` to reuse panels within a beat. Generated assets
persist, so an interrupted run resumes rather than re-buying.

Finished videos: `http://100.116.54.94:5001/videos`, and recorded on the engine
against their spec.

## Money — check before spending, always

Real measured rates. Quote them; do not estimate from memory.

| Action | Cost |
|---|---|
| Library classification | ~$0.04/video |
| Shot analysis | ~$0.08/video |
| P+S reading | ~$0.019/video |
| Visual style brief | ~$0.02/account |
| Spec generation | ~$0.03 |
| Video assembly | $0.27 (12 panels) – $0.99 (48) |

A `$15/day` cap (`PPE_DAILY_BUDGET_USD`) covers all three analysis pipelines.
When it is hit the engine answers `held: budget` and the runner waits for the
UTC-midnight reset rather than failing.

**State the cost before an action that spends, and never start a long spending
run without saying the total first.** A dry run is free; use it.

## Importing

`http://100.116.54.94:5001/run` — tick accounts × pipelines, runs in the
background. Ticking Production runs both readings, since the video is
downloaded and transcribed once either way. Already-analysed videos are
skipped. Instagram cannot be asked for an account's reel list, so a new
account needs its links pasted once.

## Things that will bite you

- **Account names arrive in several spellings.** "americanbaron" and
  "@americanbaron" are one account. Merge duplicates with
  `POST /api/channels/merge {"from_name": ..., "into_name": ...}` — exact names.
- **yt-dlp reports no duration for Instagram.** Anything reading duration must
  fall back to `ffprobe` on the file.
- **A partially uploaded shot input** is re-ingested and its shot list rebuilt,
  not re-classified — the stored list and a fresh detection disagree.
- **Deploying restarts the engine.** Do not deploy while a run or a batch job
  is in flight; it will die mid-way.
- **`/api/health`** (key-authed) reports disk, the SQLite write lock,
  classifier slots and recent tracebacks. Check it first when anything 500s.

## Reference

`reference.md` in this directory lists every endpoint, its body and its
response. Read it when you need an exact call.
