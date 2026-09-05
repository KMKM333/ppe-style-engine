#!/bin/bash
# verify_coverage.sh — confirms a batch actually landed correctly.
# classified_by='claude' alone is NOT sufficient proof — the breakdown
# merge has been observed succeeding at the classification step while
# still leaving 0 rows in video_sections/video_points. This checks real
# row counts per video_id, then reports the channel's overall
# "N of N analysed" coverage from the live site.
#
# Usage: verify_coverage.sh <channel_name_url_encoded_platform_query> video_id1 [video_id2 ...]
#   (channel_name arg here is only used for the final grep match, pass the
#    plain channel name e.g. "Johnny Harris")

set -uo pipefail

CHANNEL_NAME="$1"
shift
VIDEO_IDS=("$@")
SSH_HOST="srv-d9uuaqqd0e5s73ch02kg@ssh.oregon.render.com"

VID_LIST=$(IFS=,; echo "${VIDEO_IDS[*]}")

ssh -o ConnectTimeout=15 -o BatchMode=yes "$SSH_HOST" "cd /app/scripts && python3 -c \"
from db_init import get_conn
conn = get_conn()
for vid in [$VID_LIST]:
    s = conn.execute('SELECT COUNT(*) FROM video_sections WHERE video_id=?', (vid,)).fetchone()[0]
    p = conn.execute('SELECT COUNT(*) FROM video_points WHERE video_id=?', (vid,)).fetchone()[0]
    v = conn.execute('SELECT COUNT(*) FROM video_visuals WHERE video_id=?', (vid,)).fetchone()[0]
    cb = conn.execute('SELECT classified_by FROM video_attributes WHERE video_id=?', (vid,)).fetchone()
    flag = ' <<< ZERO SECTIONS, RETRY NEEDED' if s == 0 else ''
    print(vid, 'sections=', s, 'points=', p, 'visuals=', v, 'classified_by=', cb[0] if cb else None, flag)
\"" 2>&1 | grep -v "client_global_hostkeys_prove_confirm"

echo "--- channel coverage ---"
curl -s --max-time 15 "https://ppe-style-engine.onrender.com/channels?platform=YouTube" \
  | grep -B1 -A6 "$CHANNEL_NAME" | grep -E "$CHANNEL_NAME|analysis breakdown"
