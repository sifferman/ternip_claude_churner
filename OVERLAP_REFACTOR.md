# FU-Overlap Refactor (branch: FuOverlap)

**Goal:** overlap the matmul (`tmatmul_go`, 76% of cycles) with non-matmul FU work to
capture up to the model's **+25% ceiling** (1943 → ~2400 tok/s), using the FPGA's free
resources (BRAM ~95% free, LUT ~66% free) — a *local*-logic lever that avoids the
DDR-crossing routing congestion that blocks BS=8/VP=8.

**Baseline (do not regress):** nk=4 BS=6, release `2026.07.11-2126`, WNS +0.002,
silicon-validated (all 4 CUs pass, coherent text), **1943 tok/s**. This is the fallback.

## Why this lever (vs. everything else, all exhausted)
- BS=8 / VP=8: routing-congestion-blocked (−0.430 / −0.802) despite 65–95% free area.
- TP: at the memory-bound crossover; reducing craters tok/s, raising doesn't help (DDR-bound).
- DDR efficiency alone: no gain at the TP=128 crossover.
- Placement/fanout: recipe already tuned (CELL_BLOAT, MAX_FANOUT, pblocks deployed).
- nk: fixed at 4 (hard rule).
→ Overlap is the only remaining path that converts free resources into throughput.

## Core hypothesis (BEING VERIFIED by investigation agents)
The core issues instructions in-order and the FUs share one `ternip_vector_registers`
port, so non-matmul ops can't run during `tmatmul_go` even though the vreg port is FREE
during GO (tmatmul_go uses DDR + internal BRAMs). IF the 53.6% matmul-only-serial is
schedulability-limited (not true-data-dependency-limited), then concurrent FU issue +
a 2nd vreg read port can capture the overlap.

**GATING QUESTION:** is the overlap dependency-locked or schedulable? (agent 3 answering)
If dependency-locked → abandon, lock in 1943 tok/s. If schedulable → proceed.

## Plan (phased; each phase gated by sim before Vivado)
- [ ] **P0 Investigation** (in progress): map core dispatch, vreg ports, overlap ceiling.
- [ ] **P1 Design**: concurrent-FU-issue scheme + 2nd vreg read port; sim harness to
      MEASURE achieved overlap (cycle_counter drop) before any Vivado build.
- [ ] **P2 Implement multi-port vector_registers** (2nd read port on the free BRAM 2nd port).
- [ ] **P3 Implement concurrent FU issue** in ternip_core (issue non-matmul while tmatmul_go streams).
- [ ] **P4 Sim-validate**: rms_tb + tmatmul_tb + cocotb + test_emulator + measure cycle_counter.
- [ ] **P5 Vivado**: OOC first (does it close + how much overlap), then pynqvivado_au250.
- [ ] **P6 Silicon-validate on fulladd** (basic + benchmark + demo) — must stay correct.

## Rules for this effort
- Never regress the validated 1943 tok/s deliverable (it's tagged + safe on NumSeparateKernels).
- Sim/model-measure the overlap gain BEFORE each expensive Vivado build (the refactor's
  payoff is uncertain; don't burn 5-6h builds on unvalidated overlap).
- Any FF-heavy addition risks the congestion limit — keep the new logic local + lean.

## Log
- 2026-07-24: branches FuOverlap created (ternip/ternary_matmul/churner) off validated state.
  3 investigation agents launched (core dispatch, vreg ports, dependency-vs-schedulability).
