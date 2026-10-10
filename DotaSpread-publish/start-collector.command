#!/bin/zsh
set -e
project_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$project_dir"
if [ ! -f .env.collector ]; then
  (umask 077; cp .env.collector.example .env.collector)
fi
chmod 600 .env.collector
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import flask, requests, ijson, psycopg, psycopg_pool' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r backend/requirements.lock.txt
fi
echo 'Dota Spread: домашний сборщик. Остановка — Ctrl+C.'
# Prevent idle system sleep while collecting; the screen may still turn off.
exec /usr/bin/caffeinate -i .venv/bin/python -m backend.local_worker
