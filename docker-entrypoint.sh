#!/usr/bin/env bash
set -euo pipefail

# Simple entrypoint: wait for DB to accept connections by running migrations
# with retries, then exec the given command. This avoids starting the web
# process before the DB is ready.

MAX_RETRIES=${MAX_RETRIES:-10}
SLEEP_SECONDS=${SLEEP_SECONDS:-3}

echo "Waiting for database and running migrations (retries=${MAX_RETRIES})..."
retries=0
until python manage.py migrate --noinput; do
  retries=$((retries+1))
  if [ "$retries" -ge "$MAX_RETRIES" ]; then
    echo "Migrations failed after $retries attempts. Exiting."
    exit 1
  fi
  echo "Waiting for DB... (attempt $retries/$MAX_RETRIES)"
  sleep ${SLEEP_SECONDS}
done

echo "Migrations applied. Running: $@"

exec "$@"
