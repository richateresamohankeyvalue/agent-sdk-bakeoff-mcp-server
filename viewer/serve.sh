#!/usr/bin/env bash
# Serves the repo root over HTTP so the viewer can fetch data/*.json
# (fetch() of file:// URLs is blocked by CORS, so this needs a real server).
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${1:-8765}"
echo "Serving at http://localhost:${PORT}/viewer/  (Ctrl+C to stop)"
python3 -m http.server "$PORT"
