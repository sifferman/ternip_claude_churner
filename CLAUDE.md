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

## The loop (one iteration = one `YYYY.MM.DD-HHMM` build)

Each iteration's release / tag is named after the wall-clock when the
Vivado build kicks, in the form `YYYY.MM.DD-HHMM` (24-hour, local
PDT, no colon — git tag names can't contain `:`). Take the time from
`build.log`'s first `[HH:MM:SS] Run vpl: Step create_project:
Started` marker. The release **title** can use the friendlier
`YYYY.MM.DD-HH:MM` (with colon) form since titles aren't tag names.

```bash
date '+%Y.%m.%d-%H%M'       # tag form, e.g. 2026.05.25-1846
date '+%Y.%m.%d-%H:%M'      # title prefix, e.g. 2026.05.25-18:46
```

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
- `NumVectorRegisters` — BRAM-backed (see
  `third_party/ternip/rtl/common/ternip_pipelined_mem_data_lane.sv:45`);
  scale up if BRAM utilization is low.

Don't touch other parameters. Don't introduce new configs.

## The actual optimization target: tokens/second

**The optimization target is `tokens/second`, NOT WNS, NOT
utilization, NOT BatchSize, NOT MHz.** Those are intermediate
variables. The end goal is: maximize `tokens/second = clk_freq *
BatchSize / cycle_counter` (per `ternary_matmul/sw_utils/target/
report_instruction_timing.py`).

Constraints derived from this:
- **Must run at 300 MHz**. The board is unreliable at other
  frequencies (see "Target frequency" section). So WNS must close
  (with `[advanced] skipTimingCheckAndFrequencyScaling=1`, the
  xclbin will be packaged at 300 MHz regardless, but the design
  must actually meet timing for the bitstream to be valid on
  silicon).
- **BatchSize is the multiplier**. Higher BatchSize → linearly more
  tokens/sec (until area fills).
- **`cycle_counter` (per-token cycle count) depends on
  `VectorParallelism` / `LutParallelism`**. Lower VP/LP → more
  cycles per token. Per `report_instruction_timing.py`, halving VP
  ~doubles `cycle_counter` for non_matmul ops.

Rule of thumb for picking what to change between iterations:
1. Run `report_instruction_timing.py <config> <model>` to estimate
   the new tokens/sec.
2. If the change INCREASES estimated tokens/sec → try it.
3. If the change DECREASES estimated tokens/sec → only try it if
   it makes a stuck timing-closure problem tractable. Otherwise
   skip.
4. **Increasing one parameter at the cost of decreasing BatchSize
   is almost always a net LOSS** unless the saved area unlocks a
   much larger BatchSize next iteration.

The user said it directly:
> the actual goal is to maximize tokens/second. So if increasing
> one parameter causes BatchSize to decrease, which causes
> tokens/second to decrease, then ignore that change.

## Utilization tracking (mandatory for every MaxCores build)

Every MaxCores build's release notes MUST include a
utilization breakdown via the `vivado-utilization` skill:

```bash
python3 .claude/skills/vivado-utilization/scripts/parse_kernel_util.py \
    ternary_matmul/synth/pynqvivado_au250/build/xcu250_D=1024_MaxCores
```

Include both the three-way table (Platform / Kernel / Free) and
the scaling-multiplier output. This goes in the release body as a
section titled `## Utilization` after the timing results.

**Balanced utilization heuristic**: if one resource is high (e.g.
LUT 90%) and another is low (e.g. BRAM 10%), the next iteration
should shift parameters to use the LOW resource — but only if
doing so doesn't reduce `tokens/sec`:

- Low BRAM → consider `NumVectorRegisters++` (uses BRAM, frees
  swap_instructions → cycle_counter decreases → tokens/sec UP).
- Low DSP → consider `VectorParallelism++` (uses DSPs in the
  multiplier lanes, cycle_counter decreases → tokens/sec UP).
- Low FF → consider re-adding pipeline stages (helps timing closure
  without reducing tokens/sec).
- Low LUT → harder; LUT is often the binding constraint.

If a parameter change improves balance BUT forces BatchSize
DOWN, compute the new `tokens/sec` from `report_instruction_timing.py`.
If it's lower, SKIP the change.

The whole loop reduces to: **find the (config, BatchSize) tuple
that maximizes `BatchSize × clk_freq / cycle_counter` subject to
"the bitstream closes at 300 MHz"**.

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
    YYYY.MM.DD-HHMM.csv \
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
- **`opt_design CONGESTION REPLACE`** (2026.05.26-22:14): drop
  `-directive Explore` and add MORE_OPTIONS
  `-hier_fanout_limit 512 -muxf_remap -propconst -retarget -sweep`
  at `opt_design`. WNS -0.848 → -0.284 (one-shot win, 564 paths
  cleaned).
- **`place_design AltSpreadLogic_high`** (2026.05.27-03:15):
  the breakthrough. WNS -0.284 → -0.190, TNS -50.121 → -0.607,
  failing 558 → 8. The spread-logic-aggressive directive cracked
  the marginal-path congestion pattern that build_14's
  CONGESTION REPLACE left behind.
- **`post_route_phys_opt -sll_reg_hold_fix` REPLACE pattern**
  (2026.05.26-22:14): drop the directive and put `-sll_reg_hold_fix`
  alone. Per UG904 4-167, directive XOR MORE_OPTIONS — REPLACE
  the directive, never ADD alongside.
- **`[advanced] param=compiler.skipTimingCheckAndFrequencyScaling=1`**
  in `kernel.cfg` (2026.05.27-10:27, UG1702 line 10249).
  Disables AUTO-FREQ-SCALING-04. xclbin packaged at the requested
  300 MHz regardless of WNS; per-step `report_timing_summary`
  data is unaffected, so our CSV still reports the design's
  true WNS. The `--xp param:…` CLI form was DEAD in Vitis 2023.1
  (silently ignored).

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
- **Aggressive TCL `force_replication_on_nets` for buffer tready**
  (2026.05.25-2137). Added a rule targeting
  `*pipelined_mem*decoupled_ready*buffer*lanes*tready*` with
  `FLAT_PIN_COUNT > 30` to replicate the wide-CE source FFs from
  2026.05.24-0501's 33-endpoint cluster. Over-replicated — the
  placer rearranged to fit the extra replicas, pushing
  `csig_parallelized`'s PISO→csig→csig_out_q path to a much worse
  layout. Net: WNS -0.259 → -0.719, TNS -3.873 → -151.4, failing
  endpoints 33 → 1431, achieved 246.9 → 244.2 MHz. **Lesson**: TCL
  `force_replication_on_nets` thresholds matter — the existing
  pipelined_mem rule's `FLAT_PIN_COUNT > 100` is the floor for this
  design; going lower destabilizes placement. Don't reapply.
- **Per-lane `stall1` in `ternip_pipelined_mem`** (2026.05.24-0827). Goal was to
  break the FO=4166 `axis_tready → wdata_q1.CE` cluster by giving each
  `data_lanes[i]` its own stall expression sourced from one lane's
  per-lane tready. The cluster did vanish — but the placer's re-layout
  exposed `tmatmul_operation_q[1]` → its synth-replicas at FO=322 with
  7 LUT levels (slack -0.308) and `tmatmul_operation_q[1]` →
  `latched_tmatmul_addrs_q.CE` at 5 LUT levels (slack -0.19). Net
  result: WNS -0.259 → -0.308, TNS 4× worse, frequency 242.1 → 216.4
  MHz. Don't re-apply this exact RTL change. The TCL replication in
  `pre_phys_opt_design.tcl` (already targets `*stall1*`, `*read_valid_q*`,
  `*pipelined_mem*`) is doing what this change attempted, and it's
  better behaved for the placer.
- **`phys_opt_design -directive AddRetime`** (2026.05.27-05:42).
  Goal: retiming after placement (with real delay info) to
  re-balance the 13-14 LUT cones in tmatmul_dma's FSM transitions.
  WNS -0.190 → -0.377, TNS -0.607 → -4.047, failing 8 → 99.
  **AddRetime replaced AggressiveExplore's entire pass**, losing
  the unrelated opts that the design depended on.
- **`phys_opt_design -directive AlternateReplication`**
  (2026.05.27-12:49). Goal: replicate source FFs at lower fanouts
  (the 8 failing paths have FO 17-21, below AggressiveExplore's
  default threshold). **IDENTICAL numbers to AddRetime** —
  WNS -0.377, TNS -4.047, failing 99 — confirming that BOTH
  directives bypass an AggressiveExplore-specific opt that this
  design depends on. **AggressiveExplore is the only valid
  phys_opt_design directive for this design.** Don't try other
  variants without first establishing what AggressiveExplore is
  uniquely doing.
- **`route_design -directive AggressiveExplore`** (2026.05.27-15:10).
  Hypothesis: extra global iterations + tighter convergence
  could crack the 8 depth-bound paths (net delay > logic delay
  on cluster B). **NEUTRAL** on WNS/TNS/failing — bit-identical
  to Explore (-0.190 / -0.607 / 8) — but added +27 min build
  time. Routing wasn't the bottleneck; depth is. Don't reapply
  unless we have specific evidence of sub-optimal routing.

### Things to try (open ideas, prioritized)

**See [TO-TRY.md](TO-TRY.md)** for the living list. It has two sections:
"User-Generated" (priorities the user adds) and "Claude-Generated"
(ideas Claude surfaces from timing-report analysis or from this section's
history). When picking the next iteration's change, **drain
User-Generated first, then top of Claude-Generated.** Move tried ideas
from TO-TRY.md to the "have-been-done" lists above as you go.

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

Title: `YYYY.MM.DD-HH:MM: <one-line change summary>` (title can use
the colon since it's not a git ref; the tag itself is
`YYYY.MM.DD-HHMM`).

Body conventions:
- **Free-text wall-clock**: `2026-05-25 6:46 PM PDT` (year-included,
  AM/PM, never 24-hour).
- **Table column headers**: `2026-05-24 5:01 AM (previous)` /
  `2026-05-25 6:46 PM (current)` — readable date + role label,
  never the bare tag code.
- **Cross-reference body text**: the tag form
  `2026.05.24-05:01` is fine inline (matches the GitHub URL), or
  the readable date — pick whichever flows better.

Body template:
```
## Build status
**Build kicked off 2026-MM-DD H:MM AM/PM PDT**, expected completion
~H:MM AM/PM PDT (reference 2h 52m). Flips to "finished, generating
report" the instant the build hits a terminal state.

## Change
<2-3 sentences. What was modified, where, and why.>

## Submodule commits
- ternip: <link to ternip commit>
- ternary_matmul: <link to ternary_matmul commit>

## Pre-build verification
<table of lint + sims results>

## Results
| Metric | 2026-MM-DD H:MM AM/PM (previous) | 2026-MM-DD H:MM AM/PM (current) | Δ |
|---|---:|---:|---:|
| **WNS** (kernel scope, ns) | ... |
| **TNS** (kernel scope, ns) | ... |
| **Failing endpoints** | ... |
| **Achieved frequency** (MHz) | ... |
| Build time | ... |
| Config | ... | ... | — |

## Top failing cluster
<1-3 paragraphs describing what's failing now and a hypothesis for the
next iteration>

## Build notes
<segfault? cosmetic XRT error? AUTO-FREQ-SCALING value?
anything you noted>
```

Attach:
- `YYYY.MM.DD-HHMM.csv` (the timing CSV)
- `build.tar.gz` (the tarred build dir — see `scripts/collect_artifacts.sh`)
- `build.log` (snapshot before kicking the next build)

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

## Always keep eq2 building — NEVER GIVE UP

**This is the most important rule. eq2 sitting idle is a bug.** The
user runs this harness for days unattended; every idle hour is a
failed iteration that cost nothing to start.

1. **At the start of every turn, check whether a build is running on
   eq2.** If not, find out why and fix it before doing anything else
   (yes, before responding to the user, before writing memory, before
   anything). The very first command in such a turn should kick a
   build.
2. **Chain builds with near-zero gap.** When the monitor fires
   `BUILD SUCCESS` / `BUILD FAILED`, the sequence is:
   1. **First (~1-2 min)**: generate the timing CSV via local vivado
      batch (`.claude/skills/vivado-read-reports/scripts/generate_timing_csv.tcl`)
      against the just-finished `prj.xpr`. **This MUST happen before
      the kick** — the next iteration's v++ wipes the xpr in ~30s and
      the per-endpoint timing data becomes unrecoverable. Lost on
      2026.05.25-18:46; don't repeat.
   2. **Second (instant)**: snapshot `build.log` to
      `artifacts/<datecode>/build.log` so the kick doesn't truncate it.
   3. **Third (instant)**: `bash scripts/run_build.sh` to kick the
      next iteration. eq2 is busy again.
   4. **Fourth (1 min, before tar)**: if this was a MaxCores build,
      run the vivado-utilization skill against the build dir and
      capture the output. The xpr is being wiped but
      `kernel_util_routed.rpt` lives in `impl_1/` which is preserved
      until v++'s new build phase touches it (a few minutes after
      kick). Snapshot it explicitly to
      `artifacts/<datecode>/kernel_util.txt` for the release body.
      `<datecode>/build.tar.gz` artifact. Runs alongside the new
      build's sv2v phase; harmless to both.
   5. **Fifth (parallel)**: edit the just-finished build's release
      body with the results, upload assets. Start the new build's
      monitor + draft its release body.

   The 1-2 min CSV step costs <1% of the iteration's 3 hours and
   guarantees we keep every iteration's timing data.

   When chaining, also stop the previous build's monitor before
   starting a new one — the old monitor doesn't know the log got
   truncated and will silently re-attach to the new build.
