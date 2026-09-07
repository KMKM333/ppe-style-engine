---
name: ppe-reference-style
description: Build or refine a PPE render style from an account's real reference clips by MEASURING them, generating a probe, and scoring the output against the reference. Use whenever visual output looks wrong, a render style needs building or refining, or the user mentions reference clips, palettes, visual quality, or a look not matching an account.
---

# Refining a render style from reference clips

## The finding this is built on

A written brief describing an account's look produced clean panels in the wrong
palette with no caption system. Passing **10 seconds of that account's real work**
as a video reference reproduced its palette, its figure construction and its
caption plate — and the plate had never been described in the prompt at all.

So: **the clip carries the look; the brief carries what the clip cannot.** When
the two disagree, the clip wins. That changes what the brief is for. It is not a
description of the style — it is a record of what the reference *actually
produced*, plus the rules a 10-second sample cannot show.

Two documented ways this goes wrong, both of which this skill exists to prevent:

- **Generalising from one sample.** @guijooorge's style recorded royal blue
  `#306ce4` as "the" palette. The next section generated from the same reference
  came back yellow at 87% of frame. The ground is chosen *per scene*. One sample
  cannot show that, and a named palette actively hides it.
- **Language that fits two media.** "Rough edges, warm palette, shallow depth"
  described a photographic collage and produced a flat vector illustration,
  because every one of those words fits both.

Both are measurement problems. Measure; do not describe.

## The tool

`measure_reference.py` ships **in this skill directory** — that is the source of
record. It runs on the transcriber box, which has the clips and the ffmpeg. Copy
it up if it is missing or stale:

```bash
scp .claude/skills/ppe-reference-style/measure_reference.py \
    root@95.217.16.211:/root/bulk-transcriber/
```

It reads pixels, not impressions, and everything it does is **free** — ffmpeg,
numpy and tesseract, no API calls. `tesseract-ocr` is installed on the box; if a
rebuild loses it, `apt-get install -y tesseract-ocr` brings it back.

```bash
cd /root/bulk-transcriber && eval "$(grep '^export ' run.sh)"
./venv/bin/python3 measure_reference.py --channel guijooorge --limit 4
./venv/bin/python3 measure_reference.py --style guijooorge-flat
./venv/bin/python3 measure_reference.py --compare <reference.mp4> <output.mp4>
```

What it reports, and why each one is there:

| Measure | What it settles |
|---|---|
| `ground_verdict` | FIXED / PER-SCENE / MIXED / UNKNOWN — whether a colour is a brand colour or a per-scene choice. This is the royal-blue lesson, computed. |
| `flatness` top3 / top8 | Separates flat vector from photographic **by number**. ≥75% = flat vector. ~54% = textured or archival. Adjectives cannot do this. |
| `linework` | Edge density and near-black area — thick outlines vs none. |
| `mean_saturation` | The user has rejected renders for over-saturation; this is the dial. |
| `caption` band | Where type sits — a frequency heuristic, superseded by the OCR block below. |
| `cuts_per_min` | Pacing, from the clip itself. |
| `letterbox` | Uniform padding cropped before anything is measured — see the trap below. |
| `attrs` | Surface and structure: grain, whether fills are flat or ramped, shadowing and depth, shape language, edge sharpness, hue-family count, temperature. |
| `type` | Read by OCR: casing, cap height as % of frame, alignment, band, words on screen, stroke weight, glyph colour, and **the plate behind the type** with a uniformity score. |
| `brief` | A paste-ready block written **from the measurements**, in the shape that worked. |

The verdict logic asks *"does one ground dominate?"*, not *"how many distinct
grounds are there"* — the second answer moves with the merge tolerance, the
first does not.

### Two traps the tool now handles, and you must not undo

**Padding is not design.** These clips carry a uniform band at top and bottom.
Left in, it counted as design: near-black "linework" for an account whose
linework is thin, and it dragged every ground share down. The bars are cropped
before anything is measured — but only when the band's colour actually differs
from the artwork, because on flat-colour material the top rows are legitimately
a uniform ground and cropping those would shrink the very ground share the
colour verdict rests on.

**A burned-in subtitle is not the account's caption style.** The first OCR pass
read `"And they're getting richer and richer."` in a generic sans off a grey
pill and reported it as @guijooorge's caption treatment. It is an auto-caption.
Position separates them, not size: his subtitle words all sit at 90–95% frame
height, while @Barry's Economics sets `SEARCH THE BURRY INSIDE` at 34–45% in
caps. Text below 85% height is reported as `bottom_band` and kept **out** of the
type analysis — but it is reported, never dropped, because an account that
genuinely sets its captions low needs that call made by a person.

