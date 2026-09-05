#!/bin/bash
# retry_needs_review.sh — re-runs classification for ONE video_id on
# production via SSH. Deliberately one video per invocation (never
# chained in a single sequential loop over many videos) — a chained
# sequential loop retrying 10 videos was observed getting silently
# killed by a runtime cap after 6 of 10 completed. Call this once per
# needs_review video_id, as independent tool calls.
#
# Usage: retry_needs_review.sh <video_id>

set -euo pipefail

VIDEO_ID="$1"
SSH_HOST="srv-d9uuaqqd0e5s73ch02kg@ssh.oregon.render.com"

ssh -o ConnectTimeout=15 -o BatchMode=yes "$SSH_HOST" \
  "cd /app/scripts && python3 auto_process_video.py --video_id $VIDEO_ID" 2>&1 \
  | grep -v "client_global_hostkeys_prove_confirm"
