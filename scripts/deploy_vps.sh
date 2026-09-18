#!/usr/bin/env bash
# Build a linux/amd64 image, ship it to the VPS over SSH and restart the container.
# Usage: scripts/deploy_vps.sh [ssh-target]   (default root@93.127.199.251)
# The VPS keeps its own /opt/gridwise/.env (GROQ_* secrets, HOST_BIND=127.0.0.1, HOST_PORT=8090);
# nginx proxies https://bup-preli-la-team.inovate.it.com -> 127.0.0.1:8090.
set -euo pipefail
TARGET="${1:-root@93.127.199.251}"
cd "$(dirname "$0")/.."
docker buildx build --platform linux/amd64 -t gridwise:amd64 --load .
scp -q docker-compose.yml "$TARGET:/opt/gridwise/"
docker save gridwise:amd64 | gzip | ssh "$TARGET" 'gunzip | docker load'
ssh "$TARGET" 'cd /opt/gridwise && docker tag gridwise:amd64 gridwise:latest && docker compose up -d --no-build --force-recreate && sleep 4 && curl -fsS 127.0.0.1:8090/health && echo'
