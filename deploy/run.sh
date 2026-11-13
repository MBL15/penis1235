#!/usr/bin/env bash
# Сборка образа и запуск сайта и бота с урезанными правами. Запускать на сервере из каталога проекта: bash deploy/run.sh
# Другие контейнеры и сайты сервера не затрагиваются.
set -euo pipefail
cd "$(dirname "$0")/.."

DATA="$PWD/data"
chmod 700 "$DATA"
chmod 600 "$DATA"/*.db .env .env.bot 2>/dev/null || true

sudo docker build -q -f deploy/Dockerfile.offline -t afisha:latest .

# Общие ограничения: не root, файловая система только для чтения (пишем лишь в /data и /tmp), без привилегий
# и возможностей ядра, потолок по памяти и числу процессов.
HARDEN=(
  --restart unless-stopped --network host
  --user 1000:1000 --read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m
  --cap-drop ALL --security-opt no-new-privileges
  --pids-limit 200 --memory 512m -e HOME=/tmp
  -v "$DATA":/data
)

sudo docker rm -f afisha-web afisha-bot >/dev/null 2>&1 || true
sudo docker run -d --name afisha-web "${HARDEN[@]}" --env-file .env afisha:latest
sudo docker run -d --name afisha-bot "${HARDEN[@]}" --env-file .env.bot afisha:latest python -m app.bot

sleep 4
sudo docker ps --format '{{.Names}} {{.Status}}' | grep afisha
curl -s -o /dev/null -w 'сайт: %{http_code}\n' http://127.0.0.1:8001/api/health