### What OCR is for

Not reading words — **localising the type so its material can be measured**. Once
the glyph boxes are known, the tool measures stroke weight, cap height as a share
of frame, alignment, glyph colour, and whether the type sits on a solid plate or
straight on the artwork. The plate is the point: passing a reference clip
reproduced @guijooorge's caption plate when the prompt had never mentioned one.
Now it can be stated — `plate #cec1b6, uniformity 0.81, glyphs #7f7267` — instead
of hoping the reference carries it.

Confidence is reported. Below ~70% treat the strings as unreliable; the geometry
(box, height, position) stays usable even when the characters are wrong.

## Which lever actually moves the output

Measured, by scoring the renders we already had against the account each was
imitating. Every render below differs from a sibling by one variable.

| Lever | Effect | Cost |
|---|---|---|
| **Saturation correction** | **2.34 → 1.07** (−54%) | free |
| **+ gentle palette lock** | 1.07 → **1.03** | free |
| **Best-of-N selection** | headroom up to **1.38** — one render's best scene scored 1.09 against its own mean of 2.34 | N× image credits |
| Applying a measured style at all | 3.55 → 2.34 (−34%) | free |
| Higgsfield instead of the free route | 2.34 → 2.17 (−7%) | 26–32.5 credits |
| Reference frames | **inconclusive** — see below | image credits |

Two findings worth carrying:

**Saturation was the largest error term in every render, and it pointed the
wrong way.** Renders aimed at @johnnyharris, whose clips measure 0.29, came out
at 0.65–0.72; renders aimed at @guijooorge, whose clips measure 0.70, came out
at 0.30–0.39. The two accounts had each other's saturation. Correcting it alone
takes a free-route render past the Higgsfield cut of the same material, for
nothing.

**What you feed the generator matters about five times more than which
generator it is** — 34% for applying a measured style against 7% for switching
to Higgsfield.

**The reference-frame result is not yet trustworthy.** The one A/B we have
scored 2.66 with frames against 2.12 without, but that pair also differs in
caption treatment, so the regression cannot be attributed. It needs a clean
single-variable run before it earns any spend.

## The correction tool

`match_look.py`, beside this file. It writes a NEW file and never touches its
input, so it runs on a free-route render, a Higgsfield output or a downloaded
clip alike — and reverting it means not running it.

```bash
./venv/bin/python3 match_look.py --set-targets guijooorge-flat --from-channel guijooorge
./venv/bin/python3 match_look.py in.mp4 out.mp4 --style guijooorge-flat --saturation
./venv/bin/python3 match_look.py in.mp4 out.mp4 --style guijooorge-flat --saturation --palette-lock
```

Saturation is corrected by measuring, not by guessing a multiplier: ffmpeg's
`eq=saturation` acts in YUV and the metric is area-weighted HSV, so the tool
applies a first guess, re-measures, refines, and keeps whichever pass landed
closest.

Palette-lock defaults are **measured, not chosen**. At strength 0.8 / radius
0.28 it made the render worse than saturation alone (1.13 against 1.07); at
0.4 / 0.16 it improves on it (1.03). A lock has to nudge — snapping hard
destroys the shading that makes flat fills read as deliberate.

## Selection in the renderer

`assemble_video.py --best-of N --style <slug>` generates N candidates per panel
and keeps the one closest to the style's measured targets. **Default 1 = the old
path**, and the filename carries `_bestN`, so reverting is passing nothing.

It only runs against a style carrying measured targets — without them there is
nothing to select on, and it says so rather than inventing a target.

`--ref-source style` (default) takes reference frames from the applied style's
own reference clip rather than the creation's account. Creation 13 is a Barry's
Economics script rendered in @guijooorge's style, and the old path handed it
Barry's frames — blending two accounts so neither came through. `--ref-source
account` restores the old behaviour.

## The loop

**Steps 1, 2, 5 and 6 are free. Step 4 costs credits and must be agreed first.**

### 1. Measure the reference clips — free

```bash
./venv/bin/python3 measure_reference.py --channel <account> --limit 4 --json /tmp/ref.json
```

Use **at least 3 clips from 3 different videos**. The tool spreads its picks
across source videos for exactly this reason, and prints `SINGLE CLIP — not
enough to state the account's system` when it cannot. Honour that message; do
not write a style from one clip.

