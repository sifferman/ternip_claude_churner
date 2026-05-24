# CLAUDE.md — ternip_claude autonomous loop

You are running in `--dangerously-skip-permissions` mode on a self-driving
FPGA-timing-optimization harness. Your job is to **iterate on the design,
build it, record results, and try again** — for days, without supervision.

## What this repo is

- `ternary_matmul/` — private fork of `sifferman/ternary_matmul` on
  branch `NumDdrBanksPerTmatmul`, URL `git@github.com:sifferman/
  ternary_matmul_claude.git`. The build harness: Makefile, configs, dv/,
  synth/ (incl. pynqvivado_au250).
- `ternary_matmul/third_party/ternip` — nested submodule pointing at the
  private ternip fork `git@github.com:sifferman/ternip_claude.git` on
  branch `NumDdrBanksPerTmatmul`. RTL building blocks: math (mul/div/
  sqrt/fixed-point convert), common (pipelined_mem,
  multioperand_accumulator, gearbox_fifo, pipelined_interconnect), fus
  (rms, tmatmul, loadstore, rowwise_operation, vector_registers),
  ternip_core (top of kernel logic). **There is no top-level `ternip/`
  submodule — only the nested one inside ternary_matmul.**
- `references/` — read-only sources of truth (style guide, Vivado docs,
  example projects, yosys source).
- `.claude/skills/` — skill scripts (`yosys-fanout`, `vivado-read-reports`,
  `vivado-utilization`). Invoke with `/<skill-name>` or by running the
  scripts directly.
- `scripts/` — workflow helpers for build / poll / collect / release.

## Never waste a run

**A Vivado build is 3–4 hours.** Treat every iteration as expensive. Before
committing to a build:

- The RTL change must pass `make lint` and both `make sim` simulators
  (verilator + vcs) for both `tmatmul_tb` and `rms_tb`. Always. No
  exceptions.
- Run `/yosys-fanout` to verify the structural intent (register inserted?
  fanout dropped? lane split honored?). Yosys takes minutes, not hours.
- Pick ONE change per iteration. If you're tempted to bundle two ideas,
  do them in two iterations — interpretability is more valuable than
  speed.
- If the same path keeps failing across 3+ iterations, you're at the
  wrong layer. Step back, re-read the timing CSV cluster, look at
  placement, don't just keep editing RTL.

A bad run costs 3–4 hours plus the storage of its tarball. A great run
costs the same. Spend the time pre-build to make sure you're getting a
great run.

## The loop (one iteration = one `hard_N` build)

```
┌─ Pick an optimization (see "What to try next" below)
│   • One RTL change at a time — never bundle two ideas in one build
├─ Edit RTL in ternary_matmul/ (or ternary_matmul/third_party/ternip/)
├─ Run sims and lint (REQUIRED — see "Verification" below)
├─ Run /yosys-fanout to verify structural impact (optional but cheap)
├─ Commit to the affected submodule(s), push to the private forks
├─ Update submodule pointers in this repo (ternip_claude_churner),
│   commit, push
├─ Tag + create a GitHub release on ternip_claude with a short blurb
│   and links to the submodule commits
├─ Kick off `make pynqvivado_au250_hw CONFIG=xcu250_D=1024_OneCore`
│   on eq2 (see "Build invocation" below). Build takes ~3-4 hours.
│   (Switch to MaxCores only once OneCore is close to passing.)
├─ Poll build.log every 5-15 minutes until done
├─ Collect artifacts:
│   • Tar synth/pynqvivado_au250/build → release asset
│   • Run /vivado-read-reports → CSV → release asset
│   • Compute WNS / TNS / failing-endpoint summary → release body
└─ Edit the release with results, then loop
```

## Target frequency

**300 MHz is the only valid target.** The board is unreliable at other
frequencies. If AUTO-FREQ-SCALING-04 fires (Vivado auto-drops the kernel
clock), the iteration didn't close timing — treat that as "didn't pass"
even if the build itself succeeded.

## Config to use (and what you may change)

**Default config: `config/xcu250_D=1024_OneCore.svh`.** Start every
iteration with this. The only parameters in OneCore you are allowed to
modify:

- `VectorParallelism`
- `LutParallelism`
- `CoreInterconnectNumStages`

Once OneCore is close to passing (WNS slack within ~0.1 ns of 0), switch
to `config/xcu250_D=1024_MaxCores.svh`. In MaxCores, the allowed-to-modify
list is:

