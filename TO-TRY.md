# TO-TRY.md

Forward-looking list of changes that might improve timing closure on
this design. Claude pulls from this when it needs to kick a new
iteration and no one has directed a specific change. **eq2 must never
sit idle (see CLAUDE.md "Always keep eq2 building"), so default to the
top of "Claude-Generated" when nothing else is queued.**

Each entry should be actionable on its own: name the file(s) to touch,
the structural intent, and the expected effect. When something here
gets tried, move it to CLAUDE.md's "Things that have been done and
worked" (or "net-negative") list and delete the entry here.

---

## Pre-autonomous-loop historical best (legacy `ternary_matmul4/`)

For reference: before this autonomous harness was set up, the best
WNS achieved on the original `/mada/users/esifferm/GitHub/ternary_matmul4/`
working tree was **-0.386 ns (`hard_11.csv`, 2102 paths)**, very nearly
matched by hard_12.csv at **-0.389 ns (2082 paths)**. All later builds
on that tree (hard_13 / hard_14 / hard_15) came in WORSE.

That hard_11/12 state predated the convert ready/valid refactor and
the lane-split experimentation. The ONLY changes from upstream were:

- `rst_nq` reset register: `MAX_FANOUT=10` for synth-time replication
  close to consumers
- Per-block local `rst_ni_q` re-registration (UG949 §4.5 pattern #3)
  inside `ternip_tmatmul`, `ternip_rms`, etc.
- Source-attribute `MAX_FANOUT=25` on `tmatmul/state_q` and
  `tmatmul_operation_q`
- `MAX_FANOUT=25` on `pipelined_mem`'s `read_valid_q1/q2` and
  `write_valid_q1/q2`
- `(* MAX_FANOUT = "25" *)` on `rms/rms_op_q` and rms FSM `state_q`
- `MREG=1` (DSP M-stage register) enabled inside `ternip_starmul`

That's it. Pure attribute-driven hygiene + one DSP register stage.
No convert refactor, no lane modules, no `ternip_skid_lane`,
no sig/csig PISO→SIPO pipeline reg. Cleaner RTL than today's state
and a better build result.

**Lesson for the autonomous loop:** simpler RTL beat the "structurally
clean" refactor in our hands. If the loop seems to be flailing, consider
reverting recent additions and adding them one-at-a-time — bundled
changes mask which ones helped and which hurt.

---

## User-Generated

### 2. FF / area reduction for MaxCores BatchSize ramp (investigation)

User-directive: "We may find that we actually want to reduce the
number of flip-flops in the RTL, or simplify other things. Congestion
may be the biggest issue as we increase BatchSize. Maybe do an
investigation on what things could be removed from the RTL that don't
affect correctness, and don't decrease token throughput too much."

Throughput modeled via `ternary_matmul/sw_utils/target/
report_instruction_timing.py <config.svh> <model>` (MMfreeLM-370M).
On MaxCores BatchSize=16:
  VP=4 (default):  2028 tokens/sec
  VP=1 (current):   864 tokens/sec  ← per user-directive build_23

**Correction (2026.05.27):** First-pass analysis incorrectly claimed
`NumVectorRegisters` storage is in FFs. It is **BRAM** — see
`third_party/ternip/rtl/common/ternip_pipelined_mem_data_lane.sv:45`:
`logic [LaneBits-1:0] MEM [NumEntries];` with synchronous write at
line 62 and synchronous read at line 49. Vivado infers RAMB36/RAMB18.
NumVectorRegisters reductions therefore save BRAM, not FFs.

The true FF sinks at MaxCores BatchSize=16 are:
1. **`multioperand_accumulator`** tree-stage pipeline regs:
   each stage = D × FixedPointPrecision = 1024×16 = 16k FFs.
   With ~log2(TmatmulParallelism) = 8 tree stages × 16 cores = ~2M
   FFs.
2. **`tmatmul` accumulator + per-stage pipelining** in the 256-lane
   MAC array.
3. **`CoreInterconnectNumStages=6`** pipeline registers (×16 cores).
4. **Per-FU input/output pipeline registers** in rms / rowwise /
   sig / csig (the convert ready/valid pipeline regs added in
   the recent refactor each add D×16 = 16k FFs per pipe stage).
5. **`importvector` / `exportvector`** DECOUPLED_READY buffer
   stages = `ternip_pipelined_interconnect` with NumStages=1, each
   stage holds DATA_WIDTH × NumLanes = 1024×8 = 8k FFs/buffer.

**FF-reduction candidates, sorted by expected FF saved / throughput
cost (high → low ROI):**

#### A. `CoreInterconnectNumStages=4 or 3` (vs current 6)
- **FF saved per core**: 2 stages × interconnect width. CoreInterconnect
  width is ~D + control bits ≈ 1100 bits. 2 stages × 1100b × 16 cores
  ≈ 35k FFs. Smaller win.
- **Throughput cost**: Reduces latency between core ↔ DMA by 2 cycles.
  Lower latency = less stall waiting. Could be net POSITIVE for
  throughput (no swap impact).
- **Risk**: If 6 stages were chosen because of cross-SLR routing,
  dropping to 4 could violate setup time on the inter-stage hop.
  Cross-SLR LAGUNA delay is 1.5-3 ns; need to confirm placement.

#### B. Remove `(* MAX_FANOUT = "25" *)` source-side attributes
- **FF saved**: NEGATIVE — these CAUSE replication. Removing them
  REMOVES replicas. Could save ~5-10% FF on rms_op_q,
  pipelined_mem's read_valid/write_valid q's, tmatmul/state_q.
- **Throughput cost**: 0% (not a timing change, just a routing
  attribute).
- **Risk**: TCL replication in `pre_phys_opt_design.tcl` already
  targets these nets; the source-side attribute may be redundant.
  But this depends on TCL targeting being good enough.

#### C. Single-stage `multioperand_accumulator` tree (vs default depth)
- **FF saved per core**: Each tree stage = D × FixedPointPrecision
  = 16k FFs. Removing 1 stage saves 16k FFs × 16 cores = **256k FFs**.
  Removing 2 stages saves 512k FFs. This is the largest FF-saving
  candidate on the list.
- **Throughput cost**: Each stage removed = 1 less cycle latency on
  tmatmul_go. Currently tmatmul_go is BW-bound per
  `report_instruction_timing.py` (DRAM bandwidth saturating before
  compute), so latency reduction gives near-zero throughput gain
  but also near-zero throughput LOSS.
- **Risk**: Combinational depth in the adder tree grows. At 300 MHz,
  each stage handles ~3 ns of combinational add. Removing stages
  could push setup violations into the accumulator. Run yosys-fanout
  + a small Vivado smoke build at reduced stages before committing.

#### D. `importvector` / `exportvector` `DECOUPLED_READY=0` (vs current 1)
- **FF saved**: One full ternip_pipelined_interconnect buffer
  (DATA_WIDTH × NumLanes ≈ 1024 × 8 = 8k FFs per importvector × 4
  importvectors per core × 16 cores = 512k FFs).
- **Throughput cost**: Removes the read-side handshake decoupling.
  If reads stall back-pressure into the request path. Hard to
  quantify without sim. Could regress.
- **Risk**: CLAUDE.md lists DECOUPLED_READY=1 on importvector pmem
  as a "Things that have been done and worked" win. Removing it
  is reverting that win.

#### E. `NumVectorRegisters=2 or 3` (vs current 4) — **BRAM saver, not FF**
- **BRAM saved per core**: `NumVectorRegisters × D × FixedPointPrecision`
  bits stored in BRAM (RAMB36/RAMB18). At NVR=4, D=1024, FPP=16:
  64 Kb per core × 16 cores = 1 Mb (= ~28 RAMB36 × 16 cores = ~450 BRAMs).
  Dropping to 2 saves ~225 BRAMs.
- **Throughput cost**: Depends on register pressure in scheduled
  program. If the compiler can fit MMfreeLM-370M in 2 registers
  (likely for the simple matmul/RMS dataflow), 0% cost. If not, more
  swaps → more cycles. Check via `swap_instructions_counter` in
  `report_instruction_timing.py` (currently 394 swaps at NVR=4; rerun
  at NVR=2 to see counts).
- **Why it's listed**: BRAMs are NOT typically the congestion driver
  on AU250 (DSP and routing usually are), but if BRAM utilization is
  high enough to force placements into specific SLRs, reducing BRAM
  can give the placer more flexibility.

#### F. `NumLanes=4` on importvector pmem (vs current 8)
- **FF saved**: Halves the lane count → halves lane FF overhead.
  Per-lane CE wire fanout doubles. Could regress timing.
- **Throughput cost**: 0% (structural, not timing).
- **Risk**: Lane count was tuned for the wide-CE problem. Going
  back risks reintroducing the FO=4163 cluster from build_9.

#### G. Reduce `BatchSize-dependent` per-batch buffers
- **FF saved**: Many modules use a buffer depth = BatchSize. At
  BatchSize=16 that's 16x the FF cost. Reducing to depth=4 saves
  3x.
- **Throughput cost**: If buffers were sized for worst-case
  back-pressure, reducing them could cause stalls. Need
  throughput model to verify.
- **Where to look**: gbfifo_loadstore_w, gbfifo_tmatmul, FIFO depths
  in dv/ instances.

**Investigation method:** when picking the next iteration's candidate,
run `report_instruction_timing.py` on the modified config to verify
the throughput cost is acceptable BEFORE committing to the build.

### 1. Drop `kernel_compiler_margin` from 20% to 5% (or lower)

**Where:** v++ link command in `ternary_matmul/synth/pynqvivado_au250/`
(check Makefile for the v++ invocation; pass via `--xp
prop:solution.kernel_compiler_margin=5`). OR set via the kernel.cfg
profile if there's a directive for it.

**What:** Vivado/Vitis adds a default 20% clock-uncertainty margin
when AUTO-FREQ-SCALING-04 picks the kernel frequency. This explains
the 30-65 MHz gap between WNS-implied zero-slack frequency and the
reported achieved frequency across builds 1-6.

| Build | WNS | zero-slack freq | × 0.80 (20%) | Vivado picked |
|---|---:|---:|---:|---:|
| 5:01 AM | -0.259 | 278 MHz | 222 MHz | 242.1 |
| 12:34 AM | -0.243 | 279 MHz | 224 MHz | 213.8 |
| 3:13 AM | -0.166 | 286 MHz | 229 MHz | 230.5 |

Dropping to 5% would unlock ~+42 MHz on the current state at zero
RTL risk. Worst case: a build might not be board-stable at the
higher reported frequency (the 20% was there for jitter / process
variation safety), discoverable at hardware validation time but
not at synth time.

**Why:** Forum-confirmed (Xilinx forums) that
`--xp prop:solution.kernel_compiler_margin=<pct>` controls this.
Default 20 is overly conservative for our design's clock paths.
Real-world data point: **ThunderGP** uses `kernel_compiler_margin=10%`
on every graph kernel (see
`references/ThunderGP/application/common.mk` lines 90, 94) — so
production-ready Xilinx Vitis builds DO tune this knob, and 10%
is the empirical "still safe" floor for accelerator kernels.

**Risk:** Low — config-only, no RTL movement, easy to revert. The
new frequency may not run reliably on the AU250 board if the
design has any high-jitter clock paths, but that's a runtime
characteristic detected by board validation, not a synth issue.
Related knob: `--kernel_frequency=N` to set a hard target, and
`--xp param:compiler.enableAutoFrequencyScaling=0` to disable
scaling entirely.

---

---

## Claude-Generated

These are ideas Claude has surfaced from timing-report analysis or
from reading CLAUDE.md "What to try next" / QUESTIONS.md. Roughly
prioritized — top of list = highest expected impact / lowest risk.

### A. Try `Performance_EarlyBlockPlacement` strategy

**Where:** `synth/vivado_common/` (add a strategy override) or via
`kernel.cfg`'s `[vivado]` section:
```
prop=run.impl_1.strategy=Performance_EarlyBlockPlacement
```

**What:** The Xilinx xbtest timing-closure tips doc explicitly says
"frequently Performance_EarlyBlockPlacement" is the strategy that
closes when default doesn't. We're using Vivado defaults today
(no strategy override anywhere in synth/). Single specific strategy
is a low-risk experiment.

**Why:** Default Vivado synth/impl strategies are tuned for
typical user code; this one was hand-picked for HBM/AXI-heavy
Vitis kernels exactly like ours. Could shift the 246.9 MHz peak
upward or stabilize the placement-variance we saw between
2026-05-25 6:46 PM and 2026-05-26 5:49 AM (same config, 246.9 vs
236.0 MHz).

**Risk:** None — alternative strategy, same RTL, same config. If
it doesn't help, revert the one line.

### B. Enable phys_opt + route Explore directives + post-route phys_opt — **ALREADY APPLIED (baseline)**

Verified 2026-05-26 2:05 PM: `synth/pynqvivado_common/
generate_kernel_cfg.tcl` lines 28-52 already emit:
```
[vivado]
prop=run.__KERNEL__.{STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS}={-retiming}
prop=run.impl_1.STEPS.OPT_DESIGN.ARGS.DIRECTIVE=Explore
prop=run.impl_1.STEPS.PLACE_DESIGN.ARGS.DIRECTIVE=ExtraNetDelay_high
prop=run.impl_1.STEPS.PHYS_OPT_DESIGN.IS_ENABLED=true
prop=run.impl_1.STEPS.PHYS_OPT_DESIGN.ARGS.DIRECTIVE=AggressiveExplore
prop=run.impl_1.STEPS.ROUTE_DESIGN.ARGS.DIRECTIVE=Explore
prop=run.impl_1.{STEPS.ROUTE_DESIGN.ARGS.MORE OPTIONS}={-tns_cleanup}
prop=run.impl_1.STEPS.POST_ROUTE_PHYS_OPT_DESIGN.IS_ENABLED=true
prop=run.impl_1.STEPS.POST_ROUTE_PHYS_OPT_DESIGN.ARGS.DIRECTIVE=AggressiveExplore
```

This is **the FireSim TIMING + CONGESTION strategy already
applied as the baseline** for every build in this loop (since
seed commit 9b9ca24). Don't re-propose. The only directive lever
still untried at impl-step level is `opt_design`'s additional
flags from FireSim CONGESTION (`-hier_fanout_limit 512 -muxf_remap
-propconst -retarget -sweep`).

### D. Alternative strategy: `Performance_ExploreWithRemap`

**Where:** `[vivado]` section in `kernel.cfg`. Per
`references/halalboro-fpga-accelerators/Vitis-AI/examples/waa/apps/
resnet50/build_flow/DPUCVDX8G_vck190/vitis_prj/scripts/system.cfg`
line 27:
```
prop=run.impl_1.strategy=Performance_ExploreWithRemap
```

**What:** Strategy alternative to `Performance_EarlyBlockPlacement`
(candidate A). More aggressive remap-based netlist transformations,
balances exploration depth vs. runtime. Used by Vitis-AI's
production resnet50 build on VCK190.

**Why:** Two-strategy A/B test — try A first, then this if A doesn't
help. Different strategies pick different starting points, so they
sample different placement minima.

**Risk:** None (same as candidate A — alternative strategy, same
RTL/config).

### E. FireSim CONGESTION extras: `opt_design` MORE_OPTIONS — **PARTIALLY APPLIED**

Baseline already has: `synth_design -retiming`, `opt_design
-directive Explore`, `phys_opt AggressiveExplore`, `route
Explore + -tns_cleanup`, post-route phys_opt AggressiveExplore.
What's NOT in the baseline is the `opt_design` MORE_OPTIONS set
(`-hier_fanout_limit 512 -muxf_remap -propconst -retarget -sweep`).
That's the remaining piece of FireSim CONGESTION.

(Original full-strategy entry below preserved for reference.)

### E-original. FireSim "CONGESTION" strategy: synth retiming + tight fanout cap + muxf_remap

**Where:** `references/firesim/platforms/xilinx_alveo_u250/cl_firesim/
scripts/strategies/strategy_CONGESTION.tcl`. FireSim runs on the
SAME PART we target (xcu250-figd2104). Their CONGESTION strategy
script combines:
```
synth_options    = "-retiming"
opt_options      = "-hier_fanout_limit 512 -muxf_remap -propconst -retarget -sweep"
phys_directive   = "AggressiveExplore"
route_directive  = "Explore"
```

**What:** Synthesis-time retiming lets Vivado move registers across
combinational logic to balance pipeline stages — could rebalance
the long combinational chains we keep seeing (tmatmul_operation_q,
csig, tmatmul_dma). `-hier_fanout_limit 512` caps replication
proactively (vs our reactive `force_replication_on_nets` rules).
`-muxf_remap` rewrites wide muxes into narrower trees — directly
targets the FSM decode cones in our build.

**Why:** Synthesis-time fixes hit the netlist before placement gets
a chance to commit to a bad layout. Vivado users on the same part
(xcu250) ship with these options as their CONGESTION strategy.

**Risk:** Synthesis runtime grows ~10-20% with `-retiming`. Other
options are post-synth and only affect impl time. Otherwise
no functional risk.

### F. FireSim TIMING strategy: SLL-reg-hold-fix — **PARTIALLY APPLIED**

Baseline already has `place_design -directive ExtraNetDelay_high`
and `route -tns_cleanup`. What's NOT in baseline is the
`post_route_phys_opt_design -sll_reg_hold_fix` flag — the
U250-LAGUNA-specific lever for cross-SLR hold violations. To
apply: extend the kernel.cfg directive on
`STEPS.POST_ROUTE_PHYS_OPT_DESIGN` to include a MORE_OPTIONS
clause with `-sll_reg_hold_fix`.

(Original full-strategy entry below preserved for reference.)

### F-original. FireSim "TIMING" strategy: ExtraNetDelay_high + tns_cleanup + post-route SLL fix

**Where:** `references/firesim/platforms/vitis/cl_firesim/
build-strategies/strategy_TIMING.cfg`. Combines:
```
place_design       -directive ExtraNetDelay_high
route_design       -tns_cleanup
post_route_phys_opt_design -sll_reg_hold_fix
```

**What:** `ExtraNetDelay_high` tells the placer to assume worst-case
net delay early (the placer makes more pessimistic decisions to
ensure routes have slack). `-tns_cleanup` is a route_design option
that removes unused routing after route to free capacity for
critical paths. `-sll_reg_hold_fix` fixes hold violations on
SLR-crossing register paths (LAGUNA cells) — directly addresses
the cross-SLR issues this design has.

**Why:** Three orthogonal levers on three different impl stages.
The SLL-reg-hold-fix is U250-specific (LAGUNA) and could be the
single biggest unblocker for our cross-SLR paths.

**Risk:** Build time grows. ExtraNetDelay_high can sometimes make
WNS slightly worse if the placer was already finding a good
layout — A/B carefully.

### G. Disable AUTO-FREQ-SCALING entirely (production-style, not for closure debug) — **IN FLIGHT (build_19)**

**Status:** Pre-staged for build_19 (2026.05.27, kicks when build_18
ends). Will move to CLAUDE.md done/net-negative when result lands.

**Where:** In `synth/pynqvivado_common/generate_kernel_cfg.tcl`,
add this section to the generated kernel.cfg:

```
[advanced]
param=compiler.skipTimingCheckAndFrequencyScaling=1
```

Two reference points:

1. **Official source — UG1702 (Vitis Reference Guide), 2023.1.**
   `references/vivado-docs-2023.1/Vitis Reference Guide (UG1702).htm`
   line 10249:

   > `compiler.skipTimingCheckAndFrequencyScaling`
   > Type: Boolean. Default Value: FALSE.
   > This parameter causes the Vivado tool to skip the timing check
   > and optional clock frequency scaling that occurs after the last
   > step of implementation process, which is either `route_design`
   > or post-route `phys_opt_design`.
   > Applies to: `v++ --link`, `vpl.impl`.

   Our build runs `v++ --link` and goes through `vpl.impl`, so the
   parameter applies. Setting it to `1` (=TRUE) skips the final
   timing check + the AUTO-FREQ-SCALING-04 decision.

2. **Working example — `references/simplitis/02-axil-bram/
   xilinx_u250_gen3x16_xdma_4_1_202210_1/kernel.cfg`.** Uses the
   `[advanced] param=…=1` config-file form (not `--xp`).

**Important — config-file form, not `--xp`:** The `--xp param:…`
CLI form was DEAD in Vitis 2023.1 (silently ignored — verified
in 2026-05-26 11:15 AM build_9 with `kernel_compiler_margin`
and build_10 with `run.impl_1.strategy`). The `kernel.cfg
[advanced] param=…` form is the one Vitis 2023.1 actually
parses. Ignore the `halalboro-fpga-accelerators` `.mk` snippet's
`--xp` syntax — it's from an older Vitis version.

**What:** Skips Vivado's final timing-summary check and the
AUTO-FREQ-SCALING-04 downshift. The xclbin gets packaged at the
requested clock (300 MHz from the U250 xdma platform) regardless
of WNS. Per-step `report_timing_summary` data is unaffected, so
our CSV via `generate_timing_csv.tcl` against `prj.xpr` still
reports true WNS/TNS.

**Why:** Removes the silent ~20-30% margin tax that
AUTO-FREQ-SCALING-04 applies (e.g. build_16 with WNS=-0.190
implies a zero-slack frequency of ~282 MHz, but the bitstream
shipped at 218.5 MHz — a 29% downshift). With this param=1, the
bitstream ships at exactly the requested 300 MHz and our CSV
tells the true WNS story.

**Companion knob:** `compiler.enableAutoFrequencyScaling=0`
(also a `[advanced] param=`). Skipping the timing check
implicitly disables scaling; setting both explicitly is
belt-and-suspenders.

**Risk:** If the design genuinely doesn't close, the bitstream is
shipped at 300 MHz and may glitch on the failing paths at
runtime. We discover this at build time (`report_timing_summary`
in the CSV still reports negative WNS) rather than runtime,
though — Vivado's per-step reports are independent of this
flag. The board may not run at 300 MHz reliably; that's a
deferred problem (worry about it when we have a passing build).

### C. Force BRAM LOC from a known-good build (reproduce the lucky placement)

**Where:** New `synth/vivado_common/place_design_pre.tcl`-style
file with hand-extracted `set_property LOC ... [get_cells ...]`
constraints.

**What:** The xbtest doc's "Force LOC of BRAM" workflow:
1. Open a passing-timing DCP (the 246.9 MHz build's checkpoint)
2. `find_1 [get_cells -hierarchical -filter {PRIMITIVE_TYPE =~ BLOCKRAM.*.*}]`
3. Highlight + right-click → "fix cells"
4. `write_xdc -exclude_timing kernel_bram.xdc`
5. Add the LOCs to `place_design_pre.tcl` in the failing build

This pins the BRAM locations from a lucky build. The placer
respects them and reproduces (most of) the good layout.

**Why:** Directly attacks the Vivado-non-determinism issue that
made 2026-05-26 5:49 AM lose 11 MHz on what should have been an
identical config to 2026-05-25 6:46 PM.

**Risk:** LOC constraints are fragile — if any BRAM moves or is
removed in a future RTL change, the constraint breaks. Need to
re-extract after significant kernel changes. Medium effort to set
up; high payoff if the 246.9 MHz lucky placement was BRAM-dominated.

### 0. csig_parallelized PISO → csig_out_q skid revisit (newly relevant)

**Where:** `ternary_matmul/third_party/ternip/rtl/math/ternip_csig_parallelized.sv`
near the existing csig_out_q skid (lines ~108-130).

**What:** 2026.05.25-2137 surfaced a 1411-endpoint cluster in
`csig_parallelized/piso_loadstore_r/two_fifo.fifo0/head_r_reg →
csig_out_q_reg[*]` at -0.719 ns slack. The existing comment notes a
build_15-era 1134-path / -0.698 ns fix lived here; this seems to be
the same cluster resurfacing under placement pressure. Options:
- 2-deep skid (csig_out_pre_q + csig_out_q) instead of the current
  1-deep — gives the placer more freedom to spread the PISO→csig
  combinational cone.
- Lane-split the PISO output bus so each lane has its own
  csig_out_q — drops per-FF fanout from VectorParallelism to 1.

**Why:** Was masked by the build_4 over-replication but the cluster
existed already in build_1 at -0.259 ns (3rd-tier path; not WNS
dominant). With buffer-tready and trace_memory cleaned up, this is
plausibly the next ceiling.

**Risk:** Latency change (1-cycle for the skid extension). Test
tmatmul_tb both simulators carefully — csig is in the rowwise path.

### 1. `tmatmul_operation_q[1]` FSM transition pipelining

**Where:** `ternary_matmul/third_party/ternip/rtl/fus/ternip_tmatmul.sv`,
the `tmatmul_operation_q` register and its FSM cone.

**What:** 2026.05.24-0827 surfaced a 7-LUT-level self-loop on
`tmatmul_operation_q[1]` → its MAX_FANOUT replicas (-0.308 ns slack
when the importvector wide-CE cluster was displaced). The FSM
combinational decode (`state_q == X && tmatmul_operation_q == Y`) is
the deep cone. Either:
- Register the next-state decode (insert an FF between the big
  `else if` chain and `tmatmul_operation_d`), accepting a 1-cycle
  FSM latency.
- Split the wide `case`/`else if` into smaller pre-decoded conditions
  that can be flattened.

**Why:** Even with TCL replication, MAX_FANOUT=25 leaves 13 replicas
that fan out 25 each; the deep logic cone is what makes the path
slow. Cutting the cone in half is structural, not placement-dependent.

**Risk:** FSM latency change may shift downstream handshakes by
1 cycle — test thoroughly on tmatmul_tb both simulators.

### 2. `latched_tmatmul_addrs_q` wide-CE refactor

**Where:** `ternary_matmul/third_party/ternip/rtl/ternip/ternip_core.sv`
near line 441 (`latched_tmatmul_addrs_d`/`_q` declaration) and the
write site at line 591.

**What:** `latched_tmatmul_addrs_q` is `[NumDdrBanksPerTmatmul-1:0]` of
`ddr_address_t`. The write site demuxes by `tmatmul_addr_counter_q`
into one of the banks — creating a wide CE selection cone. CLAUDE.md
item #3 calls this out: FO~410 on the alumacc cone. Split the array
into separately-named per-bank registers (`latched_addr_b0_q`,
`latched_addr_b1_q`, ...) each with its own narrow CE, then re-pack
on the read side.

**Why:** Surfaced as cluster B in 2026.05.24-0827 (`tmatmul_operation_q[1]`
→ `latched_tmatmul_addrs_q.CE`). Was masked by 2026.05.24-0501's wide-CE
cluster. Even with 2026.05.24-0827 reverted, this is the second-tier path.

**Risk:** Low — pure structural rename. Read-side semantics
unchanged; write demux just becomes per-bank `if`s.

### 3. MOA → importvector backpressure pipeline stage

**Where:** the data path between `ternip_multioperand_accumulator`'s
`out_valid_q` and the importvector buffer's wide tdata register CE.
Probably `ternip_tmatmul.sv` around the accumulator → importvector
hop, or use `ternip_pipelined_interconnect` to insert a stage.

**What:** CLAUDE.md item #1. The MOA lives in one part of the SLR
layout, the importvector buffer in another. Insert a register slice
on `accumulator_q2_ready` or revisit the buffer's CE source
structurally.

**Why:** This is the originally-identified cluster in CLAUDE.md's
"things to try" list. We never hit it in build_0..2026.05.25-1846 because
the `rms → convert` cluster dominated first, but with 2026.05.24-0501's
rms input slice in place the next dominant SLR-crossing is the
MOA → importvector edge.

**Risk:** Adding a register here adds 1 cycle of latency on the
accumulator → importvector hop. Check tmatmul_tb golden output
timing.

### 4. Per-bank importvector → MOA skid stages

**Where:** `ternip_tmatmul.sv` per-bank importvector ↔ MOA edges.

**What:** CLAUDE.md item #2. Cross-SLR data movement at 300 MHz
needs explicit pipelining; `ternip_pipelined_interconnect` already
exists for this and is used elsewhere. Insert another instance where
MOA output / importvector input crosses SLR boundaries.

**Why:** Same root cause as #3 but from the data side rather than
the control side.

**Risk:** Same as #3 — latency shift.

### 5. AXI Lite control interconnect timing investigation

**Where:** `axi_interconnect_ctrl` instantiation, possibly in
`ternip_buffered.sv` or the AXI infrastructure auto-generated by
Vitis.

**What:** CLAUDE.md item #4. After kernel-side bottlenecks clear,
the AXI Lite control interconnect sometimes becomes the WNS source.
Mostly placement-dependent, hard to fix structurally. Possible
levers: floorplanning hints in `pre_place_design.tcl`, or fewer
kernel-side changes per iteration to give Vivado breathing room.

**Risk:** Mostly an analysis/diagnostic task; the fix (if any) is
placement-tuning rather than RTL.

### 6. Move to MaxCores + scale BatchSize (only after OneCore closes)

**Where:** Switch CONFIG to `xcu250_D=1024_MaxCores`. Tune
`BatchSize` (target 20+), `VectorParallelism`, `LutParallelism`,
`CoreInterconnectNumStages`.

**What:** CLAUDE.md item #6. Once OneCore is "close enough" to
closure (WNS within ~0.1 ns of 0), the next productive direction is
multi-core throughput.

**Why:** OneCore at WNS=-0.259 (2026.05.24-0501) isn't quite there yet, but
not far. If multiple OneCore-direction iterations stop moving WNS,
this is the alternate axis.

**Risk:** MaxCores debugging is much harder than OneCore. CLAUDE.md
warns explicitly against this until OneCore is close.

---

## When this list gets short

Re-read the latest build's CSV for new cluster patterns, scan
CLAUDE.md "Things that have been done and worked" for ideas to
extend (e.g., the 2026.05.24-0501 input slice could have analogues on
exportvector, rowwise_operation, etc.), and re-read `references/` for
new techniques.

## User-Generated (2026-07-15): Heterogeneous per-SLR BatchSize (smaller BS on SLR1)
**Motivation (user):** SLR1 hosts the XRT shell/static logic → most congested → its CU
is the tightest fit. Give the SLR1 CU a smaller BatchSize so the other 3 SLRs can run
full BS.

**Constraint:** `nk=ternip_ip:4` replicates ONE kernel → all CUs share BatchSize.
Heterogeneous BS requires TWO kernel .xo variants.

**Gated on:** the running uniform BS=8 + rms-fix build (2026.07.15-0803). OOC closed at
BS=8 but OOC has no shell, so it did NOT test SLR1 congestion. The full build is the real
test:
- If it CLOSES → uniform BS=8 = 2778 tok/s, heterogeneity unnecessary (best outcome).
- If it FAILS clustered in the SLR1 CU (ternip_ip instance 4, slr_order {0 3 2 1} → the
  4th CU) → heterogeneous is the fix.

**Implementation sketch (if needed):**
1. Two config .svh: BS=8 (ternip_ip) + BS=4-or-6 (ternip_ip_small).
2. Build two kernel .xo: package_kernel.tcl with $name=ternip_ip and =ternip_ip_small
   (separate synth+package each). generate_kernel_xml.tcl for both names.
3. generate_kernel_cfg.tcl: `nk=ternip_ip:3,ternip_ip_small:1`; sp=/slr= map the 3 big
   CUs to SLR0/2/3 (banks 0,2,3) and the small CU to SLR1 (bank 1).
4. Host (test/benchmark/demo): per-instance BatchSize (instruction stream + data layout
   differ per CU). Bank map already [0,3,2,1]; small CU is instance 3 → SLR1/bank1.

**tok/s:** (8,8,8,6)=30 units ~2604; (8,8,8,4)=28 ~2431. Both beat BS=6 (24, 1943),
both lose to uniform BS=8 (32, 2778). So only pursue if uniform BS=8 misses on SLR1.

### UPDATE 2026-07-15: Heterogeneous per-SLR BatchSize — DATA SAYS IT WON'T HELP
The uniform BS=8 + rms-fix full build (2026.07.15-0803) failed −0.430 with 13550 failing
endpoints spread across ALL 4 CUs: ip_2/SLR3 8518, ip_1/SLR0 7508, ip_4/SLR1 6284,
ip_3/SLR2 4734. **SLR1 is NOT the bottleneck** (SLR3/SLR0 fail worse). Each CU's failures
are intra-CU, and no SLR can individually hold BS=8. So smaller-BS-on-SLR1 (or any config
with a BS=8 CU) will not close. Heterogeneous is DEAD for exceeding uniform BS=6 unless a
fundamentally lower-density kernel is developed first. BS=6 (1943 tok/s) is the nk=4
tok/s ceiling. Do NOT build the 2-kernel heterogeneous flow against the current density.
