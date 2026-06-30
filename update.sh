#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

mkdir -p backups
if [ -d data ]; then
  tar -czf "backups/data-$(date +%Y%m%d-%H%M%S).tar.gz" data
fi

docker compose pull || true
docker compose up -d --build --remove-orphans
docker compose ps