- `VectorParallelism`
- `LutParallelism`
- `CoreInterconnectNumStages`
- `BatchSize` — push this as high as possible. Target 20+.

Don't touch other parameters. Don't introduce new configs.

## Build invocation

The build runs on remote host `eq2`. Use exactly this pattern (the double-ssh
is intentional — `pkill -u esifferm` kills your previous session, and the
second ssh starts a fresh one):

```bash
ssh eq2
pkill -u esifferm
ssh eq2
bash
cd /soe/esifferm/GitHub/ternip_claude/ternary_matmul
make pynqvivado_au250_hw CONFIG=xcu250_D=1024_OneCore &> build.log &
disown
```

Then close the ssh session. Use `scripts/run_build.sh` to do this idempotently.

## Polling

`scripts/poll_build.sh` greps `build.log` for the markers:
- `Run vpl: Step impl: Completed` — success path
- `vpl: Failed` or `Segmentation fault` — error
- `Total elapsed time:` — bottom of run, look at last few lines for status

Cadence: 5 minutes early in the build (synth/opt), 10–15 minutes once it's
in route_design (the long phase). Use the `loop` skill or `ScheduleWakeup`
with delays of 600–900 s. Don't poll faster than 60 s — wastes the prompt
cache TTL.

## Error handling

- **Vivado segfault during route_design**: rerun the build, no RTL change.
  Add a `Build N: rerun after Vivado segfault` line to the release notes.
- **XRT `libxrt_core.so: undefined symbol: xclProbe` at end of build**:
  Cosmetic, after the bitstream is generated. Timing reports are still
  readable. Don't rerun; just collect artifacts.
- **`AUTO-FREQ-SCALING-04` warning**: timing didn't close at 300 MHz; Vivado
  scaled to a lower clock. Not a build failure — collect artifacts and
  read the new frequency from the warning.
- **Sim/lint failures after RTL edit**: ALWAYS your bug. Fix before any
  Vivado build — Vivado is way too slow to debug functional issues.

## Verification (run after every RTL change, BEFORE committing)

```bash
cd ternary_matmul
make lint    CONFIG=xcu250_D=1024_OneCore
make sim TOP=tmatmul_tb SIMULATOR=verilator CONFIG=xcu250_D=1024_OneCore
make sim TOP=tmatmul_tb SIMULATOR=vcs       CONFIG=xcu250_D=1024_OneCore
make sim TOP=rms_tb     SIMULATOR=verilator CONFIG=xcu250_D=1024_OneCore
make sim TOP=rms_tb     SIMULATOR=vcs       CONFIG=xcu250_D=1024_OneCore
```

OneCore config is plenty for functional verification — MaxCores adds nothing
besides build time. Tmatmul and rms cover the failing-path hotspots.

## Yosys source code is available

If you ever need to understand yosys's behavior — how `synth_xilinx` maps
something, whether a pass honors a particular attribute, what xc7 vs xcup
differences are — the full yosys source tree is at `references/yosys/`.
Two starting points:

- `techlibs/xilinx/synth_xilinx.cc` — the top-level Xilinx synth flow
- `passes/opt/` — the opt_dff / opt_share / opt_clean passes that
  determine what survives flattening

This is faster and more reliable than guessing about yosys behavior — read
the source when in doubt.

## Yosys for prototyping (`/yosys-fanout`)

Yosys synth_xilinx -family xc7 finishes in ~1–3 minutes vs ~4 hours for
Vivado. Use it to check **structure**, not timing:
- Is fanout actually reduced after a lane split / replication?
- Did a register get inserted between two FFs (e.g. convert skid)?
- Is `KEEP_HIERARCHY` honored?

Yosys cannot tell you net delay. Vivado is the only authority for "did
timing close." Yosys is for fast iteration on RTL changes before the
multi-hour Vivado commit.

Caveat: yosys's xc7 DSP inference is incomplete (it produces ~10–20 DSP48E1
cells vs Vivado's hundreds). For DSP-related changes, the structural check
("is there a register between source and sink?") is still valid because
yosys infers a multiplier either way — just as LUTs+CARRY4 instead of DSP.

## Generating timing reports (`/vivado-read-reports`)

