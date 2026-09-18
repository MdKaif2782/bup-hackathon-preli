#!/usr/bin/env bash
# Release + deploy in one step:
#   1. build a multi-arch image and push damegami2782/gridwise:<tag> to Docker Hub
#   2. on the VPS, pull that exact tag and recreate the container
# Usage: scripts/deploy_vps.sh v1.0.1 [ssh-target]
# The VPS keeps its own /opt/gridwise/.env (GROQ_* secrets, HOST_BIND=127.0.0.1, HOST_PORT=8090,
# GRIDWISE_IMAGE); nginx proxies https://bup-preli-la-team.inovate.it.com -> 127.0.0.1:8090.
set -euo pipefail
TAG="${1:?usage: deploy_vps.sh <tag> [ssh-target]}"
TARGET="${2:-root@93.127.199.251}"
IMAGE="damegami2782/gridwise:${TAG}"
cd "$(dirname "$0")/.."

docker buildx inspect gw-builder >/dev/null 2>&1 || docker buildx create --name gw-builder --driver docker-container >/dev/null
docker buildx build --builder gw-builder --platform linux/amd64,linux/arm64 -t "$IMAGE" -t damegami2782/gridwise:latest --push .

scp -q docker-compose.yml "$TARGET:/opt/gridwise/"
ssh "$TARGET" "set -e; cd /opt/gridwise
docker pull -q $IMAGE
sed -i '/^GRIDWISE_IMAGE=/d' .env && echo GRIDWISE_IMAGE=$IMAGE >> .env
docker compose up -d --no-build --force-recreate
for i in \$(seq 1 30); do curl -fsS 127.0.0.1:8090/health && exit 0; sleep 1; done; echo 'health check failed'; exit 1"
echo; echo "deployed $IMAGE"
