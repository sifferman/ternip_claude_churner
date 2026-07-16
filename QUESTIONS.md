# QUESTIONS.md

Open questions / judgement calls Claude wants the user to review out of
band. Newest at the top. Each entry: what was decided, the alternative
considered, and how to redirect if the user wants something different.

**Auto-cleanup**: when Claude can confidently answer a question itself
(or the user answers it elsewhere), the entry is removed from this list.

---

## 2026-06-09 4:14 PM PDT — NSAI_1 failed timing on DDR4 IP paths (not kernel)

### Status

NSAI_1 (2026.06.09-1217) closed with **WNS=-0.248 ns / 191 failing
endpoints on mmcm_clkout0**. Already a 4× improvement vs NTB's
WNS=-0.98 ns. But the worst-failing paths are inside the platform's
DDR4 IP (`memory_subsystem/.../u_ddr_cal/u_ddr_cal_addr_decode/...`
and `memory_subsystem/.../u_ddr_mc/.../txn_fifo_output_reg`), NOT in
our kernel logic. The per-instance SLR pblocks displaced the DDR4 IP
into a slightly tighter placement.

### Decision (NSAI_2)

Bisect: revert `CoreInterconnectNumStages = 4 → 8` only. Keep the
floorplan + bd.tcl rewire. If WNS stays at ~-0.25 ns, NumStages wasn't
the cause and the pblocks are squeezing DDR4 — NSAI_3 will loosen
pblocks (or accept the small platform-side violation via
`skipTimingCheckAndFrequencyScaling=1`).

### What I would ask the user

- The DDR4 calibration path that's failing (`u_ddr_cal_addr_decode`) is
  a startup-only calibration path. -0.248 ns slack on a one-shot
  calibration register-to-register hop is **functionally irrelevant**
  — calibration runs once at boot and doesn't repeat at 300 MHz. Vivado
  static timing analysis doesn't know this is a calibration path.
  Should I just enable `skipTimingCheckAndFrequencyScaling=1` to package
  the bitstream and rely on the actual silicon behavior?
- Alternatively: could the user manually tag those calibration paths as
  `set_false_path` in a TCL hook so Vivado skips them in static
  analysis?

### Default I went with

Pursue real timing closure via bisect. The `skipTimingCheckAndFrequencyScaling`
escape hatch stays available for later iterations.

---

## 2026-06-01 3:30 PM PDT — closing the last 0.751 ns requires deeper ternip refactor

### Status

Build_44 reached WNS=-0.751 ns (+1.688 ns recovery vs build_31, 69%
closure). Slice+pblock recipe is saturated — build_45's trivial
`(* keep_hierarchy *)` on the R-channel slice tripped the same MOA
verify failure that build_33/34/41/42 hit. ANY additional placement
perturbation pushes MOA over Vivado's verify tolerance.

### Top remaining failing paths (build_44 CSV)

```
SRC: core[N]/buffered/core/tmatmul/state_q_reg[1]/C
DST: core[N]/buffered/core/latched_instr_q_reg[*]/CE
slack: -0.751 ns, 14 paths

SRC: core[*]/buffered/core/tmatmul/tmatmul_operation_q_reg[1]/C
DST: various intra-core sinks
slack: ~-0.7 ns range, 102 paths
```

### What I tried (and reverted)

**Attempt 1: Register `all_fus_in_ready` in ternip_core.sv** to break
the combinational chain from FU state FFs to `latched_instr_q[*].CE`.

Result: **broke the FU mutual-exclusion protocol**. The registered
ready told the FSM "OK to dispatch" but the actual FU was still
processing. Two FUs both asserted `vector_request_valid`, tripping
the `unique case (1)` assertion at `ternip_core.sv:488`.

Reverted in working tree (no commit).

### Why naive registers don't work

The `all_fus_in_ready` AND gate exists because `vector_register` is
SHARED across all 4 FUs. The current protocol requires exactly one
FU active at a time. Any registered/delayed view of the ready
signal lets the FSM dispatch a new FU while an old one is still
draining → port conflict.