3. **Pre-stage the next iteration while the current build runs.**
   During the 3-hour Vivado window, pick the next change from
   `TO-TRY.md` (User-Generated first, then Claude-Generated), edit the
   RTL, run sims/lint, and commit/push so eq2 has the code. The kick
   is then a 5-second action when the current build ends.
4. **There is always something to try.** If TO-TRY.md is empty,
   re-read the latest CSV for new clusters, scan `references/` for
   relevant techniques, or revisit a previously net-negative idea
   with a different angle. Even when WNS is closed at 300 MHz, there
   is `BatchSize` to tune, `--trace_memory`-style cleanups, and
   MaxCores work. **Never decide "there's nothing useful to do."**
5. **Regressions are not a reason to stop.** A net-negative iteration
   means you learned something — log it in CLAUDE.md's
   "net-negative" list, revert if needed, AND IN THE SAME COMMIT BLOCK
   bundle a fresh change so the next build moves forward. A
   pure-revert build is a wasted 3 hours.
6. **The smallest-blast-radius candidate from TO-TRY.md is always a
   defensible choice.** When you're not sure what to try next, pick
   the lowest-risk entry and ship. Add a QUESTIONS.md note explaining
   what you picked and why; the user reads it asynchronously and
   redirects mid-flight if they want something else.

