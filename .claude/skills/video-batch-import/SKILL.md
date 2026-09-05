---
name: video-batch-import
description: Submit a batch of YouTube video URLs through the local Bulk Transcriber to the live PPE Engine, poll until classified, auto-retry failures, and verify coverage. Use whenever the user pastes YouTube URLs and asks to import/analyse/add them (e.g. "new imports", "next batch", "analyse these").
---

# Video batch import

Automates the "submit → poll → retry → verify" ritual for adding long-form
YouTube videos to the PPE Engine via the local Bulk Transcriber. Built after
repeating this by hand across many batches in one session and hitting the
same few failure modes each time — see Guardrails below before running.

## Prerequisites

- The local Bulk Transcriber must be running (`http://localhost:5001`).
  `scripts/submit_batch.sh` checks this and fails fast with a clear message
  if it's down — start it with `cd instagram_transcriber && ./run.sh`.
- `PPE_INGEST_API_KEY` is read by the transcriber itself from its own env
  (set in `instagram_transcriber/run.sh`) — this skill never needs the key
  directly, since it talks to the transcriber's `/ingest_video` route, not
  the live engine's `/api/ingest/video` directly.
- Know the channel name up front. If the user didn't state one and this
  isn't an obvious continuation of a channel already used earlier in the
  conversation, ask before submitting — a wrong/placeholder channel name
  creates a bad channel row that then needs manual SQL correction.

## Procedure

1. **Submit.**
   ```bash
   .claude/skills/video-batch-import/scripts/submit_batch.sh "<Channel Name>" url1 url2 ...
   ```
   Capture the `<url> <job_id>` lines. Any line ending `SUBMIT_FAILED` means
   that URL didn't get a job_id — don't include it in polling.

2. **Poll in bounded chunks — never one long-running loop.**
   ```bash
   .claude/skills/video-batch-import/scripts/poll_batch.sh \
     "instagram_transcriber/results" 780 job_id1 job_id2 ...
   ```
   Run this as a background Bash call. It bounds itself to ~13 minutes
   (780s) and returns `ALL_DONE` or `CHUNK_TIMEOUT`. On `CHUNK_TIMEOUT`,
   just call it again with the same job_ids — jobs still processing pick
   up where they are; this keeps any single tool invocation short enough
   that a runtime cap can't silently truncate the wait.

3. **Extract results.** Each finished job's printed line has
   `ppe_video_id` and `classified_by`. Videos with `classified_by: claude`
   and no error are done. Videos with `classified_by: needs_review` need
   step 4. `NO_MANIFEST` for a job that should exist is itself a bug —
   don't just re-poll forever, surface it.

4. **Retry `needs_review` videos — one video per call, never chained.**
   ```bash
   .claude/skills/video-batch-import/scripts/retry_needs_review.sh <video_id>
   ```
   Call this once per needs_review video, as **independent** tool calls
   (parallel or sequential turns are both fine — just not one bash script
   looping over several videos in a single invocation). Retry each up to
   3 times total before giving up and reporting it as a genuine failure
   to the user with the actual validation error text.

5. **Verify — don't trust `classified_by` alone.**
   ```bash
   .claude/skills/video-batch-import/scripts/verify_coverage.sh "<Channel Name>" video_id1 video_id2 ...
   ```
   Flags any video with 0 `video_sections` rows even if `classified_by`
   said success — that combination has happened before. Any flagged video
   goes back to step 4.

6. **Report.** Summarize: how many succeeded first try, how many needed
   retries (and how many attempts), any still failing after 3 retries
   (with the real error), and the channel's final "N of N analysed" line
   from `verify_coverage.sh`'s output.

## Guardrails (all from real incidents in this project)

- **Never `git push`/deploy while a batch has jobs in flight.** Render's
  Docker deploys tear down the old container, silently killing every
  detached `auto_process_video.py` subprocess mid-call — no error is
  logged, the video just stays stuck at `classified_by='auto'` forever.
  If a deploy is genuinely needed mid-troubleshooting, wait for the
  current batch's jobs to finish (or fail) first. If you must deploy
  anyway, immediately follow up by re-running step 4 for every video
  that was still `auto`/in-flight at deploy time, on the assumption they
  were all orphaned.
- **Job status has no JSON API — poll `manifest.json` on disk, not a
  guessed endpoint.** `/video/<job_id>` returns HTML for humans. There is
  no `/video_status/<job_id>` or similar; assuming one exists caused a
  ~50-minute silent 404 loop previously.
- **`classified_by='claude'` is necessary but not sufficient.** Always run
  step 5's real row-count check before declaring a batch done.
- **If YouTube 403s on download** (seen after ~90 videos processed in one
  session), the fix already deployed is forcing yt-dlp's
  `extractor_args.youtube.player_client` to `["android", "ios", "web"]` in
  `instagram_transcriber/app.py` (all 3 `yt_dlp.YoutubeDL` call sites) —
  this works fully anonymously, no browser cookies needed. If 403s recur
  despite that, it's a new/different block; don't assume cookies are the
  fix without testing (they weren't, last time).
- **One video per SSH/retry call, not a chained loop.** A single bash
  invocation sequentially SSH-retrying many videos has been silently
  killed partway through by an apparent ~20-25 minute runtime cap on
  long-running tool calls. Keep each retry (and each poll chunk) its own
  bounded call.
