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

## User-Generated

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
