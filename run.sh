#!/usr/bin/env bash
# Start the whole app - API, vision-service and frontend - in Docker.
# (The database is hosted Supabase, reached over HTTPS - nothing local.)
#
#   ./run.sh          start everything (rebuilds if code changed)
#   ./run.sh stop     stop everything
#   ./run.sh logs     follow the logs
#
# Everything runs detached, so this returns to your prompt when it's ready.
set -euo pipefail
cd "$(dirname "$0")"

API_URL="http://localhost:8000"
APP_URL="http://localhost:8080"

case "${1:-start}" in
  stop)
    echo "==> Stopping..."
    docker compose down
    echo "Done."
    exit 0
    ;;
  logs)
    exec docker compose logs -f
    ;;
  start) ;;
  *)
    echo "Usage: ./run.sh [start|stop|logs]" >&2
    exit 1
    ;;
esac

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker isn't running. Start Docker Desktop, then re-run this." >&2
  exit 1
fi

if [ ! -f backend/.env ]; then
  cp backend/.env.example backend/.env
  echo "==> Created backend/.env from .env.example"
fi

echo "==> Building and starting containers..."
docker compose up -d --build

echo "==> Waiting for the API..."
for _ in $(seq 1 90); do
  if curl -fsS "$API_URL/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl -fsS "$API_URL/health" >/dev/null 2>&1; then
  echo "ERROR: the API never became healthy. Check: ./run.sh logs" >&2
  exit 1
fi

echo
echo "  App        $APP_URL"
echo "  API docs   $API_URL/docs"
echo
echo "  No account yet?  $APP_URL/register.html"
echo "  Need a funded test account?"
echo "      docker compose exec backend python -m scripts.seed_dev_user"
echo
echo "  ./run.sh logs   follow logs"
echo "  ./run.sh stop   stop everything"
echo
