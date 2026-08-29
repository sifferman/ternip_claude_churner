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

---
## 2026-08-14 ~9:15 AM — CONSOLIDATED to main worktree + BS=10 building

**Worktree cleanup DONE** (user request):
- Retired `nsai` + `nsai_bs6` worktrees. Only the main `ternip_claude` worktree remains.
- Main worktree now on **NumSeparateKernels** (churner 10ea911 -> ternary_matmul 20420ad -> ternip
  86ba9d8, all the fixes: state_q fanout + norm_mul + round2 + bug-1 guard). Builds now run from
  the main worktree (/soe/esifferm/GitHub/ternip_claude/ternary_matmul).
- Preserved everything: dot backend (committed a343643 on NumDdrBanks + files brought to
  NumSeparateKernels 46c5a63), benchmark timing-fix + hetero benchmark (5d18848), AutoBridge +
  QUESTIONS + MISSION_24H (dadaa78/f22687f). Nothing lost.

**MEASURED throughput (fixed-timer benchmark, all silicon-validated):**
- 24-lane (nk=4 BS=6): 1942 tok/s (baseline).
- asym8 (30-lane, BS=8 roomy): 2417 tok/s (+24.5%), 30 distinct texts validated.
- asym9 (33-lane, BS=9 roomy): 2641 tok/s (+36%), CLOSED +0.014.
- Model ~92% accurate (measured/model consistent across configs -> ~8% is systematic, likely DDR
  efficiency; model-improvement ideas logged).

**BS=10 building now** (from main worktree, eq2): ternip_big BS=10 x3 + ternip_small BS=6 x1 =
36 lanes, model ~3120 tok/s. RISK: occupancy ~68-70% roomy (est), over the ~65% ceiling -> may not
close. If it closes -> ~2850 measured (near/over 3k). If not -> asym9 (2641, +36%) is the deliverable.

PENDING (side item): the parallel-compile in benchmark_pynqvivado_hetero.py uses THREADS but the GIL
gives ~no speedup (measured 347s). Should switch to ProcessPoolExecutor for real parallelism -- not
done yet (awaiting your threads-vs-processes preference; I lean per-distinct-config processes).

