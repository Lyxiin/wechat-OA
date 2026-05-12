#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-/opt/wechat-OA/wechat-OA}"
CRON_FILE="$(mktemp)"

mkdir -p "$PROJECT_DIR/logs"

(crontab -l 2>/dev/null || true) | awk '
  /# BEGIN wechat-OA auto pipeline/ { skip=1; next }
  /# END wechat-OA auto pipeline/ { skip=0; next }
  skip != 1 { print }
' > "$CRON_FILE"

cat >> "$CRON_FILE" <<CRON
# BEGIN wechat-OA auto pipeline
*/30 7-10 * * 1-5 /usr/bin/flock -n /tmp/wechat_oa_pipeline.lock /bin/bash -lc 'cd $PROJECT_DIR && mkdir -p logs && python3 auto_pipeline.py run >> logs/pipeline.log 2>&1'
# END wechat-OA auto pipeline
CRON

crontab "$CRON_FILE"
rm -f "$CRON_FILE"
crontab -l
