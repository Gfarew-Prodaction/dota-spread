#!/bin/zsh
set -e
project_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$project_dir"
exec .venv/bin/python -m backend.local_worker --stop