Clips are registered at `/production/reference-clips`, grouped by account. If an
account has none, cut them with `extract_reference_clips.py` first.

### 2. Write the brief from the measurements — free

Start from the tool's `brief` block. Then add only what pixels cannot show:

- **subject matter and what is being drawn** — figures, objects, how a person is
  constructed;
- **an `avoid` list** — the media it must not drift into;
- **caption mode** — `baked` when the model's own lettering is part of the
  composition, `overlay` only for photographic material. Overlaying type on a
  panel whose lettering the model composed is a documented mistake that made a
  render worse.

Never restate a colour the tool did not measure. If the verdict is PER-SCENE,
write the **rule** ("a different saturated ground per scene, chosen for the
beat"), never a hex value.

### 3. Attach the reference clip — free

On `/production/reference-clips`: select the clip, choose the style, **Set as
reference**. This clears `reference_media_id`, the cache of where that clip was
uploaded to the provider — otherwise the style shows the new clip and generates
from the old one.

**Reference clips must be 20s or under.** Measured: 10s and 20s are accepted;
45s and 95s are rejected with a bare `422` that says nothing about why.

### 4. Generate ONE probe section — COSTS CREDITS, ASK FIRST

> Standing instruction from the user, verbatim: **"DONT SPEND MONEY OR OTHER APP
> CREDITS WITHOUT MENTIONING"**. This includes probes. State the cost and wait
> for a yes.

One section, ~5 seconds. Not a full video. Via Higgsfield `generate_video` on
`seedance_2_5` in `omni_reference` mode, passing the clip as `video_references`.

### 5. Score the output against the reference — free

```bash
./venv/bin/python3 measure_reference.py --compare <reference.mp4> <output.mp4>
```

This is the step that makes "better" a number instead of an impression. It
reports each measure as `match` or `OFF` and lists what to carry into the next
prompt. Do not judge a render by eye and call it improved.

### 6. Write the style from the OUTPUT, not from the source analysis — free

The user's correction, and the rule that follows from it: build the Higgsfield
example first using the account as *information*, and only **then** base the
render style on what Higgsfield actually produced — because those videos come
out far better than the free route, so the output is the better evidence of what
the style really is.

Record in `notes` what the reference supplied *for free* (things that appeared
without being described) versus what the prompt had to carry. That distinction is
the whole value of the style.

## Traps when running an A/B on the renderer

**A style with `caption_mode: baked` silently sets `--no-caption-overlay`.** So a
render made with a baked style carries `_nocap` and one made before that rule did
not — and comparing the two measures caption treatment as well as whatever you
meant to test. This is the same confound that made the first reference-frame
result untrustworthy. Check the output FILENAME suffixes match on everything
except the variable under test before comparing anything.

**Compare panels, not finished videos.** Panels are generated before treatment,
so caption mode, grade and grain cannot reach the PNGs in the work directory.
Scoring those with `score_image` is both cleaner and free, and it sidesteps the
trap above entirely.

**Each variant gets its own work directory**, named from the same suffix, so
nothing is shared and TTS is re-billed per variant. Copy `beat_*.mp3` and
`audio.txt` across first — but note the destination name must include every
suffix the run will produce, `_nocap` included, or the seeding silently misses.

**Re-check the cost estimate after adding any flag that multiplies calls.** The
estimator quoted $0.46 for a best-of-3 run — it did not know the flag existed,
so it under-quoted threefold and `--budget` guarded a meaningless number. That is
the same class of error as the run once quoted at $1.95 that billed about $9.

## Rules that came from real failures

1. **n ≥ 3 videos before stating any system.** One clip describes one clip.
2. **A palette is a system, not a set.** If the ground changes per scene, say so
   as a rule. Naming a hex value freezes one sample as the truth.
3. **Never describe with words that fit two media.** If a sentence would suit
   both a vector illustration and a photographic collage, it is not doing any
   work. Use the flatness number, the grain figure and the axis-alignment score.
4. **The clip beats the brief.** Do not spend prompt length re-describing what
   the reference already shows; spend it on what the reference cannot show.
5. **Match caption mode to the MATERIAL, not the account.** Illustrated →
   `baked`. Photographic → `overlay`.
6. **Measure the output before claiming improvement.** Step 5 exists because
   "looks better" has been wrong before.
7. **Ask before any credit spend, including a probe.**

## Related

- `ppe-production` — the pipeline this feeds: script → spec → video.
- `/production/styles` — the styles themselves.
- `/production/reference-clips` — the clips, grouped by account.
