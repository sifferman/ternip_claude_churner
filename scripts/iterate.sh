#!/usr/bin/env bash
# End-to-end iteration script. Use only AFTER you've already done the RTL
# change, run sims, and pushed to the submodule forks. This script just
# wraps: kick build, poll, collect, release.
#
# Usage: scripts/iterate.sh <tag> <title> [config]
#   tag    = release tag (e.g. hard_16)
#   title  = release title (one-line summary of the change)
#   config = build CONFIG, default xcu250_D=1024_MaxCores

set -euo pipefail

TAG="${1:?usage: iterate.sh <tag> <title> [config]}"
TITLE="${2:?missing title}"
CONFIG="${3:-xcu250_D=1024_MaxCores}"

REPO_DIR="/soe/esifferm/GitHub/ternip_claude"
cd "$REPO_DIR"

"$REPO_DIR/scripts/run_build.sh" "$CONFIG"

echo "Polling build.log every 10 minutes..."
while true; do
    sleep 600
    status=$("$REPO_DIR/scripts/poll_build.sh" "$REPO_DIR/build.log") || rc=$?
    rc=${rc:-0}
    echo "$(date +%H:%M:%S)  $status"
    [[ $rc -eq 0 ]] || break
done

# Whether success or failure, collect what we can.
"$REPO_DIR/scripts/collect_artifacts.sh" "$TAG" "$CONFIG"

# Make the release
"$REPO_DIR/scripts/make_release.sh" "$TAG" "$TITLE" "$REPO_DIR/artifacts/$TAG/summary.md"
