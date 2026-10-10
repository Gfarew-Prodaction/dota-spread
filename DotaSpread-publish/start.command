#!/bin/zsh
set -e
project_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$project_dir"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r backend/requirements.lock.txt
fi
if ! .venv/bin/python -c 'import flask, requests, ijson, psycopg, psycopg_pool' >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r backend/requirements.lock.txt
fi
if [ ! -d frontend/node_modules ]; then
  (cd frontend && npm ci)
fi
(cd frontend && npm run build)
echo 'Dota Spread: http://127.0.0.1:5050'
echo 'Открой этот адрес в браузере. Для остановки нажми Ctrl+C.'
exec .venv/bin/python -m backend.app
