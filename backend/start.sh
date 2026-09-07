#!/usr/bin/env bash
# Kept because flow.md names this as the backend's one-command start.
# The compose file now lives at the repo root and starts the frontend too,
# so this just delegates - ../run.sh is the single real implementation.
set -euo pipefail
exec "$(dirname "$0")/../run.sh" "$@"
