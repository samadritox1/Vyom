#!/usr/bin/env sh
set -e
if [ -f .venv/bin/activate ]; then . .venv/bin/activate; fi
python -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
