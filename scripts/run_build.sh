#!/usr/bin/env bash
# Kick off a Vivado build on eq2 in the background.
#
# Usage: scripts/run_build.sh [CONFIG]
#   CONFIG defaults to xcu250_D=1024_MaxCores

set -euo pipefail

CONFIG="${1:-xcu250_D=1024_OneCore}"
REPO_DIR="/soe/esifferm/GitHub/ternip_claude"
PROJECT_DIR="$REPO_DIR/ternary_matmul"
LOG="$REPO_DIR/build.log"

echo "Killing previous esifferm processes on eq2..."
ssh eq2 pkill -u esifferm 2>/dev/null || true

# Brief pause so the kill takes effect before we re-ssh
sleep 2

echo "Starting build on eq2: CONFIG=$CONFIG"
ssh eq2 bash <<EOF
cd "$PROJECT_DIR"
nohup make pynqvivado_au250_hw CONFIG="$CONFIG" > "$LOG" 2>&1 &
disown
echo "Build PID: \$!"
EOF

echo "Build kicked off. Log: $LOG"
echo "Poll with: scripts/poll_build.sh"
