#!/bin/bash
# submit_batch.sh — POSTs a list of YouTube URLs to the local Bulk
# Transcriber's /ingest_video route and prints "<url> <job_id>" per line.
#
# Usage: submit_batch.sh "<channel name>" url1 [url2 ...]
#
# The route responds with a 302 redirect to /video/<job_id> — job_id is
# parsed from the Location header, never from the response body (the body
# is just the index page's HTML, which redirect responses don't render).

set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 \"<channel name>\" url1 [url2 ...]" >&2
  exit 1
fi

CHANNEL="$1"
shift

CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://localhost:5001/ 2>&1 || echo "000")
if [ "$CODE" != "200" ]; then
  echo "ERROR: local Bulk Transcriber is not responding at http://localhost:5001 (got HTTP $CODE)." >&2
  echo "Start it with: cd instagram_transcriber && ./run.sh" >&2
  exit 1
fi

for url in "$@"; do
  loc=$(curl -s -X POST http://localhost:5001/ingest_video \
    -F "url=$url" \
    -F "channel=$CHANNEL" \
    -F "cookies_from_browser=" \
    -D - -o /dev/null | grep -i "^Location:" | tr -d '\r' | awk '{print $2}')
  job_id=$(basename "$loc")
  if [ -z "$job_id" ] || [ "$job_id" = "." ]; then
    echo "$url SUBMIT_FAILED" >&2
  else
    echo "$url $job_id"
  fi
done