```bash
vivado -mode batch -nojournal -nolog \
    -source .claude/skills/vivado-read-reports/scripts/generate_timing_csv.tcl \
    -tclargs ternary_matmul/synth/pynqvivado_au250/build/xcu250_D=1024_OneCore/hw/_x/link/vivado/vpl/prj/prj.xpr \
    hard_N.csv \
    level0_i/level1/level1_i/ulp/ternip_ip_1
```

Outputs WNS / TNS / failing-path-count to stdout (grep `VIVADO_READ_REPORTS_*`)
and a per-endpoint CSV. The `level0_i/.../ternip_ip_1` filter scopes to the
kernel — without it, you'll get thousands of paths through XRT/platform
infrastructure that you can't fix.

## What to try next (priority-ordered, when picking the next iteration)

This list reflects lessons from this session — don't redo things in the
"don't bother" column.

### Things that have been done and worked (don't redo)
- `rms.sv` 1-stage skid before MOA (data path)
- `DECOUPLED_READY=1` on importvector's pmem
- Disable floorplanning (`pre_place_design.tcl.disabled` rename)
- `ternip_fixed_point_convert` proper ready/valid + always-pipelined (1-cycle internal skid)
- `ternip_mul`/`sqrt`/`div` plumbed to handshake with the new convert
- Pipeline reg between PISO and SIPO inside `ternip_sig_parallelized` and
  `ternip_csig_parallelized`
- `importvector` pmem `NumLanes=8` (moderate lane split: 8 instances of
  `ternip_pipelined_mem_data_lane`, FO~512 per CE wire)

### Things that have been done but were net-negative (don't redo)
- `NumLanes=64` on importvector (placement chaos — too many small instances)
- `NumLanes=16` on vector_registers / exportvector (caused regressions —
  these widths are already small enough to not have a wide-CE problem)
- Lane-splitting `go_ddr_data_q` per bank into 8 lanes (didn't help once
  the importvector lanes were tuned; added complexity)
- 2-deep skid before MOA in rms (over-fetches; broke tmatmul_tb timing
  semantics)
- Asynchronous reset experiments (Xilinx UG949 §4.2 strongly recommends
  sync, especially for DSP48 / BRAM)

### Things to try (open ideas, prioritized)

1. **MOA → importvector backpressure path** — when the previous bottleneck
   (rms_op_q → DSP.B) is fixed, the next dominant cluster is
   `multioperand_accumulator/out_valid_q_reg → importvector buffer's wide
   tdata register CE`. The MOA's `out_valid_q` is in one part of the SLR
   layout, the importvector buffer is in another. Options: pipeline the
   `accumulator_q2_ready` signal, or revisit the importvector buffer's
   CE source structurally.
2. **Per-bank importvector or full importvector → MOA skid stages**.
   Cross-SLR data movement at 300 MHz needs explicit pipelining;
   `ternip_pipelined_interconnect` exists for this purpose (already used
   inside `pipelined_mem`'s `DECOUPLED_READY` path). Insert another
   instance where MOA output / importvector input cross SLR boundaries.
3. **Inspect `ternip_core`'s instruction-fetch / address-staging FSM**.
   `latched_tmatmul_addrs_d` has a wide demux that puts an alumacc cone
   at FO~410. Not a hot path during the matmul itself but counts toward
   WNS during instruction dispatch.
4. **AXI Lite control interconnect (`axi_interconnect_ctrl`)** — sometimes
   becomes the WNS source after kernel-side bottlenecks clear. This is
   Xilinx auto-generated and hard to fix structurally; mostly placement-
   dependent. If it dominates, try fewer kernel-side changes per iteration
   to give Vivado breathing room.
5. **Disable `--trace_memory` entirely**. We currently emit per-SLR
   trace_memory in `synth/pynqvivado_common/generate_kernel_cfg.tcl`
   (lines 58-72) to silence the Vitis "did not specify more than one SLR
   for tracing" warning. Trace memory adds AIM monitors + per-SLR trace
   buffers that consume routing resources but only matter for runtime
   profiling (`xrt-smi`). For pure timing closure they're dead weight.
   Try removing the `[profile]` block from `kernel.cfg` generation
   entirely (or setting `data=none:none:none` + dropping the `trace_memory`
   lines) and see how much WNS moves. Easy to revert, easy to measure.
6. **Once OneCore closes, tune MaxCores** — switch to
   `xcu250_D=1024_MaxCores`, push `BatchSize` up (target 20+), and re-
   tune `VectorParallelism`/`LutParallelism`/`CoreInterconnectNumStages`
   for the larger floorplan. Don't go to MaxCores before OneCore is
   close to closing; debugging at MaxCores is much harder.

## Style and code rules

See [STYLE.md](STYLE.md) for the full list. The non-negotiables:

- **Every module has ready/valid**, unless it's an intentional skid buffer
  or it's purely combinational. Turning a combinational module into a
  sequential one means **adding ready/valid on both ends**.
- **Every FF has `_d` and `_q` named signals**. Per
  [lowRISC VerilogCodingStyle](references/lowRISC-style-guide/VerilogCodingStyle.md),
  signal-suffix order: `_n` (active-low) → `_d`/`_q` → `_i`/`_o`/`_io`.
- **Self-documenting names**. Verbose, descriptive, no jargon. If you can't
  pick a good name, the function/variable probably shouldn't exist (or
  should be split, or merged with something else).
