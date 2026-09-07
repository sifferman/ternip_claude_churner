# CLAUDE.md — ternip_claude autonomous loop

A self-driving FPGA-timing harness for `ternary_matmul` on an Alveo U250.
Iterate on the design, build it, record the result, try again — for days,
unsupervised.

## Repo layout

- `ternary_matmul/` — the build harness (Makefile, targets, dv/, synth/).
  Private fork `sifferman/ternary_matmul_claude`.
- `ternary_matmul/third_party/ternip` — nested submodule, the RTL. Private fork
  `sifferman/ternip_claude`. There is no top-level `ternip/`.
- `plans/` — per-variant and per-refactor planning docs.
- `references/` — read-only sources of truth (style guide, Vivado docs, yosys).
- `scripts/` — build / poll / collect helpers.
- `artifacts/<datecode>/` — per-iteration reports. Not in git.

## The target is tokens/second

`tokens/second = clock × lanes / cycle_counter`, where **lanes is the sum of
per-CU BatchSize**. Not WNS, not utilization, not MHz — those are intermediate.

Count lanes per CU. `report_timing` once multiplied
`NumSeparateAxiInstances × BatchSize` off a single `Config`, which silently
ignored every kernel but the first and understated heterogeneous targets by
~17%. `benchmark_pynqvivado` counts correctly; trust it over any projection.

Before committing to a build, run `report_instruction_timing` and only proceed
if projected tok/s goes **up**. Raising one parameter at the cost of BatchSize
is almost always a net loss.

## Hard rules

- **300 MHz only.** The board is unreliable elsewhere. `AUTO-FREQ-SCALING-04`
  firing means the iteration did not close, even if the build "succeeded".
- **Build on eq2 only.** eq1 is reserved for other users. One build at a time.
- **Never do out-of-context builds.** `make vivado` / vivado_generic cannot run
  on hardware and does not model shell or DDR congestion. Go straight to
  `make pynqvivado_au250_hw`.
- **`NumSeparateAxiInstances` is always 4.** Never ship nk=2/3.
- **Never rerun a passing build on identical RTL+config.** Vivado is
  deterministic; it burns hours for zero information.
- **One change per build.** Two ideas bundled makes the result uninterpretable.
- **Stage artifacts BEFORE kicking the next build.** v++ wipes `build/` within
  seconds. Order: timing CSV (needs `prj.xpr`), then `build.log`, then
  `kernel_util_routed.rpt`, then the tar, then kick.
- **`rm -rf build/<CONFIG>` when only TCL changed.** Make does not track TCL
  hooks; a stale `kernel.xclbin` makes it skip synthesis silently.
- **Delete `loaded.xclbin` before any silicon run.** A stale one programs the
  wrong bitstream and fakes a hardware failure.
- **Validate on the real card** (godbolt, BDF 0000:64:00.1). Pin pynq to the
  U250; an unpinned `Overlay` grabs the wrong device and hangs.
- **Never leave eq2 idle, and never ask permission to kick.** The smallest
  blast-radius candidate is always defensible. Just start it.

## The binding constraint is CLB occupancy

Not LUT%. Target ~65% slice occupancy per SLR; higher is unroutable. Low LUT%
is a symptom of spreading, not free room.

Consequences, all measured:

- **Adding flip-flops hurts.** `CoreInterconnectNumStages=3` is tok/s-neutral so
  it looks free, but it made TNS 9× worse and failing endpoints 5×.
- **`VectorParallelism=8` does not fit** — 13584 CLBs needed against 10439
  available, despite DSP sitting at 20% of budget. VP consumes CLBs.
- **Fix timing by removing logic, never by compacting** — compacting raises
  occupancy.
- **Pblocks are not ours.** XRT is a DFX flow; the regions come from the
  platform and cannot be resized or softened.

## Verification — every gate, before every build

```bash
cd ternary_matmul
make lint CONFIG=xcu250_D=1024_OneCore
make sim TOP=tmatmul_tb SIMULATOR=verilator CONFIG=xcu250_D=1024_OneCore
make sim TOP=tmatmul_tb SIMULATOR=vcs       CONFIG=xcu250_D=1024_OneCore
make sim TOP=rms_tb     SIMULATOR=verilator CONFIG=xcu250_D=1024_OneCore
make sim TOP=rms_tb     SIMULATOR=vcs       CONFIG=xcu250_D=1024_OneCore
( cd dv/cocotb/axi_ternip_batched && CCACHE_DISABLE=1 make SIM=verilator )
make yosys_check_pipelined      # if any pipelined-interconnect RTL changed
```

