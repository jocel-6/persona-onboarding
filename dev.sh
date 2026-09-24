#!/usr/bin/env bash
# Run backend (port 8000) and frontend (port 3000) together. Ctrl+C stops both.
set -euo pipefail
cd "$(dirname "$0")"
trap 'kill 0' EXIT
(cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000) &
(cd frontend && npm run dev) &
wait
