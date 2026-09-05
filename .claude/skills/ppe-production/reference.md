# PPE Engine — endpoint reference

Base: `https://ppe-style-engine.onrender.com`
Transcriber (Tailscale only): `http://100.116.54.94:5001`

Anything under `/api/*` needs `X-Ingest-Key: $PPE_INGEST_API_KEY`, except
`/api/skill/context`, which is public so a session can orient before it has a
key. The key lives in `/root/bulk-transcriber/run.sh` on the VPS and is never
stored in this repo.

## Orientation

| Call | Returns |
|---|---|
| `GET /api/skill/context` | current profiles, coverage, recent creations/specs, `ready_to_generate`. **Start here.** |
| `GET /api/health` | disk, SQLite write lock, classifier slots, recent tracebacks |
| `GET /api/analysis/roster` | every Instagram account with the videos the engine holds and which pipelines have each |

## Analysis

| Call | Notes |
|---|---|
| `POST /api/ingest/video` | Library. `{title, script, channel, platform, url, duration_sec, length_band}` |
| `POST /api/videos/<id>/classify` | `{skip_topic_check: true}` to pass the keyword gate deliberately |
| `POST /api/ingest/production-spec` | Shot analysis. `{title, channel, url, duration_sec, scene_threshold, shots:[…]}`. A dedupe hit whose frames are complete is returned untouched; one missing a frame has its shot list REPLACED. |
| `POST /api/production-spec/inputs/<id>/shots/<sid>/frame` | `{image_base64}` — format detected from the bytes |
| `POST /api/production-spec/inputs/<id>/classify` | `{only_unclassified: true}` classifies only shots with no category and leaves the rest — use for repairs, never a plain re-run |
| `POST /api/ingest/format` | P+S. `{channel, title, url, duration_sec, transcript, has_audio, frames:[…]}`. The PVS code is allocated from the account name. |
| `POST /api/format/inputs/<id>/classify` | joint visual+script pass |
| `POST /api/production-spec/channels/<cid>/visual-style` | derives palette / art style / typography from that account's own frames |

Classify routes answer `{"held": "budget"}` at the daily cap and
`{"held": "busy"}` at the classifier cap (`PPE_MAX_CLASSIFIERS`, default 2).
Both mean *wait and retry*, never *failed*.

## Generation

| Call | Notes |
|---|---|
| `POST /production/transform` (form) | `transformation_id`, `mode=editing\|combined`, and `production_profile_id` OR `format_profile_id`. Exactly one style source. |
| `POST /production/spec-creations/<id>/regenerate` | re-runs generation for an existing spec |
| `GET /api/creations/<id>/assembly-plan` | beats expanded to shots: duration, panel prompt, caption, voiceover. Free. |
| `POST /api/creations/<id>/assembled` | records a built video. `{url, path, duration_sec, n_shots, cost_usd}`. Idempotent per spec. |

## Maintenance

| Call | Notes |
|---|---|
| `POST /api/channels/merge` | `{from_name, into_name}` — exact names. Moves videos/inputs/format profile, drops duplicate style profiles, rebuilds survivors. |
| `GET /api/production-spec/duration-repair` | shot inputs ingested with no duration |
| `POST /api/production-spec/inputs/<id>/append-shot` | adds a dropped closing shot; idempotent |
| `GET /api/format/duration-repair` | P+S inputs that need re-sampling |
| `POST /api/entity/rename` / `delete` | `{kind, id, name}` — kinds: video, book, production_input, format_input, format_profile, channel (rename only) |

## Pages

| Path | |
|---|---|
| `/production/inputs` | one row per import, both readings, grouped by account, sortable |
| `/production/profiles` `/production/profiles/<code>` | editing profiles + visual identity |
| `/production/formats` `/production/formats/<code>` | script+editing profiles |
| `/production/transform` | make a spec |
| `/production/spec-creations/<id>` | the spec; `/share` is the standalone page |
| `/production/creations` | assembled and planned videos |
| `/usage` | real spend by call type |

## Scripts (on the VPS, `/root/bulk-transcriber`)

| Script | |
|---|---|
| `assemble_video.py --creation_id N [--dry-run] [--max-images N] [--quality low\|medium\|high] [--budget USD]` | builds the video |
| `repair_durations.py` | appends closing shots lost to a missing duration |
| `repair_format.py` | re-samples P+S inputs read from their first 9 seconds |
| `convert_shot_frames.py` | shrinks legacy PNG frames to JPEG |

## Engine scripts (`scripts/`, run on Render or locally)

`analyse_visual_style.py --channel_id N`, `classify_production_spec_shots.py
--input_id N [--only_unclassified]`, `classify_format_input.py
--format_input_id N`, `auto_process_shortform_video.py --video_id N`.
