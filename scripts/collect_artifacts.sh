#!/usr/bin/env bash
# Collect build artifacts after a (successful or failed) Vivado build.
# Produces:
#   - <tag>.csv                (per-endpoint timing CSV via vivado-read-reports)
#   - build.tar.gz            (tarred synth/pynqvivado_au250/build, capped size)
#   - summary.md              (WNS/TNS/failing count + top cluster description)
#
# Usage: scripts/collect_artifacts.sh <tag> [config]
#   tag    = release tag suffix (e.g. "build_16")
#   config = build CONFIG, default xcu250_D=1024_MaxCores

set -euo pipefail

TAG="${1:?usage: collect_artifacts.sh <tag> [config]}"
CONFIG="${2:-xcu250_D=1024_OneCore}"

REPO_DIR="/soe/esifferm/GitHub/ternip_claude"
PROJECT_DIR="$REPO_DIR/ternary_matmul"
BUILD_DIR="$PROJECT_DIR/synth/pynqvivado_au250/build/$CONFIG"
XPR="$BUILD_DIR/hw/_x/link/vivado/vpl/prj/prj.xpr"

OUT_DIR="$REPO_DIR/artifacts/$TAG"
mkdir -p "$OUT_DIR"

echo "=== Timing CSV ==="
if [[ -f "$XPR" ]]; then
    vivado -mode batch -nojournal -nolog \
        -source "$REPO_DIR/.claude/skills/vivado-read-reports/scripts/generate_timing_csv.tcl" \
        -tclargs "$XPR" "$OUT_DIR/$TAG.csv" "level0_i/level1/level1_i/ulp/ternip_ip_1" \
        2>&1 | grep -E "VIVADO_READ_REPORTS_|Wrote" || true
else
    echo "WARN: no XPR at $XPR — build did not reach route_design. Skipping CSV."
fi

echo "=== Summary ==="
SUMMARY="$OUT_DIR/summary.md"
{
    echo "# $TAG"
    echo
    if [[ -f "$OUT_DIR/$TAG.csv" ]]; then
        echo "## Timing"
        awk -F',' 'NR>1 {sum+=$3; if($3<min || NR==2) min=$3} END {printf "- **WNS**: %.3f ns\n- **TNS**: %.3f ns\n- **Failing endpoints**: %d\n", min, sum, NR-1}' "$OUT_DIR/$TAG.csv"
        echo
        echo "## Top failing cluster"
        echo '```'
        awk -F',' 'NR>1 {
          src=$1; gsub("level0_i/level1/level1_i/ulp/ternip_ip_1/inst/", "", src)
          n=split(src,p,"/"); key=p[1]; if(n>=2)key=key"/"p[2]; if(n>=3)key=key"/"p[3]
          c[key]++; if($3+0<w[key]||!(key in w)) w[key]=$3+0
        } END {for(k in c) printf "%5d  %8.3f  %s\n", c[k], w[k], k}' "$OUT_DIR/$TAG.csv" \
            | sort -rn | head -5
        echo '```'
    else
        echo "(no timing data — build did not produce a routed design)"
    fi
    echo
    echo "## Build notes"
    if grep -qE "Segmentation fault" "$REPO_DIR/build.log" 2>/dev/null; then
        echo "- Vivado segfault during route. (Rerun with no RTL change.)"
    fi
    if grep -qE "AUTO-FREQ-SCALING-04" "$REPO_DIR/build.log" 2>/dev/null; then
        freq=$(grep "AUTO-FREQ-SCALING-04" "$REPO_DIR/build.log" | grep -oE '[0-9.]+ MHz' | tail -1)
        echo "- AUTO-FREQ-SCALING-04 fired: kernel clock auto-dropped to $freq"
    fi
    if grep -qE "undefined symbol: xclProbe" "$REPO_DIR/build.log" 2>/dev/null; then
        echo "- Cosmetic XRT lib error at end of build (after bitstream). Ignore."
    fi
} > "$SUMMARY"

cat "$SUMMARY"

echo
echo "=== Tar build dir ==="
if [[ -d "$BUILD_DIR" ]]; then
    # Exclude hw_emu/ — it's a stale subdirectory holding ~5GB of old
    # hw_emu run artifacts (xclbin + huggingface_cache + v++ temps) that
    # accumulates inside the MaxCores build dir whenever someone runs
    # `make pynqvivado_au250_hw_emu CONFIG=xcu250_D=1024_MaxCores`. It
    # has nothing to do with the hardware build and bloats every tar by
    # ~4GB, pushing it above GitHub's 2GB release-asset limit.
    tar czf "$OUT_DIR/build.tar.gz" --exclude="$CONFIG/hw_emu" \
        -C "$PROJECT_DIR/synth/pynqvivado_au250/build" "$CONFIG" 2>&1 | tail -5 || true
    ls -lh "$OUT_DIR/build.tar.gz"
    # GitHub caps a single release asset at 2GB. If the tar is larger, split
    # into ~1.8GB numbered parts so make_release.sh can upload them all.
    # Reassemble with: cat build.tar.gz.part_* | tar xzf -
    rm -f "$OUT_DIR"/build.tar.gz.part_*
    tar_size=$(stat -c %s "$OUT_DIR/build.tar.gz")
    if (( tar_size > 1900000000 )); then
        echo "build.tar.gz is $((tar_size/1000000))MB > 2GB — splitting into parts"
        split -b 1800M -d "$OUT_DIR/build.tar.gz" "$OUT_DIR/build.tar.gz.part_"
        rm -f "$OUT_DIR/build.tar.gz"
        ls -lh "$OUT_DIR"/build.tar.gz.part_*
    fi
else
    echo "WARN: no build dir at $BUILD_DIR — skipping tar"
fi

echo
echo "Artifacts collected at: $OUT_DIR"
