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

## ⛔ GATE VERDICT (2026-07-24): DO NOT PROCEED — overlap is dependency-locked
Investigation (3 agents + model measurement) is CONCLUSIVE:
- The 53.6% matmul-serial is TRUE data dependency (transformer recurrent critical chain:
  matmul → post-proc → next matmul; layer N → N+1), NOT a scheduler artifact.
- 24.3% of runtime is non-matmul-ONLY (no matmul in flight to hide under) — untouchable.
- The +25% (max_parallelism 1.25x) is DEPENDENCY-BLIND — an over-optimistic bound.
- The RTL ALREADY overlaps one op/GO; measured 1943 tok/s = 91% of the overlap-assuming
  model (2130). The HW already captures the main available overlap.
- EMPIRICAL TEST: aggressive ldv-prefetch scheduler tweak → cycle_counter 3,379,223 →
  3,379,616 (0.01% WORSE). The one capturable slice Agent 3 named captures nothing.
**Conclusion: the FU-overlap refactor's realistic payoff is ~0, not +25%. nk=4 BS=6
(1943 tok/s) is near-optimal for this architecture + model. Recommend LOCK IN.**
The matmul (76%, DRAM-bound at the TP=128 crossover) is the real bottleneck; speeding it
needs TP↑ (congestion-blocked at nk=4) AND DDR-efficiency↑ together — the actual ceiling.

## Plan (phased; each phase gated by sim before Vivado) — SUPERSEDED by gate verdict above
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

## P0 investigation findings (2026-07-24)

### Core dispatch (ternip_core.sv) — the blocker is SURGICAL
- Instruction accept = conjunctive AND of ALL FUs' `in_ready` + `!stall` (lines 393-397).
  Any one busy FU blocks issue of EVERY instruction → in-order single-issue.
- Dispatch is a stateless combinational demux on `instruction_i.fu` (lines 486-517); no
  central "current FU" register. "Busy" = each FU's `in_ready_o` low.
- Vreg port = fixed-PRIORITY MUX (loadstore>rms>rowwise>tmatmul, lines 399-437), NOT a real
  arbiter; a sim assertion `$fatal`s if >1 FU drives it (lines 444-459). Relies on
  single-FU-active invariant.
- **KEY: during tmatmul_go the vreg port is FREE** (GO uses DDR + internal importvector/
  exportvector BRAMs, `vector_request_valid_o=0`), and tmatmul HOLDS `in_ready=1` during GO
  (line 475, via 1-entry queue). So ONE non-matmul op already overlaps a GO today (= the
  model's 29.2%). Nothing structural serializes it except the conjunctive gate.
- **FIX = per-target issue gate** (gate `instruction_ready_o` on only the selected FU's
  `in_ready`) + **turn the vreg mux into a real arbiter with backpressure** (stall loser,
  drop the one-driver assertion). This extends overlap from 1 op to a chain of independent
  non-matmul ops during each GO.

### vector_registers (ternip_vector_registers.sv + ternip_pipelined_mem.sv)
- Single shared R/W request port (1RW). Backed by INFERRED single-port RAM (`MEM[]`,
  one addr/cycle) — so the U250 BRAM's 2nd port is IDLE/free.
- 2nd read port = RTL-only (~40-60 lines pipelined_mem, ~15-20 vector_registers, ~30-60 core),
  negligible BRAM cost. Hazard: true-dual-port read-during-write collision → keep an
  interlock at the core-arbiter level (safest), or add write-forwarding.
- **2nd read port is OPTIONAL** — the arbiter-with-backpressure can serialize port access
  cycle-by-cycle; the 2nd port only helps if both concurrent FUs are read-bound every cycle.

### Refactor scope (revised, smaller than feared)
1. Per-target issue gate (ternip_core ~lines 393-397) — the main unblock, few lines.
2. Vreg port real arbiter + backpressure + relax assertion (ternip_core ~399-459).
3. (Optional) 2nd vreg read port if port contention limits measured overlap.
GATE still pending: agent 3 — is the overlap dependency-locked or schedulable?

## Log
- 2026-07-24: branches FuOverlap created off validated state. 3 investigation agents launched.
  Agents 1 (core dispatch) + 2 (vreg ports) done — findings above; blocker is the conjunctive
  issue gate, fix is surgical. Awaiting agent 3 (dependency-vs-schedulability gate).
