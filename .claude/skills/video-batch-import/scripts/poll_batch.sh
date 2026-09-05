#!/bin/bash
# poll_batch.sh — polls instagram_transcriber/results/<job_id>/manifest.json
# directly off disk (there is NO JSON status endpoint — /video/<job_id>
# returns HTML, and guessing an endpoint name here previously caused a
# ~50-minute silent 404 loop) until every job reaches status done/error,
# or a bounded time window elapses.
#
# Deliberately bounded (default 720s) rather than looping indefinitely:
# a single long-running background poll has been observed getting
# silently killed by a runtime cap around 20-25 minutes with no error.
# Call this repeatedly (each call is a fresh, independent invocation) until
# it reports ALL_DONE.
#
# Usage: poll_batch.sh <results_dir> <max_seconds> job_id1 [job_id2 ...]
# Prints one "<job_id> <status> <stage>" line per job, then a final
# "ALL_DONE" or "CHUNK_TIMEOUT" line.

set -uo pipefail

RESULTS_DIR="$1"
MAX_SECONDS="$2"
shift 2
JOBS=("$@")

cd "$RESULTS_DIR"
start=$(date +%s)

while true; do
  all_done=1
  for jid in "${JOBS[@]}"; do
    if [ ! -f "$jid/manifest.json" ]; then
      all_done=0
      continue
    fi
    st=$(python3 -c "import json;print(json.load(open('$jid/manifest.json')).get('status'))" 2>/dev/null)
    if [ "$st" != "done" ] && [ "$st" != "error" ]; then
      all_done=0
    fi
  done
  elapsed=$(( $(date +%s) - start ))
  if [ "$all_done" = "1" ]; then
    break
  fi
  if [ "$elapsed" -gt "$MAX_SECONDS" ]; then
    break
  fi
  sleep 15
done

for jid in "${JOBS[@]}"; do
  if [ -f "$jid/manifest.json" ]; then
    python3 -c "
import json
m = json.load(open('$jid/manifest.json'))
print('$jid', m.get('status'), '/', m.get('stage'), '/ ppe_video_id:', m.get('ppe_video_id'),
      '/ classified_by:', m.get('classified_by'), '/ err:', m.get('classification_error') or m.get('error'))
"
  else
    echo "$jid NO_MANIFEST"
  fi
done

if [ "$all_done" = "1" ]; then
  echo "ALL_DONE"
else
  echo "CHUNK_TIMEOUT"
fi