**Self-check at the end of every turn:** did this turn result in
(a) eq2 building a new iteration, (b) RTL/config staged for the
next iteration that will kick the moment current build finishes,
or (c) a release update for a build that just completed? If
none, something is wrong — reopen the loop and find what to ship.

## Don't pause to ask questions

This loop is meant to run unattended. **Do not invoke `AskUserQuestion`
during iteration.** Use your best judgement for routine decisions (which
fix to try next, how big a refactor to take on, whether to bundle vs
split, etc.).

When you hit something genuinely uncertain or worth a human decision:

1. Pick a defensible default and proceed.
2. Append a short note to `QUESTIONS.md` at the repo root describing
   the choice point, what you decided and why, and what you would ask
   the user if you could. They review it out of band — your job is to
   keep moving.

Reserve `AskUserQuestion` for hard blockers where you genuinely cannot
proceed (e.g. credentials missing, the build host is unreachable).

## Release body must reflect build status in real time

Every release should be useful to a human who checks GitHub directly,
**not just to you when you finish the iteration**. So:

- When you create the release at build-kick time, include a "Build
  status" section near the top with the **estimated completion
  time** (use `scripts/eta.sh` for a wallclock figure). Refresh this
  on each significant ETA emission if drift moves more than ~10 min.
- The instant the build hits a terminal status (success / failure),
  the **first action** is `gh release edit` flipping that section to
  something like:

  > Build finished, generating report. Full WNS / TNS /
  > failing-endpoint data + CSV + tarball coming in a follow-up edit.

  Only then do you generate the CSV, tar the build, and produce the
  final release-body edit with the results.

A stale "_TBD after build._" body when the build has actually
finished is worse than no release at all — it implies the
iteration is still running when it isn't.