**Cocotb is non-negotiable.** The in-tree SV testbenches stub out the
top-level AXI ports, so only cocotb exercises `m_axi_*` / `s_axi_*` /
`s_axis_*`. Set `CCACHE_DISABLE=1` — a cc1plus segfault is ccache+PCH, never
your RTL.

**Also lint the sv2v output** as Verilog-2005. The gates read SystemVerilog
directly and miss sv2v→Vivado breakage; literal-width casts (`8'(...)`) are the
landmine.

A sim or lint failure after an RTL edit is always your bug. Fix it before
Vivado, which is far too slow to debug function.

## Build and poll

```bash
ssh eq2
cd /soe/esifferm/GitHub/ternip_claude/ternary_matmul
make pynqvivado_au250_hw TARGET=synth/pynqvivado_au250/targets/<target>.json &> build.log &
disown
```

5–7 hours typical. Poll every 10–15 minutes, never faster than 60 s. Better:
block on the terminal marker instead of polling.

```bash
until ssh eq2 'grep -qE "Step impl: Completed|vpl: Failed|Total elapsed time" .../build.log'; do sleep 300; done
```

Read the final numbers from
`impl_1/hw_bb_locked_timing_summary_postroute_physopted.rpt`, and the per-kernel
violation breakdown from its `Slack (VIOLATED)` blocks — which kernel owns the
violations is usually the whole story.

**Placement WNS estimates do not predict the outcome.** One build read −0.631 in
placement and closed at +0.011; another read −0.016 and failed. Only the routed
summary counts. And `post_route_phys_opt` has recovered nothing on four
consecutive failing builds — do not count on it for margin.

## Error handling

- **Vivado segfault in route_design** — rerun unchanged, note it.
- **XRT `undefined symbol: xclProbe` at the end** — cosmetic, after the
  bitstream. Collect artifacts.
- **`VPL 18-1000 partially-conflicted nets`** — if you see it twice on the same
  RTL it is not a flake; Vivado is deterministic. Bisect your last change.
- **`VPL 30-487` CLBs required > available** — a capacity failure, not timing.
  The design does not fit; no directive will save it.
- **`v++ 60-745 duplicate kernel name`** — two `.xo` files carry the same
  packaged name. The kernel cache must be keyed on the kernel name, not just its
  parameters.

## Style

See [STYLE.md](STYLE.md). The non-negotiables:

- Every module has ready/valid unless it is an intentional skid or purely
  combinational.
- Every FF has `_d`/`_q` signals; suffix order `_n` → `_d`/`_q` → `_i`/`_o`.
- Self-documenting names. Code carries almost no comments; write one line only
  for a *why* the code cannot express.
- Minimize new modules: instantiate an existing one, else inline, else — last
  resort — add one, after checking `third_party/`.
- Tool attributes (`KEEP_HIERARCHY`, `srl_style`) belong in TCL/XDC, not RTL.
- Never `(* dont_touch *)`. Never trust `MAX_FANOUT` over a structural fix.
- Don't remove resets to save area; don't add async ones.

## Recording an iteration

**Release only the builds that close.** A build that misses timing gets its
reports staged to `artifacts/<datecode>/` and nothing more. Releases live at
https://github.com/sifferman/ternip_claude_churner/releases and are meant to
answer one question: which bitstreams work. Of 202 historical releases only 63
carry a tarball, which is exactly why the list stopped meaning anything.

On a close, tag `YYYY.MM.DD-HHMM` from `build.log`'s first
`Run vpl: Step create_project: Started` marker, and **attach the build
directory including `kernel.xclbin`**. Exclude `hw_emu/`; `split -b 1800M` past
GitHub's 2 GB asset limit and attach every part.

The body carries WNS / TNS / failing-endpoint count, achieved frequency and
AUTO-FREQ-SCALING status, lanes and tok/s, and utilization. **Tag the config in
the title and in a `Config` row**, and source every staged file from that
config's own build directory — mixing configs in one release silently misleads.

Every build, pass or fail, goes in the search's results table: config, WNS, TNS,
failing count, tok/s, artifact datecode. Overwriting a descriptor in place loses
every intermediate config.

## Working unsupervised

Do not ask which build to kick; pick the smallest-blast-radius candidate and
start it. Log genuinely open questions to the branch's plan doc in `plans/`.

A regression is information, not a reason to stop. Record it in the plan doc so
it is not repeated, and ship a fresh change in the same breath — a pure-revert
build wastes hours.

The tok/s effort lives on a **feature branch**, never on `main`. `main` holds
the harness itself.
