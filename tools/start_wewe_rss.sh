#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-/opt/wechat-OA/wechat-OA}"
DATA_DIR="${2:-/data/wewe-rss/data}"
mkdir -p "$PROJECT_DIR" "$DATA_DIR"

if docker ps -a --format '{{.Names}}' | grep -qx 'wewe-rss'; then
  docker start wewe-rss >/dev/null
  echo "wewe-rss container already exists and is started."
  exit 0
fi

IMAGE="cooderl/wewe-rss-sqlite:latest"
if ! docker pull "$IMAGE"; then
  IMAGE="swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/cooderl/wewe-rss-sqlite:latest"
  docker pull "$IMAGE"
fi
docker run -d \
  --name wewe-rss \
  --restart unless-stopped \
  -p 4000:4000 \
  -e DATABASE_TYPE=sqlite \
  -e SERVER_ORIGIN_URL=http://localhost:4000 \
  -v "$DATA_DIR:/app/data" \
  "$IMAGE"

echo "wewe-rss container started."