- **Minimize new modules**. Order of preference for new logic:
  1. Instantiate an existing module
  2. Inline the pattern
  3. Add a new module — **LAST RESORT**, and check
     `ternary_matmul/third_party/` first (you'll usually find what you
     need there).
- **No `Pipelined`-style parameters without ready/valid plumbing**.
  Sticking a flop into a combinational path is a hack unless backpressure
  goes through it. The `ternip_fixed_point_convert` refactor in this
  branch is the canonical example of how to do it right.
- **Don't trust `MAX_FANOUT`** — it's been observed to give modest
  improvements at best on this design. Prefer structural fixes (module-
  instance lane splits, additional pipeline stages, etc.) over attribute
  hints.
- **Never use `(* dont_touch *)`**. The project owner can guarantee this
  attribute won't help timing — it just blocks Vivado's optimizer from
  doing useful work. If you find yourself wanting it, the underlying
  structural problem is wrong and `dont_touch` won't save you.
- **Tool-specific attributes (`KEEP_HIERARCHY`, `srl_style`, etc.) should
  live in TCL/XDC, not RTL.** Acceptable to put them inline during
  exploration / prototyping, but anything that ships back to the main
  RTL fork should be moved to `synth/vivado_common/pre_synth_design.tcl`
  (or the appropriate stage script). Avoid baking vendor-specific hints
  into the RTL surface.
- **Don't randomly remove resets** — UG949's "only reset what needs it"
  means avoiding **FF reset PINs** for data-path flops. Setting things to
  0 from another FF's output is fine.
- **`ternip_skid_lane.sv` exists but is considered a poor abstraction —
  don't use it in new code**. The convert refactor uses inline ready/valid
  skid patterns; do the same.

## Pipelining trade-off (read before adding pipeline stages)

There is no free pipeline stage. The trade-off:

- **More FFs → more routing congestion.** Every new pipeline register
  adds nets that the router has to handle. On a congested-already build,
  adding pipeline stages can push WNS *worse* by stressing route_design.
- **More FFs → more placement flexibility.** Vivado can move FFs around
  much more freely than combinational logic. Strategic FFs let the
  placer relax tight clusters; adding one at the right boundary can
  unlock a 1+ ns gain.
- **SLR crossings always need an FF.** A combinational signal that
  physically crosses an SLR at 300 MHz never makes timing — the LAGUNA
  routing alone is ~1.5–3 ns. Pipeline registers at SLR boundaries are
  non-negotiable.

So: don't pipeline indiscriminately. Pipeline where the timing report
*shows you* a long net (especially cross-SLR), not "everywhere it might
help". When in doubt, do the smaller experiment first — yosys-fanout
tells you the structural before-and-after, and a single Vivado iteration
tells you whether the trade-off paid off.

## Cross-SLR considerations

The AU250 is a 4-SLR FPGA. DDR banks are pinned to specific SLRs by the
xilinx_u250_gen3x16_xdma platform:
- DDR[0] → SLR0
- DDR[1] → SLR1
- DDR[2] → SLR2
- DDR[3] → SLR3

The kernel logic (one ternip core) is typically placed in SLR0 or SLR2.
Per-bank DDR data and address streams must cross SLRs to reach the core.
Cross-SLR routing through an LAGUNA register tile is ~1.5–3 ns of pure
wire delay — that's 50–90% of your 3.33 ns clock period.

Practical implications:
- **Any logical signal that physically crosses an SLR must be on a
  pipelined register-to-register hop**. Combinational logic + cross-SLR
  routing in the same cycle never makes timing at 300 MHz.
