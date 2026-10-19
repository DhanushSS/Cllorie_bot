#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec 9>.deploy.lock
flock -n 9 || { echo 'Another deployment is running.'; exit 1; }
test -f .env || { echo 'Create .env first (see README).'; exit 1; }
chmod 600 .env
if [[ ${1:-} != '--no-pull' ]]; then
  git pull --ff-only
fi
revision=$(git rev-parse --short HEAD)
image="calorie-bot:$revision"
docker build -t "$image" .
docker run --rm --env-file .env "$image" python -m py_compile bot.py conversation.py config.py db.py nutrition_api.py nvidia_client.py sheets_client.py charts.py
docker volume create calorie-bot-data >/dev/null
previous=$(docker inspect --format '{{.Image}}' calorie-bot 2>/dev/null || true)
if [[ -n "$previous" ]]; then
  docker image tag "$previous" calorie-bot:previous
  docker stop -t 30 calorie-bot >/dev/null
  docker rm calorie-bot >/dev/null
fi
start_image() {
  docker run -d --name calorie-bot --restart unless-stopped \
    --env-file "$PWD/.env" -e DB_PATH=/data/bot_state.db \
    -v calorie-bot-data:/data \
    --log-opt max-size=10m --log-opt max-file=3 "$1"
}
if ! start_image "$image"; then
  [[ -z "$previous" ]] || start_image calorie-bot:previous
  exit 1
fi
sleep 12
if [[ $(docker inspect --format '{{.State.Running}} {{.RestartCount}}' calorie-bot) != 'true 0' ]]; then
  echo 'Startup failed. Restoring the previous image when available; inspect logs.'
  docker stop -t 30 calorie-bot >/dev/null || true
  docker rm calorie-bot >/dev/null
  [[ -z "$previous" ]] || start_image calorie-bot:previous
  exit 1
fi
echo "Running $image. Check Telegram and docker logs --tail 30 calorie-bot."
