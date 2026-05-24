---
name: vivado-read-reports
description: Inspect a completed Vivado/Vitis FPGA build and produce a timing report CSV sized to exactly the number of failing nets. Use this skill whenever the user asks to "read", "check", "inspect", "analyze", or "look at" a Vivado build's timing -- including phrases like "what's the WNS", "show me the failing paths", "how bad is timing on this build", or "pull a timing CSV from this xpr". Triggered by /vivado-read-reports.
---

# vivado-read-reports

Open a Vivado project, query the most recent implementation run, and produce:

- **WNS** (worst negative slack) reported to the user
- **TNS** (total negative slack) reported to the user
- A **CSV** at the requested output path containing exactly one row per failing setup endpoint (no padding, no truncation)

The CSV column format matches Vivado's GUI **Reports → Timing → Timing Summary → Export to Spreadsheet** flow:

```
From,Name,Slack,Levels,High Fanout,To,Total Delay,Logic Delay,Net Delay,Requirement,Source Clock,Destination Clock,Exception,Clock Uncertainty
```

That's the same format as this project's existing `hard_N.csv` files, so the output can be fed straight into the same clustering analysis scripts.

## When to use

Trigger this skill when the user invokes `/vivado-read-reports <path>`, or when they describe wanting timing data out of an existing Vivado project (`.xpr`). It opens the project in batch-mode Vivado — no GUI, no rerun of synthesis/place/route.

If the user just gives a build directory or doesn't supply an `.xpr` explicitly, look for `*.xpr` underneath the path they gave (Vitis link projects live at `.../link/vivado/vpl/prj/prj.xpr`).

## Inputs

1. **`<xpr_path>`** — path to a `.xpr`. Required.
2. **`<csv_output_path>`** — optional. If omitted, write the CSV next to the `.xpr` as `vivado_timing.csv`.
3. **`<cells_filter>`** — optional cell hierarchy pattern (e.g.
   `level0_i/level1/level1_i/ulp/ternip_ip_1`). When set, restricts the
   timing report to setup paths whose start AND end points are inside
   that hierarchy — mirrors the GUI Timing Summary panel's `Cells:` field
   / `report_timing -cells` behavior. **Strongly recommended for Vitis
   builds**, where unfiltered reports also pick up paths through platform
   infrastructure (DPA monitors, AXI interconnect, debug instrumentation)
   that don't gate bitstream generation and dilute the relevant cluster
   analysis. Without the filter you'll get thousands of failing paths
   even when the user's IP is timing-clean.

This project's IP filter for OneCore builds:
`level0_i/level1/level1_i/ulp/ternip_ip_1`

## How it works

The skill drives Vivado via a Tcl helper at `scripts/generate_timing_csv.tcl`:

1. `open_project <xpr>` — load the project metadata (no re-elaboration).
2. Pick the most recent **routed** implementation run via `get_runs -filter {IS_IMPLEMENTATION}` and a status check (prefers `impl_1` when present).
3. `open_run <run>` — pull the post-route design database into memory so timing path queries work.
4. `get_timing_paths -setup -slack_lesser_than 0 -max_paths 1000000 -nworst 1` — query exactly the failing setup endpoints. `-nworst 1` matches the GUI's "one path per endpoint" view (the same view that gets exported to spreadsheet).
5. Compute **WNS** = slack of the worst-slack path in that set; **TNS** = sum of slacks. Report count = `[llength $failing_paths]`.
6. Iterate, pulling these properties per path and writing one CSV row per path:
   - `STARTPOINT_PIN` → `From`
   - `"Path $i"` → `Name`
   - `SLACK` → `Slack`
   - `LOGIC_LEVELS` → `Levels`
   - `MAX_FANOUT` → `High Fanout`
   - `ENDPOINT_PIN` → `To`
   - `DATAPATH_DELAY` → `Total Delay`
   - `DATAPATH_LOGIC_DELAY` → `Logic Delay`
   - `DATAPATH_NET_DELAY` → `Net Delay`
   - `REQUIREMENT` → `Requirement`
   - `STARTPOINT_CLOCK` → `Source Clock`
   - `ENDPOINT_CLOCK` → `Destination Clock`
   - `EXCEPTION` → `Exception`
   - `CLOCK_UNCERTAINTY` → `Clock Uncertainty`

## Invocation

Run the helper Tcl in batch-mode Vivado. The command is:

```bash
vivado -mode batch -nojournal -nolog \
    -source SKILL_DIR/scripts/generate_timing_csv.tcl \
    -tclargs <xpr_path> <csv_output_path> [cells_filter]
```

Resolve `SKILL_DIR` to the directory holding this `SKILL.md`. Run from any working directory — the Tcl script uses absolute paths internally.

The script emits four marker lines on stdout that this skill should grep for and surface to the user:

```
VIVADO_READ_REPORTS_WNS=<slack ns, e.g. -0.572>
VIVADO_READ_REPORTS_TNS=<slack ns, e.g. -19.497>
VIVADO_READ_REPORTS_NFAIL=<integer, e.g. 258>
VIVADO_READ_REPORTS_CSV=<absolute path>
```

The skill should report these to the user in a compact summary:

```
WNS: <value> ns
TNS: <value> ns
Failing paths: <value>
CSV: <path>
```

Then a final check: confirm `wc -l <csv>` equals `NFAIL + 1` (header row + one data row per failure) so the user knows the file is correctly sized.

## Behavior notes & edge cases

- **No failing paths (WNS ≥ 0)**: the script still writes the CSV (header only) and reports `NFAIL=0`. That's the success case for the user's timing question.
- **Project hasn't been routed**: `open_run` will pick the most-progressed run; if route_design didn't complete, paths may be post-place rather than post-route. The script reports the run name and status so the user can see what stage they're looking at.
- **Multiple impl runs**: prefers `impl_1` when routed; otherwise picks the first routed run from `get_runs`.
- **Vitis xclbin link project**: live at `<build>/_x/link/vivado/vpl/prj/prj.xpr`. Same structure as a regular Vivado project once opened.
- **Long runtime**: opening a routed run with many constraints can take 2-5 minutes on a large design (e.g. xcu250). That's expected; the skill is not "stuck."

## Testing

A quick smoke test on this project (scoped to the ternip IP, which is what
the user has historically been filtering on in the GUI export):

```bash
vivado -mode batch -nojournal -nolog \
    -source .claude/skills/vivado-read-reports/scripts/generate_timing_csv.tcl \
    -tclargs synth/pynqvivado_au250/build/xcu250_D=1024_OneCore/hw/_x/link/vivado/vpl/prj/prj.xpr \
    /tmp/vrr_test.csv \
    level0_i/level1/level1_i/ulp/ternip_ip_1
```

Expected: WNS/TNS values that roughly match the most recent `hard_*.csv` you exported manually, and `wc -l /tmp/vrr_test.csv` = NFAIL + 1.

## Output format reference

The reference CSV format this skill targets:

```csv
From,Name,Slack,Levels,High Fanout,To,Total Delay,Logic Delay,Net Delay,Requirement,Source Clock,Destination Clock,Exception,Clock Uncertainty
some/path/cell/Q,Path 1,-0.572,4,323,some/other/path/D,3.16,0.47,2.69,3.33,kernel_clk,kernel_clk,,0.035
...
```

Existing project examples: `hard_5.csv`, `hard_6.csv`, `hard_8.csv`, `hard_9.csv` at the project root. The skill's output should be analyzable by the same Python clustering scripts that the user has been using on those files.
