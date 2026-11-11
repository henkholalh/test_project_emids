#!/usr/bin/env bash
# Start the supplied mock API from PACK_DIR (default: ../T4_Lead_AI_Architect_A).
set -euo pipefail
cd "$(dirname "$0")/.."
PACK_DIR="${PACK_DIR:-../T4_Lead_AI_Architect_A}"
PORT="${PORT:-8090}"
exec python3 -I "$PACK_DIR/mock_services/server.py" --port "$PORT"