AUTONOMOUS PLAN (you're away): when BS=10 finishes -> stage xclbin FIRST, read WNS, benchmark +
silicon-validate if it closes, publish release. If it doesn't close, asym9 stands. Keep eq2 busy.

## 2026-08-28 — overnight tok/s plan (user away until 2026-08-29)

Measured silicon baseline: 370M **3711.77** tok/s (BS 14x3+6, WNS +0.024),
1.3B **835.50** (BS 10x3+5, WNS +0.001), 2.7B **411.35** (BS 8x3+5, WNS +0.001).

### Correction: the silu PISO is fresh margin for ALL THREE models

ternip main has three `pipelined_interconnect` PISO stages. Two of them
(`ternip_sig_parallelized`, `ternip_csig_parallelized`) were already present at
`f4666f7` -- that is the 0.468 ns already spent to close 1.3B. The third,
`ternip_silu_parallelized`, is added ONLY by `75fa40c` (merged today) and is in
**no validated build for any model**. Its own comment names the case it targets:

> the `&silu_in_ready` reduction feeds straight back into the PISO's yumi, which
> is the dominant critical path in `rowwise_operation` at **D=2560**.

D=2560 is 2.7B. So the model most likely to gain is the one with the least margin.

### Where the tok/s actually is (report_instruction_timing projections)

| lever | 370M | 1.3B | 2.7B |
|---|---:|---:|---:|
| NumVectorRegisters 4->8 | **+5.3%** | +1.4% | +0.9% |
| BatchSize +1 (3 more lanes) | **+5.9%** | see below | see below |

NVR=8 is worth it only on 370M -- at D=2048/2560 the matmul dominates and swap
elimination barely moves the needle, so spending 43 ps of margin (its measured
cost, from build `2026.08.22-0908-370M-NVR8`) to buy ~1% is a bad trade on the two
models that have +0.001 and nothing to spare.

**BatchSize is the lever for the big two**, and it is gated purely on margin:
1.3B 10->11 takes lanes 35->38, 2.7B 8->9 takes 29->32. Both are ~+9-10%, far
larger than anything else available. Neither is affordable at +0.001 -- unless the
silu PISO pays out.

### Overnight queue (chosen without waiting for the user)

- **A (running, `2026.08.28-1203`)** 370M NS=1 BS=14x3+6, first build with silu
  PISO. Started 12:03 PM, ETA ~5:00 PM. Measures what the silu PISO is worth at
  D=1024 and whether NS=1 is viable. No tok/s gain by itself (BS unchanged).
- **B** 2.7B BS=9x3+5 + silu PISO. Largest single prize (~+10%) and the case the
  patch was written for, so best odds of the margin appearing.
- **C** 1.3B BS=11x3+5 + silu PISO (~+9%).

If B or C misses timing, fall back to the same model at its current BS with silu
PISO alone, to book whatever margin the patch gives before spending it.

370M's own next step (BS=15 at +5.9%, or NVR=8 at +5.3%, or both) is queued behind
B and C because 370M is the healthiest model and the big two have larger untapped
gains.

### Exact BatchSize projections (2026-08-28, after the sweep finished)

Caveat found while reading the numbers: `report_instruction_timing`'s
`multicore tokens_per_second` is `singlecore x BatchSize_big x 3` -- it does **not**
count the `ternip_small` kernel. Proof: 1.3B singlecore 24.8281 x 30 = 744.84,
exactly the BS=10x3+5 row, and BS=11x3+**6** reports identically to BS=11x3+**5**.
So the tool's ratios (+9.5% / +12.0%) overstate; scaling the *measured* silicon
numbers by lane count is the honest estimate:

| model | now | BS+1 | lanes | projected | gain |
|---|---:|---:|---:|---:|---:|
| 2.7B | 411.35 | 9x3+5 | 29 -> 32 | ~454 | **+10.3%** |
| 1.3B | 835.50 | 11x3+5 | 35 -> 38 | ~907 | **+8.6%** |
| 370M | 3711.77 | 15x3+6 | 48 -> 51 | ~3944 | **+6.2%** |

This confirms the B-then-C ordering (2.7B first, then 1.3B, then 370M).

A second, cheaper lever falls out of the same finding: because the tool ignores
`ternip_small`, its BatchSize has never been swept. Raising small 5 -> 6 adds one
lane -- worth ~+2.9% on 1.3B and ~+3.4% on 2.7B for one CU's worth of area, which
is a quarter of what a big-kernel BS+1 costs. Worth trying as the fallback if the
big-kernel bump misses timing.

### Build A result — `2026.08.28-1203`, 370M NS=1 BS=14x3+6 + silu PISO

Finished 6:30 PM, 6h 28m. Kernel-scoped (`level0_i/.../ternip_ip_1`):
**WNS 0.000, TNS 0.000, 0 failing endpoints**, AUTO-FREQ-SCALING-04 count **0**,
so it holds 300 MHz. It closes — with exactly zero margin.

**NS=1 is net-negative for 370M and should not ship.** The shipping NS=8 build at
the same BS=14 closed at **+0.024**; this one closes at **0.000**. NS=1 removes
core-interconnect pipeline stages, so this is the expected direction; the
experiment's premise (NS=1 frees area for a higher BatchSize) does not pay for the
24 ps it costs, because BS=14 is unchanged here. Revert 370M to NS=8.

**Caveat — this build cannot price the silu PISO.** It changed two things at once
vs the shipping build (NS 8->1 AND the new silu PISO), so +0.024 -> 0.000 is the
*net* of the two. The PISO could be worth anywhere from ~0 to a lot, masked by
whatever NS=1 cost. My earlier claim that A would "measure what the silu PISO is
worth at D=1024" was wrong -- it is confounded. To price the PISO at 370M would
need NS=8 + PISO, which is also exactly the build worth having, since it is the
shipping config plus one improvement.

Zero margin also rules out stacking anything on this configuration: NVR=8 costs
43 ps and BS=15 costs more, so neither fits on top of NS=1.

### Build B (running) — `2026.08.28-1844`, 2.7B BS=9x3+5 + silu PISO

Kicked 6:44 PM, ETA ~1:00 AM. 2.7B already used NS=1 natively, so this build is a
clean single-variable change from its validated +0.001 baseline: BatchSize 8 -> 9,
plus the silu PISO the baseline lacked. Projected ~454 tok/s (+10.3%).
Unlike A, this one IS interpretable: if it closes, both the PISO and the extra
lanes are paid for; if it misses, the fallback is 2.7B at BS=8 with the PISO alone
to book the margin, or small-kernel 5 -> 6 (+3.4%) as the cheaper lane.

### Revised queue

- **C**: 1.3B BS=11x3+5 + silu PISO (~+8.6%, ~907 tok/s)
- **D**: 370M NS=8 (reverted) + silu PISO at BS=14 -- prices the PISO cleanly and
  is the shipping config plus one improvement; if it shows margin >= 43 ps, NVR=8
  or BS=15 goes on top next.

## 2026-08-28 evening — `test_rms_norm_batch` FAILS for D>=2048 (pre-existing)

Running the cocotb gate against the real board targets (not just `d512_1core`)
turned up a correctness failure that the old CI matrix could never have seen:

| model | D | `test_rms_norm_batch` |
|---|---:|---|
| 370M | 1024 | **PASS** -- exact, `max abs(HW-emu) = 0.0000` on every lane |
| 1.3B | 2048 | **FAIL** -- `max abs(HW-emu) ~ 1.7` |
| 2.7B | 2560 | **FAIL** -- `max abs(HW-emu) ~ 1.7-2.6` |

**Not caused by the BatchSize bump.** 2.7B fails identically at its validated
BS=8 and at the new BS=9, so this is pre-existing and equally true of the shipping
2.7B bitstream. Build B was left running for that reason -- it does not make
anything worse.

**Not the known b>0 bug either**, despite the test's `*** FAIL (b>0 rms bug) ***`
label: **lane b=0 fails too**, so the message is misleading here and the label
should probably be reworded. The failure is on every lane, not the b>0 subset.

**Which side is wrong: most likely the RTL.** The emulator is independently
validated against the MMfreeLM reference by CI's `test_emulator`, which passes
144/144 for 1.3B with its real model. So the emulator agrees with the reference
while the RTL disagrees with the emulator. Also telling: at D=1024 the emulator
reproduces the RTL *bit-exactly* (0.0000), so the model is a faithful model of
this datapath -- it only diverges once D crosses 2048.

**This is very likely the long-standing D=2048/2560 FPGA<->emulator divergence**,
which until now was only observed on silicon and described as "starts at layer 0".
It now reproduces in a ~6 minute RTL sim, isolated to a single FU, with a
per-lane numeric diff -- a far better debugging position than a full-model run.

Direction of the error: HW output is consistently *smaller* than the emulator
(1.3B lane 0: HW `[0.031 -0.438 0.25]` vs emu `[0.094 -1.031 0.531]`), i.e. the
hardware's rms divisor is too large. What I checked and did NOT find a smoking gun
in: `RmsAccumulatorWidth = 2*P + clog2(D) + 1` does scale with D (43/44/45 bits)
and is wide enough for D squares; `rms_sqa_sum_t` is 32-bit and the squares cannot
saturate it. The remaining suspects are the `ternip_div` derived widths, which are
where D actually enters (`ALeftShiftAmount = InBPrecision+2`, so
`DivInternalPrecision` = 62 at D=1024 but 64 at D=2048/2560, crossing the
even/odd `DIV_BSG` adjustment), and `RawDivideLatencyUpperBound =
DivInternalPrecision + 16` feeding the fixed-latency equalizer.

**This is a correctness question that outranks tok/s**, so it is flagged here
rather than acted on unilaterally: the two large models' throughput numbers are
real, but they are throughput on a datapath that disagrees with the reference
model. Awaiting the user's call on whether to divert to the bug.

## 2026-08-28 — silicon answer: yes it generates text, but 2.7B is visibly damaged

Ran `generate_pynqvivado.py` on the U250 (gpu01) for all three models, each with its
md5-verified bitstream and `loaded.xclbin` deleted first. Same prompt, greedy, 24 tokens:

| model | D | rms test | greedy continuation |
|---|---:|---|---|
| 370M | 1024 | **PASS** (exact) | "...a remote, 20-year-old forest in the Andes of Brazil. They were able to find their way through the j" |
| 1.3B | 2048 | FAIL (~1.7) | "...a remote, 1970s-era California ranch. The herd were all female and all had been born with their mother" |
| 2.7B | 2560 | FAIL (~2.6) | "...a remote, 300-year-old **the world in the the the the the the the the the the the the the the**" |

**370M and 1.3B are coherent. 2.7B degenerates into `the the the the`** -- the classic
signature of a numerically damaged model. Its sampled output is also weak
("16th century village. in the UK. The unicorns were the only animals in the village in the").

The decisive point: **2.7B is the LARGER model and should produce BETTER text than
1.3B. It produces markedly worse.** And the quality ordering tracks the rms error
magnitude exactly -- 370M exact/best, 1.3B moderate error/coherent, 2.7B largest
error/degenerate. That is a real defect materially hurting the flagship model, not a
tolerance artifact in the testbench.

Caveat: 24 tokens is a short sample. But greedy decoding is deterministic, and a 2.7B
collapsing on the standard unicorn prompt while a 1.3B handles it cleanly is not the
kind of thing that happens by chance.

**Revised priority.** The 2.7B BatchSize work (build B, +10% tok/s) is optimizing the
throughput of a model whose output is degenerate. Fixing the D>=2048 rms defect is
worth more than any remaining tok/s lever on that model, and it likely also lifts 1.3B
quality. Recommend: let build B finish (it is 5h in and costs nothing extra), then
divert to the rms bug rather than kicking 1.3B BS=11.

Runtime state left on gpu01: `~/ternip_run/kernel.xclbin` currently holds the **370M**
BS=14 bitstream (`1483936d...`); `kernel_bs10.xclbin` = 1.3B, `kernel_2_7B.xclbin` = 2.7B.

## 2026-08-28 late — RETRACTION: there is no rms bug

**The D>=2048 rms failure I reported was my own testbench error, not an RTL defect.**
Retracting it in full.

Instrumenting `ternip_rms`'s divider inputs gave it away immediately. Running the
1.3B target printed:

```
RMSDBG D=1024 len=2048 accw=43     <-- hardware elaborated for D=1024, software sending 2048
```

`Cfg.D` was **1024** while `rms_length` was **2048**: the 1.3B software was driving a
**370M-elaborated design**. cocotb's `SIM_BUILD` defaults to a fixed `sim_build`
directory that is NOT keyed by `TARGET`, and I ran 370M first without clearing it,
so every subsequent target silently reused the 370M hardware.

With a clean build directory, all three pass bit-exactly:

| model | D | `test_rms_norm_batch` |
|---|---:|---|
| 370M | 1024 | PASS, `max abs(HW-emu) = 0.0000` |
| 1.3B | 2048 | **PASS, 0.0000 every lane** |
| 2.7B | 2560 | **PASS, 0.0000 every lane** |

`RMSDBG D=2048 len=2048 accw=44` and `D=2560 len=2560 accw=45` confirm the widths
scale correctly, exactly as the localparams said they would.

### The real bug, and the fix

The genuine defect was in the harness: **switching `TARGET` silently ran the previous
target's elaborated design.** That is worse than a false alarm -- it can also hide a
real regression, by passing a broken design against a stale good build.

Fixed on `larger-model-fixes` (`99793b9`): `SIM_BUILD` is now keyed to a hash of the
resolved `TERNIP_CFG` + defines, so each config gets its own directory
(370M `303d0d68a71d`, 1.3B `cacd2c5c68c5`, 2.7B `2751f9ce0453`). Regression-tested by
running 370M then 1.3B back-to-back with no manual clean: both PASS, which is exactly
the sequence that produced the false failure before.

This mattered more than usual because the CI change earlier today made "run the gate
against several real targets in one session" the normal workflow -- previously the
matrix used one config per job, so the bug was latent.

### What this does NOT explain

The 2.7B silicon generation is still degenerate ("the the the the" under greedy),
and that observation stands -- it came from real hardware, not from this testbench.
It is now unexplained. It may simply be model/prompt behaviour at 24 tokens rather
than a hardware fault; the correct next step is a longer sample and a comparison
against the CPU reference for the same prompt, NOT an RTL hunt.

## 2026-08-28 — 2.7B "the the the" is EXPECTED, not a hardware fault

Ran the same prompt through all three sources for 2.7B. Greedy, 24 tokens:

| source | output |
|---|---|
| float reference (`generate_official`) | "...a remote, icy wasteland. The unicorns **were the size of** a horse and **were the size of** a large dog" |
| **hardware-accurate emulator** | "...a remote, 3000 years ago. **the the the the the the the the the the the the the the the the the**" |
| **silicon (AU250)** | "...a remote, 300-year-old the world in **the the the the the the the the the the the the the the**" |

**The hardware-accurate emulator degenerates into `the the the the` exactly like the
silicon does.** The hardware is faithfully reproducing what the fixed-point model is
specified to produce. There is no hardware fault.

Two conclusions:

1. **My "2.7B is visibly damaged" claim was wrong**, and the reasoning behind it was
   wrong twice over: I compared 2.7B's silicon output against the *1.3B* model rather
   than against 2.7B's own reference, and I never checked what the fixed-point spec
   predicts. The float reference shows MMfreeLM-2.7B already repeats heavily at full
   precision ("were the size of ... were the size of"), and the emulator shows the
   16-bit/exponent-5 quantization tips that tendency into full degeneration.

2. **The real limitation is quantization, not RTL.** At D=2560 the current fixed-point
   format is not precise enough to keep the model coherent. If 2.7B output quality
   matters, the lever is numeric format (FixedPointPrecision / exponent), which is a
   design change with real area cost -- not a bug hunt. Everything in the RTL matches
   its spec bit-exactly (`test_rms_norm_batch` 0.0000 on all three models).

Net for the day: no correctness defect exists in the RTL. The one genuine bug found
was in the verification harness (cocotb `sim_build` not keyed by target, fixed in
`99793b9`). The tok/s queue stands unchanged.

## 2026-08-29 — FixedPointExponent is a FREE fix for 2.7B degeneration

The 2.7B "the the the" collapse is a **quantization-format** problem, and the format
knob that fixes it costs nothing.

**Why it is free.** Every width and LUT size in `ternip_types.sv` derives from
`FixedPointPrecision`. `FixedPointExponent` appears in exactly two places --
`RmsSqaSumExponent = 2 * Cfg.FixedPointExponent` and the `FixedPointOne` constant --
and neither is a width. Changing it reinterprets the same 16 bits: no wider adders,
no bigger LUTs, no extra BRAM, no cycle-count change (so tok/s is unaffected). This is
categorically different from raising `FixedPointPrecision`, which widens the whole
datapath and would wreck MaxCores timing.

At the current -5 the format spends 11 integer bits on a +/-1024 range that LLM
activations never approach, while resolution starves at 0.031.

| exponent | resolution | range | 2.7B greedy (hardware-accurate emulator) |
|---|---:|---:|---|
| **-5 (current)** | 0.031 | +/-1024 | "3000 years ago. **the the the the the the ...**" DEGENERATE |
| -6 | 0.016 | +/-512 | "**icy** region of the Arctic. The unicorns were the size of the average horse and were the size of" |
| **-7** | 0.0078 | +/-256 | "**icy wasteland.** The unicorns were the only creatures in the world that were able to survive the harsh" |
| -8 | 0.0039 | +/-128 | "**icy wasteland** of the Arctic Circle." |
| *float reference* | - | - | "*icy wasteland.* The unicorns were the size of a horse and were the size of a large dog" |

-7 and -8 both reproduce the float reference's "icy wasteland" exactly. **-7 is the
recommended setting**: it gives 4x the resolution of today while keeping +/-256 of
headroom, and produced the longest coherent continuation.

**`test_emulator` cannot see this.** It passes 192/192 at BOTH -5 and -7, so the CI
numeric gate is blind to the difference -- it checks per-instruction agreement, not
end-to-end generation quality. Anything we conclude about quantization quality has to
come from `generate_emulator`, not `test_emulator`. Worth considering a CI check that
generates text and flags degenerate repetition.

**Caveats before this ships.**
- `FixedPointExponent` is NOT on CLAUDE.md's allowed-to-modify list -> needs user
  sign-off.
- It changes `TERNIP_CFG`, so all three validated xclbins are invalidated and need
  rebuilds to benefit.
- Saturation risk is the real failure mode at smaller ranges; 1.3B and 370M are being
  checked at -7/-8 for regressions before recommending a global change.
- Timing should be re-verified: constants change, so logic differs slightly even
  though widths do not.
