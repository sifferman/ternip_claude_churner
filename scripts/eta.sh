#!/usr/bin/env bash
# Estimate build ETA from build.log against a reference OneCore build.
#
# Reference: ternary_matmul4/.../xcu250_D=1024_OneCore (eq2, 2026-05-22)
#   Total wall-clock (Vivado launch -> impl finished): ~172 min
#
# Usage: scripts/eta.sh [path/to/build.log]

LOG="${1:-/soe/esifferm/GitHub/ternip_claude/build.log}"

python3 - <<'PY' "$LOG"
import re, sys, os, datetime
LOG = sys.argv[1]
try:
    text = open(LOG).read()
except FileNotFoundError:
    print(f"ETA: no log at {LOG}")
    sys.exit(0)

# Also pull in the impl_1 runme.log if present; phase-completion markers
# (opt_design/place_design/route_design completed) only appear there.
import glob
impl_logs = glob.glob('/soe/esifferm/GitHub/ternip_claude/ternary_matmul/synth/pynqvivado_au250/build/*/hw/_x/link/vivado/vpl/prj/prj.runs/impl_1/runme.log')
for ip in impl_logs:
    try:
        text += '\n' + open(ip).read()
    except Exception:
        pass

# Reference table: (pattern, reference offset minutes from Vivado-launch)
# Source: ternary_matmul4 OneCore vivado.log + impl_1/runme.log, eq2 2026-05-22
REF = [
    (r'Run vpl: Step create_project: Started',     0),
    (r'Run vpl: Step create_bd: Completed',        1),
    (r'Run vpl: Step generate_target: Completed',  3),
    (r'Run vpl: Step config_hw_runs: Completed',   5),
    (r'Run vpl: Step synth: Started',              5),
    (r'Run vpl: Step synth: Completed',           21),
    (r'Run vpl: Step impl: Started',              21),
    (r'my_rm_synth_1 finished',                   21),
    (r'Launched impl_1',                          21),
    (r'opt_design completed successfully',        28),
    (r'place_design completed successfully',      62),
    (r'phys_opt_design completed successfully.*\nphys_opt_design: Time \(s\): cpu = 00:13:23',  72),
    (r'Starting Routing Task',                    94),
    (r'route_design completed successfully',     132),
    (r'impl_1 finished',                         171),
    (r'Run vpl: Step impl: Completed',           172),
]
REF_TOTAL_MIN = 172

# Find build's Vivado-launch wallclock via first "[HH:MM:SS] Run vpl: Step create_project: Started"
m = re.search(r'\[(\d\d:\d\d:\d\d)\] Run vpl: Step create_project: Started', text)
file_mtime = datetime.datetime.fromtimestamp(os.path.getmtime(LOG))
if m:
    today = file_mtime.date()
    t = datetime.datetime.strptime(m.group(1), '%H:%M:%S').time()
    start_ts = datetime.datetime.combine(today, t)
    if start_ts > file_mtime + datetime.timedelta(hours=1):
        start_ts -= datetime.timedelta(days=1)
    start_src = "vpl-launch wallclock"
else:
    # sv2v phase before Vivado; use file mtime - guess for sv2v duration
    start_ts = file_mtime - datetime.timedelta(seconds=30)
    start_src = "log mtime (Vivado not started yet)"

# Last matched reference marker + its actual wallclock timestamp
last = None
for pattern, off in REF:
    if re.search(pattern, text):
        # Find the timestamp of the LAST occurrence of this pattern in the log
        # If the pattern is preceded by [HH:MM:SS] in the same line, capture it
        hits = list(re.finditer(r'\[(\d\d:\d\d:\d\d)\][^\n]*' + pattern, text))
        marker_ts = None
        if hits:
            t = datetime.datetime.strptime(hits[-1].group(1), '%H:%M:%S').time()
            marker_ts = datetime.datetime.combine(start_ts.date(), t)
            if marker_ts < start_ts:
                marker_ts += datetime.timedelta(days=1)
        last = (pattern, off, marker_ts)

now = datetime.datetime.now()
elapsed_s = (now - start_ts).total_seconds()
elapsed_min = elapsed_s / 60
eta_ref = start_ts + datetime.timedelta(minutes=REF_TOTAL_MIN)
remain_ref_s = (eta_ref - now).total_seconds()

if last:
    pattern, off, marker_ts = last
    if marker_ts is not None:
        marker_elapsed_min = (marker_ts - start_ts).total_seconds() / 60
        drift_min = marker_elapsed_min - off
        hit_str = f"hit +{marker_elapsed_min:.1f} min, drift {drift_min:+.1f} min"
    else:
        # No wall-clock on this marker. We only know it happened sometime
        # between when we hit the previous marker and now. Use 0 drift for
        # ETA projection (don't punish or reward what we can't measure).
        drift_min = 0.0
        hit_str = f"hit between previous marker and now (no [HH:MM:SS] for this one)"
    # Clamp drift to >=0 for ETA -- route_design varies and can eat lead time
    eta_adj = start_ts + datetime.timedelta(minutes=REF_TOTAL_MIN + max(drift_min, 0))
    remain_adj_s = (eta_adj - now).total_seconds()
    label = pattern.split('.*')[0][:55]
    print(f"ETA: elapsed {elapsed_min:5.1f} min | last marker: {label!r} (ref +{off} min, {hit_str}) | reference-ETA {eta_ref.strftime('%H:%M')} | drift-adj ETA {eta_adj.strftime('%H:%M')} ({remain_adj_s/60:.0f} min remaining)")
else:
    print(f"ETA: elapsed {elapsed_min:5.1f} min | pre-Vivado phase ({start_src}) | reference-ETA {eta_ref.strftime('%H:%M')} ({remain_ref_s/60:.0f} min remaining if Vivado started now)")
PY
