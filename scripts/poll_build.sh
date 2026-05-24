#!/usr/bin/env bash
# Print a one-line status of the most recent build.log, suitable for use
# in a polling loop.
#
# Exit codes:
#   0 = build still running
#   1 = build completed successfully (impl finished, bitstream generated)
#   2 = build failed (segfault, error, vpl Failed)
#   3 = no build.log

set -uo pipefail

LOG="${1:-/soe/esifferm/GitHub/ternip_claude/build.log}"

if [[ ! -f "$LOG" ]]; then
    echo "no build.log at $LOG"
    exit 3
fi

# Look for terminal markers near the end of the log.
tail -100 "$LOG" > /tmp/poll_$$.tail

# Success: VPL completed
if grep -q "Run vpl: Step impl: Completed" /tmp/poll_$$.tail; then
    echo "SUCCESS: VPL completed"
    rm -f /tmp/poll_$$.tail
    exit 1
fi

# Hard failures
if grep -qE "Segmentation fault|core dumped|vpl: Failed|Error 1[34]4|make:.*Error" /tmp/poll_$$.tail; then
    last_err=$(grep -E "Segmentation fault|vpl: Failed|^Error|undefined symbol" /tmp/poll_$$.tail | tail -1)
    echo "FAILED: $last_err"
    rm -f /tmp/poll_$$.tail
    exit 2
fi

# Otherwise: still running. Show the latest meaningful line.
phase=$(grep -E "^Phase|^\[..:..:..\]|^Starting|Finished" "$LOG" | tail -1)
echo "RUNNING: $phase"
rm -f /tmp/poll_$$.tail
exit 0