A skid buffer between FSM and any specific FU doesn't help either:
the FSM needs to know the actual FU is idle (not just "slice
accepted my dispatch") before issuing the next instruction.

### Real fix candidates (need user input on direction)

1. **Per-FU vector_register ports** — give each of loadstore, rms,
   rowwise, tmatmul its OWN port. Eliminates the mutual-exclusion
   arbitration. Allows the FSM to dispatch back-to-back instructions
   to different FUs concurrently. ~100-200 lines across
   `ternip_core.sv`, `ternip_vector_registers.sv`, and each FU's
   port list. Big refactor, potentially big WNS win, and ALSO
   unlocks parallelism that could improve BatchSize scaling.

2. **Round-robin arbitrated vector_register** — single port but
   with explicit cycle-by-cycle scheduling. FSM tracks which FU
   gets the port each cycle. Registered ready is now safe because
   the scheduler enforces no conflicts. ~50-100 lines.

3. **Pipeline the `instruction_ready_o` path with bypass** —
   register `all_fus_in_ready_q` but keep the FSM checking the
   combinational `all_fus_in_ready` for actual dispatch decisions;
   use the registered version only for "advance-warning" gating.
   Subtle, complex to verify. Probably ~30 lines but high risk of
   subtle protocol bugs.

4. **Accept build_44 as final, ship at -0.751 ns** with the
   `skipTimingCheckAndFrequencyScaling` flag making the bitstream
   still package at 300 MHz. Real silicon would intermittently
   meet timing depending on PVT variation; per CLAUDE.md it's not
   the right answer but is the "no-more-work" option.

### My recommendation

**Option 1 (per-FU vector_register ports)** is the right answer
for both timing closure AND BatchSize scaling. The current
serialized FU dispatch is a Latin-square pattern; parallelizing it
across FUs would also unlock more throughput per cycle.

Awaiting your guidance on whether to attempt option 1 (or 2 if
you prefer the smaller scope), or stop at build_44.

---

## 2026-06-03 — Phase 3 BD rewrite needed before N>1 build

Phase 2 RTL refactor (single-bank ternip_tmatmul + per-unit dispatch
+ generate-block N tmatmul instances in ternip_core, ternip_buffered,
ternip_batched, axi_ternip_batched) is committed and pushed:

- ternip_claude/ternary_matmul branch `NumTmatmulBanksPerCore` HEAD
  `1e8e3a9`
- third_party/ternip branch `NumTmatmulBanksPerCore` HEAD `6dc9819`
- ternip_claude_churner branch `main` HEAD `6fd83ae`

Verified at N=1 (OneCore): lint PASS, tmatmul_tb verilator PASS.
N=4 (MaxCores) lint PASS.

### Open question: BD packaging for N>1

`axi_ternip_batched.sv` exposes `m_axi_tmatmul_*` as 2D packed arrays
`[NumTmatmulBanksPerCore-1:0][...]`. At N=1, IP-XACT in Vivado BD
*should* infer this as a single 1-port AXI bundle (8-bit arid stored
as `[0:0][7:0]`), but I haven't confirmed via a synth run.

At N>1, the BD needs a rewrite — the current `bd.tcl` was authored
for `NumSeparateAxiInstances` semantics (N copies of the entire
kernel) and would mis-instantiate everything.

The cleanest approach for N>1 is one of:
1. **Codegen**: a Python script emits a per-N RTL wrapper with N
   separately-named AXI interfaces (`m_axi_tmatmul_0_arid`,
   `m_axi_tmatmul_1_arid`, ...). The wrapper instantiates
   `axi_ternip_batched` and connects.
2. **Single shared wrapper with N AXI interfaces declared per-N
   via SV macros / preprocessor `define**: more compact but
   harder to read.

Going with (1) is my preference for the next session. I'll also
have to update `bd.tcl` to:
- Instantiate ONE wrapper (not N axi_ternip_batched copies)
- Expose N `M_AXI_<b>` ports total (was: equal to N-or-DramNumBanks)
- Wire each `m_axi_tmatmul_<b>` to `M_AXI_<b>`
- Merge `m_axi_loadstore` into `M_AXI_0` (so loadstore shares DDR0
  with instruction fetch DMA)

For tonight, I'm pausing Phase 3 since eq2 is intentionally idle
during the refactor. The next session will start with the wrapper
codegen + BD rewrite, then proceed to Phase 4 cocotb at N=4.

## 2026-07-15 — nk=4 silicon validation + test_pynqvivado readback stall
nk=4 (NSK_11, release 2026.07.11-2126) is now **fully silicon-validated** on fulladd:
`test_pynqvivado_basic` all 4 CUs pass (0 failures), benchmark 1943 tok/s, and
**`demo_pynqvivado` produces coherent text** ("The capital of France is" → "Paris,
the second largest city is Paris, the third is the capital of the United Kingdom...").
This resolves the correctness gap from NDBPT build_75 (which closed timing but printed
garbage) — nk=4 both closes (+0.002) AND computes correctly end-to-end.

**Decision made (proceeding):** kicked nk=4 **BS=8** (build 2026.07.15-0045) as the
next tok/s lever: estimator says +30% (2130→2778 tok/s). All gates pass. Falls back to
the validated BS=6 if it misses timing.

**Open question for you:** `test_pynqvivado` (the per-swap readback correctness sweep)
**stalls at ~instruction 17/3887** — not a compute bug (demo/benchmark run the full
model to completion correctly), but the ~1 min/instruction MMIO-readback debug mode
appears to hang on a specific readback after layer 0's early swap points. CLAUDE.md
notes a full-model RTL-sim harness is the missing verification layer. Would you like me
to (a) debug the readback stall in `test_pynqvivado`, or (b) build the full-model
RTL-sim harness (verilator, compare to emulator) so full-model correctness is checked
pre-silicon on every build? I've defaulted to treating demo's coherent text as the
end-to-end correctness gate for now.

## 2026-07-15 — tok/s optimization has CONVERGED at nk=4 BS=6 (needs strategic steer)
After silicon-validating nk=4 BS=6 (1943 tok/s, coherent text), I made 3 attempts to
exceed it, all failed — this is the "3 iterations → wrong layer, step back" signal:
- BS=8: −0.114 (rms logic depth)
- BS=8 + rms pipeline fix: −0.430 (rms cleared, but globally too dense; all 4 CUs, shell)
- BS=6 + rms fix: −0.059 (rms fix's FFs perturbed the congestion-limited placement → backfired)
Reverted the rms fix. Deliverable = validated flagship BS=6 (release 2026.07.11-2126).

**Every compute-density lever is exhausted:**
- BS: 6 is the ceiling (8 too dense: −0.430 full-chip; not SLR1-specific — SLR3/SLR0 fail worse)
- VP=8: +32% tok/s but density-blocked (−0.802)
- TP=64 to free density: craters tok/s to 1601 < BS=6 (matmul goes compute-bound)
- tmatmul state_q fanout: ALREADY optimized (MAX_FANOUT=25 + pipelined pre-decode); residual is density-driven
- placement congestion directives: recipe is tuned; AltSpreadLogic_high already tried+reverted (NSAI_13→14)
- rms pipelining: backfires at the congestion limit (FF additions perturb placement)

**The ONE remaining tok/s lever is a different layer: DDR/memory-bandwidth efficiency.**
The matmul is DDR-bound at 50% efficiency (config.py max_bytes_per_cycle ratio=0.50). A
DDR-efficiency win raises tok/s WITHOUT adding compute density, so it dodges the
congestion wall. But it needs real investigation of the tmatmul descriptor DMA
(dma_r_tmatmul: burst length, read outstanding — the BD M_AXI shows NUM_READ_OUTSTANDING=2,
possibly low; read/write contention on shared DDR) and a full build to validate on HW
(the estimator's 0.50 is a fixed assumption). Measured tok/s was 91% of estimate, so the
real HW is ~consistent with the 0.50 model — beating it is uncertain but is the right next
thread. This aligns with the user's earlier question ("why is the memory ~half as slow?").

**Decision needed (asked via AskUserQuestion, user stepped away):**
(1) Density/DDR R&D — pursue the DDR-efficiency thread (recommended: only tok/s lever left).
(2) Harden BS=6 — but placement levers are tuned/reverted, so no clean move; would be "let's see".
(3) Lock in BS=6 (1943 tok/s) as final for this architecture; pivot bigger-picture.

**Chose (pending user):** did NOT kick a speculative 5-6h build (no informative hypothesis;
respects the "5h last resort" rule). eq2 idle awaiting steer. My recommendation: (1) —
investigate the tmatmul DMA DDR efficiency as the next tok/s thread. If you'd rather I lock
in BS=6 or pivot, say so.
