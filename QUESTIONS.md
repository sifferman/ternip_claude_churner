# QUESTIONS.md

Open questions / judgement calls Claude wants the user to review out of
band. Newest at the top. Each entry: what was decided, the alternative
considered, and how to redirect if the user wants something different.

**Auto-cleanup**: when Claude can confidently answer a question itself
(or the user answers it elsewhere), the entry is removed from this list.

---

_(no open questions)_

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

---

## 2026-06-04 — Column-slice BS=1 not closing on AU250 platform

`NumTmatmulBanksPerCore_2..7` all failed with `VPL 18-1000 Routing
results verification failed due to partially-conflicted nets`, despite
trying:

- `_2`: SSI_SpreadLogic_high + NumDdr-ported MAX_FANOUT pass
- `_3`: SSI_SpreadLogic_high + minimal vivado_common (no MAX_FANOUT)
- `_4`: KILLED — AltSpreadLogic_high alone (no pblock)
- `_5`: KILLED — Makefile bug (kernel.cfg cached from `_4`'s kick)
- `_6`: KILLED — pblock TCL bracket-escape silently matched 0 cells
- `_7`: pblock v2 matched 9732 cells (2433/bank) → still failed in
  XRT-side H2C async clock-crossing FIFOs

SLR1 CLB densities through the series: `_2` 68.68% → `_3` ~same → `_6`
68.06% → `_7` 64.31% (with proper tmatmul_dma pblock applied).
Tmatmul_dma is only ~1/4 of the kernel — the bulk
(`tmatmul_units[u]/MOA/MAC`, vector_registers, loadstore DMA, central
interconnect) is in SLR1's central ternip_core.

### `_8` plan
Extending the pblock to also pin `tmatmul_buffers[b]` (per-bank
pipelined buffers in ternip_buffered) to SLR `b`. Won't pin
`tmatmul_units[b]` because they share vector_register / instruction-
stream connections that would need RTL pipeline insertion to tolerate
cross-SLR routes.

### If `_8` also fails — decision needed
Per CLAUDE.md "3+ iterations on the same path = wrong layer," the
options are:

1. **Reduce TmatmulParallelism 256 → 128** — halves per-tmatmul-unit
   MAC array area, halves the central column-slice compute footprint.
   Throughput regression at BS=1 (~half tokens/sec) but unlocks
   closure. **Outside the current MaxCores "allowed to modify"
   list** — needs your approval.

2. **Extend pblock to `tmatmul_units[b]`** — spread MOAs to SLRs too.
   Requires RTL pipeline insertion on shared vector_request /
   vector_read_data signals (currently combinational
   one-hop). Big change.

3. **Pivot back to NumDdrBanksPerTmatmul** — build_56 closed at BS=20
   with 69% SLL on that branch. Column-slice's BS-scaling advantage
   is unrealized while we can't close BS=1. The branch is preserved
   (`sifferman/ternary_matmul_claude` `NumDdrBanksPerTmatmul`
   `7b36013` etc.).

My recommendation if `_8` fails: revert place_design back to
SSI_SpreadLogic_high (which is hyper-tuned for SSI density issues)
AND try (1). Reducing TmatmulParallelism is the cleanest
architectural concession that doesn't lose column-slice's BS-scaling
advantage; the throughput loss at BS=1 is recovered the moment BS
ramps above ~2.

---

## 2026-06-04 21:55 PM — Pblock-only experiments exhausted

`_9` (range pblock) failed VPL 18-1000 in `ip_cc_axi_data_h2c_00`
async FIFO. Updated pblock results table:

| | tmatmul_dma | tmatmul_buffers | SLR1 CLB% | Failure |
|---|---|---|---:|---|
| `_7` | per-SLR | none | 64.3 | VPL 18-1000 XRT h2c_00 + h2c_01 |
| `_8` | per-SLR | per-SLR | 35.2 | VPL 35-3303 routing congestion |
| `_9` | range (lower/upper) | range | 57.2 | VPL 18-1000 XRT h2c_00 only |

**No pblock variant reaches a closing zone.** The kernel
(MaxCores N=4 BS=1 with TmatmulParallelism=256) is structurally
too dense for the AU250 platform's routing budget when 4 m_axi
ports are added. Per CLAUDE.md "3+ iterations on the same layer
= wrong layer" — we're at 5. **eq2 is intentionally idle pending
user direction.**

**Decision needed**:

1. **TmatmulParallelism 256 → 128**: cleanest single-knob fix. Halves
   per-tmatmul-unit MAC array area (4 units × half size each = same
   total compute as 2 full units, just spread differently). Throughput
   model needs update; from `report_instruction_timing.py` rough math,
   tokens/sec at BS=1 would roughly halve (~50 vs ~101), but the
   bottleneck moves OFF placement and BS scaling becomes viable.
   **CLAUDE.md says this is outside the MaxCores allowed-to-modify
   list — needs explicit user approval.**

2. **RTL pipeline insertion on cross-SLR signals**: surgical RTL
   change to insert FIFO/register stages on the gearbox→tmatmul
   R-data path. Makes cross-SLR routing feasible at full
   TmatmulParallelism=256. Multi-day implementation + verification
   effort.

3. **Pivot back to NumDdrBanksPerTmatmul**: build_56 (the last
   commit there) was at BS=20. Per CLAUDE.md memory, BS=6 already
   trips VPL 18-1000 on NumDdr — so build_56 may not have actually
   closed cleanly either. This pivot has the lowest blast radius
   *if* a known-good NumDdr build exists at lower BS.

My recommendation: **Option 1 (TP=128)**, but explicitly waiting
for user OK because of the allowed-to-modify list.