- The `--trace_memory` config in `kernel.cfg` distributes trace memory
  one-per-SLR (see `synth/pynqvivado_common/generate_kernel_cfg.tcl`); the
  ALL-IN-ONE-SLR default impacts timing.
- The kernel package gets pulled onto SLR2 by the platform's static SLR
  budget; if SLR2 is too congested, place_design may push parts to SLR3
  through LAGUNA crossings.

## High-fanout reset

The reset (`rst_ni`) network is a wide-fanout 1-bit signal. UG949 §4.5
documents the `DIRECT_RESET` attribute for fine-grained control. In this
design:
- `rst_ni` is sync, active-low, comes from XRT through the AXI control
  interconnect into `axi_ternip_rst`.
- `axi_ternip_rst/rst_nq_reg` is the first-stage local register. It has
  `MAX_FANOUT=10` to force replication near consumers.
- Each big sub-module (`ternip_tmatmul`, `ternip_rms`, etc.) re-registers
  rst_ni locally (`rst_ni_q`). This is pattern #3 from UG949 — local
  re-registration per region — and it beats both raw fabric routing and
  BUFG distribution for this design.

If the reset network shows up as a WNS source in a future build, the fix
is more local re-registration FFs in the failing hierarchy, not BUFG and
not `MAX_FANOUT` hints.

## Vivado docs available at `references/vivado-docs-2023.1/`

| Doc | Use it for |
|---|---|
| **UG901 Synthesis** | Synthesis attributes: `KEEP`, `KEEP_HIERARCHY`, `DONT_TOUCH`, `MAX_FANOUT`, `RAM_STYLE`, `SRL_STYLE`. DSP attribute reference. |
| **UG903 Using Constraints** | XDC syntax for `set_max_delay -datapath_only` (multicycle paths), `set_false_path`, clock groups. Useful when you need to tell the tools that a path is intentionally loose. |
| **UG906 Design Analysis and Closure Techniques** | The methodology behind the GUI Timing Summary panel that `/vivado-read-reports` reproduces. Useful for interpreting `From` / `To` / `Net Delay` / `Logic Delay` columns. |
| **UG912 Properties Reference** | Object-property names like `MAX_FANOUT`, `CLOCK_UNCERTAINTY`, `LOGIC_LEVELS`, `DATAPATH_NET_DELAY`. The `vivado-read-reports` Tcl uses these. |
| **UG949 UltraFast Design Methodology** | Reset best practices (sync vs async, when to reset, `DIRECT_RESET`), high-fanout strategies, congestion analysis. Repeatedly used in this project's reset hygiene work. |
| **UG1393 Vitis Application Acceleration Development** | `kernel.cfg` syntax: `sp=` (slave port mapping), `trace_memory`, `prop=run.X.STEPS.Y.Z`. Required for any change to `synth/pynqvivado_common/generate_kernel_cfg.tcl`. |

## How to write release notes (per iteration)

Title: `hard_N: <one-line change summary>`

Body template:
```
## Change
<2-3 sentences. What was modified, where, and why.>

## Submodule commits
- ternip: <link to ternip commit>
- ternary_matmul: <link to ternary_matmul commit>

## Results
- WNS: <ns>
- TNS: <ns>
- Failing endpoints: <count>
- Frequency: <MHz> (post AUTO-FREQ-SCALING if any)

## Top failing cluster
<1-3 paragraphs describing what's failing now and a hypothesis for the
next iteration>

## Build notes
<segfault? cosmetic XRT error? warnings? anything you noted>
```

Attach:
- `hard_N.csv` (the timing CSV)
- `build.tar.gz` (the tarred build dir — see `scripts/collect_artifacts.sh`)

## Operational hygiene

- **One change per build**. Bundling two ideas into one iteration makes the
  result uninterpretable. If you have two ideas, do them sequentially.
- **Don't poll too fast**. Vivado builds take hours. A 5-minute poll
  cadence is plenty.
- **Don't kill in-flight Vivado runs** unless they're clearly stuck or
  you have a bugfix that wouldn't be in this build anyway.
- **If the same path keeps failing across 3+ iterations**, you're attacking
  the wrong layer. Step back and look at the design hierarchy / placement,
  not the RTL.
- **Read the lowRISC style guide before any significant refactor**:
  [references/lowRISC-style-guide/VerilogCodingStyle.md](references/lowRISC-style-guide/VerilogCodingStyle.md).
