# Hermes skill: production (shot/pacing) analysis from Telegram

Paste-ready definition for a second Hermes skill, so texting a link can run **production analysis** as well as library ingestion.

## Library vs production — two different pipelines

The distinction that matters, because the same video link can go down either path:

| | **Library** | **Production spec** |
|---|---|---|
| Analyses | What the video *says* — transcript, style, arguments | How the video is *cut* — shot lengths, pacing, framing |
| Needs | Transcription (Whisper) | ffmpeg scene detection + one frame per shot |
| Profile codes | `A.*` (Instagram), `BK.*` (books) | `PS.*` |
| Existing skill | `bulk-video-ingest` | **this one** |
| Lands at | `/inputs` | `/production/inputs` |

A creator can have both — the same person's writing style *and* their editing style — as two separate profiles. They're kept apart deliberately: shot pacing has nothing to compare against a written script, so a `PS.*` profile is never ranked in a text-style comparison.

## The endpoint

On the transcriber (Tailscale-only, **not public**):

```
POST http://100.116.54.94:5001/api/shot-analysis
Content-Type: application/json
```

Body — single or batch:

```json
{ "url": "https://www.instagram.com/reel/XXXX/" }
```
```json
{ "urls": ["https://...", "https://..."],
  "channel": "null.histories",
  "scene_threshold": 0.25 }
```

| Field | Required | Notes |
|---|---|---|
| `url` / `urls` | yes (one of) | Batch is processed **sequentially**, never in parallel |
| `channel` | no | Defaults to `Unassigned Import` — reassign later rather than guessing |
| `scene_threshold` | no | 0–1, default 0.25. Lower = more cuts detected |

Response:

```json
{ "ok": true, "job_id": "a1b2c3d4e5f6", "n_videos": 1,
  "channel": "null.histories", "scene_threshold": 0.25,
  "status_url": "/status/a1b2c3d4e5f6",
  "job_page_url": "/shot_analysis/a1b2c3d4e5f6" }
```

Poll `GET /status/<job_id>` until `status` is `done` or `error`. For a batch, the manifest carries an `items[]` array with each child's own status.

**No auth header.** This app has no inbound authentication — it's bound to a private Tailscale address instead. Anything that can reach the port can start a job, so it must not be exposed publicly.

---

## Skill definition

```yaml
name: ppe-production-analysis
description: >
  Run production (shot/pacing) analysis on a video via the PPE transcriber.
  Use when the user wants to analyse HOW a video is edited — shot lengths,
  cutting rhythm, framing — rather than what it says. Trigger phrases:
  "shot analysis", "production analysis", "analyse the editing",
  "add to production", "PS profile".
  Do NOT use for normal library ingestion — that is `bulk-video-ingest`.

inputs:
  - name: urls
    description: One or more video links the user sent.
  - name: channel_name
    description: Optional creator name. Omit if the user didn't say one.

steps:
  - id: disambiguate
    description: |
      If it is not clear whether the user wants LIBRARY or PRODUCTION
      analysis, ask before spending anything. Two buttons:
        "Library (what it says)"  /  "Production (how it's cut)"
      A bare link with no other context means library — that is the
      established default. Only route here on an explicit signal.

  - id: confirm_channel
    description: |
      If channel_name was given and is not an exact match, resolve it first
      via the live engine, exactly as the library skill does:
        GET https://ppe-style-engine.onrender.com/api/channels/suggest?name=...
        header X-Ingest-Key: {{ env.PPE_INGEST_API_KEY }}
      Show candidates as buttons. If channel_name was NOT given, skip this —
      the job defaults to "Unassigned Import" and can be reassigned later.

  - id: submit
    http:
      method: POST
      url: http://100.116.54.94:5001/api/shot-analysis
      headers:
        Content-Type: application/json
      body:
        urls: "{{ urls }}"
        channel: "{{ confirmed_channel_name }}"

  - id: acknowledge
    description: |
      Reply immediately — do not make the user wait on the poll:
        "Analysing {n_videos} video(s) for shot/pacing. This takes a few
         minutes each (download, cut detection, frame classification).
         I'll report back."

  - id: poll
    description: |
      GET http://100.116.54.94:5001/status/{{ job_id }} every 30s,
      up to 30 minutes. Terminal when status is "done" or "error".
      For a batch, report progress from items[] (e.g. "3 of 8 done").

  - id: report
    description: |
      On success, report the numbers that matter — they are the whole point
      of this analysis:
        "Done. {total_shots} shots, average {avg} seconds per shot.
         Profile {profile_code} updated."
      On error, report manifest.error verbatim.
      If classification_status is "needs_review", say so and link
      job_page_url — it needs a human look.
```

---

## Worth knowing

- **Don't run this at the same time as heavy library imports.** The VPS is a ~2GB box that has already had out-of-memory kills. Library ingestion loads Whisper (~670MB); shot analysis doesn't, but ffmpeg plus a video download is still real work. Sequential is safe, concurrent is not.

- **`scene_threshold` is the one tuning knob.** If a result comes back with far fewer shots than the video obviously has, lower it (0.15). If the count looks absurdly high — camera movement being read as cuts — raise it (0.35). Default 0.25 is a reasonable starting point, not a universal answer.

- **Batches are sequential by design.** Each video spawns ffmpeg and a series of vision calls; running them in parallel on this box is how you get an OOM kill.

- **A `PS.*` profile appears automatically** once a channel's first production input is classified. Nothing to create by hand.

- **Frame batches are capped by total image size**, not just shot count. Detailed illustrated frames are large, and too many in one vision call makes the API return an empty reply. Handled engine-side — mentioned only so an empty-reply error in the logs is recognisable rather than mysterious.
