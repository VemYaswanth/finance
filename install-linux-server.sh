#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install Docker Engine and the Docker Compose plugin first."
  echo "Ubuntu/Debian quick path: https://docs.docker.com/engine/install/ubuntu/"
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose plugin is not installed. Install docker-compose-plugin first."
  exit 1
fi

mkdir -p data/users

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Edit .env if you want a different APP_PORT or AI settings."
fi

docker compose up -d --build

PORT="$(grep -E '^APP_PORT=' .env | cut -d= -f2- || true)"
PORT="${PORT:-8000}"

echo
echo "Financial Review is running in Docker."
echo "Open: http://<server-ip>:${PORT}"
echo "Create the admin account by logging in with username: admin"
