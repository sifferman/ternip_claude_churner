# Matmul/DDR Frontier (branch: MatmulDdrFrontier)

**The prize (model, confirmed by sweep):** the matmul is 76% of cycles. Speeding it needs
BOTH higher TmatmulParallelism AND better DDR efficiency — TOGETHER = **+49% (2130 → 3185
tok/s)**. Neither alone helps (they're balanced at the TP=128 knee).

| | DDR eff 0.50 | DDR eff 0.90 |
|---|---:|---:|
| TP=128 (current) | 2130 | 2169 (+1.8%) |
| TP=256 | 2130 (no gain) | **3185 (+49%)** |

Baseline to never regress: nk=4 BS=6, 1943 tok/s (release 2026.07.11-2126, silicon-validated).

## Why this is the real bottleneck
Each `tmatmul_go` = `max(compute = D*D/TP = 8192, memory = matrix_bytes / (bw·eff) = 8192)`
at TP=128 / eff=0.50 — exactly balanced (the knee). tok/s independent of TP for TP≥128
(memory-side binds); TP=64 craters (compute binds). To drop the matmul below 8192 cycles
you must lower BOTH terms: TP=256 → compute=4096, AND eff→0.9 → memory≈4551. Then
matmul≈4551 (~1.8× faster) → +49%.

## Two coupled sub-problems (both required)
### A. TP=256 must ROUTE (currently congestion-blocked)
- TP was cut 256→128 to relieve ip_cc_axi_data_h2c / ULP routing congestion (NSK_1 TP=256
  didn't route at nk=4). But the chip is 65–95% FREE (DSP 88% free) — the block is LOCAL
  routing, not area. Same class of problem as BS=8. Need a congestion strategy (floorplan,
  spreading, density trades) to land TP=256 using the free DSP.

### B. Real DDR efficiency must reach ~0.9 (currently unknown, assumed 0.50)
- The 0.50 is a MODEL assumption. At TP=128 the matmul is ~8192 cyc whether eff is 0.50
  (memory-bound) or 0.90 (compute-bound) — so silicon at TP=128 CANNOT tell us the real eff.
- Must (1) determine the REAL weight-streaming efficiency (DMA burst/outstanding/contention
  on the single bank), and (2) improve it if it's <~0.9 (bigger bursts, more outstanding,
  reduce tmatmul-read vs result-write contention on the shared bank).

## Plan
- [ ] **P0 Investigate** (in progress): (A) TP=256 congestion root cause + free-DSP feasibility;
      (B) real DDR weight-streaming efficiency + fixability; (C) is the real matmul compute-
      or memory-bound on silicon?
- [ ] **P1**: attack whichever sub-problem is tractable first; both must land for the +49%.
- [ ] Sim/model-measure before every 5-6h Vivado build. Never regress the 1943 tok/s deliverable.

## P0 finding — matmul is COMPUTE-bound at TP=128 (agent 3, cycle-accurate)
- tmatmul consumes 128 ternary weights/cycle = a 256-bit beat (ddr_r_data_i is 128 lanes,
  ternip_tmatmul.sv:93); a 512-bit DDR beat carries 256 weights → **2 tmatmul cyc per DDR
  beat**, DMA has **2× idle headroom**. GO = 8192 cyc (DdrReadsPerMatrix=D*D/TP, line 179),
  gated by the MAC array, NOT the DMA. The model's memory==compute==8192 is a coincidence of
  the ×0.5 constant, not a real memory bound.
- **TP=256**: RowParallelism still 1, DdrReadsPerMatrix=4096, tmatmul beat = 512b = DdrDataWidth
  (clean 1:1, no downsize). Compute → 4096 cyc. NOW needs the DMA to sustain ~1 beat/cycle
  (512 b/cyc = full bank BW). At real eff 80% → matmul≈5120 (~+40%); ~100% → 4096 (+49%).
- **Reframed plan: TP=256 is the PRIMARY lever (halves compute, largely decoupled).** DDR/DMA
  sustained beat-rate is the SECONDARY gate at TP=256 (even 80% is a big win, and the DMA is
  idle half the time today so headroom exists). Frontier ≈ "can TP=256 route?"

## P0 finding — TP=256 routing (agent 1)
- TP=256 = single monolithic 256-wide MAC array → 256:1 adder cone (RowParallelism stays 1
  at TP<D); doubles MAC lanes, accumulator fanin (8-stage tree), feeder buses (512b DDR beat,
  4096b importvector). Densest possible shape; local routing congestion around the convergence.
- **Free DSPs are USELESS** — ternary multiply is a LUT mux (ternip_tmatmul.sv:116-123), accum
  is fabric adds. TP=256 is LUT+routing-bound (explains DSP 88% free). My "use free DSP" was wrong.
- Route odds ~30-45% at nk=4 without relaxing BatchSize or the SLR1 CU. Best approach: per-SLR
  pblock giving each CU's tmatmul cone an oversized region + evict loadstore/rms/rowwise to
  adjacent columns (move the free LUTs next to the cone). Accumulator is already fully pipelined.
- IDEA (mine): restructure TP=256 accumulator as 2×128 partial cones summed at the end → two
  smaller convergences, more routable than one 256 cone. Speculative RTL.

## CRUX (decides if frontier is alive): can the weight DMA sustain ~1 beat/cycle?
Agent 3: real matmul is compute-bound at TP=128 (DMA idle half the time). Agent 1: TP=256 is
"zero throughput" — but only under the model's eff=0.50. If the real DMA can feed TP=256 at
~1 beat/cycle (512b/cyc ≈ full bank BW), TP=256 = +40-49%. If 0.50 is the real DMA ceiling,
TP=256 is futile. **Agent 2 (DMA burst/outstanding/contention) is the decider.**

## P0 finding — DMA (agent 2): frontier is a qualified GO
- DMA issues 1 descriptor/GO: contiguous 256KB, ~64 back-to-back 4KB page bursts (arlen=63,
  capped by AXI4 4KB guard, not AXI_MAX_BURST_LEN=256). Efficient sequencing, not row-thrash.
- The "50%" = DEMAND rate (TP=128 asks 256b/cyc = half peak), NOT lost bandwidth. DMA idle ½ time.
- Real exposures at TP=256 (which demands 512b/cyc = 100% peak): (1) NUM_READ_OUTSTANDING=2
  (bd.tcl:43,55) — raise to 8-16 to hide AR→R latency; (2) read/write contention — 3 masters
  (host, loadstore R+W, tmatmul R) merge 3→1 onto one bank/CU (bd.tcl:94-138); loadstore
  activation traffic steals read bandwidth during a matmul.
- MODEL can't predict the TP=256 gain (hardcoded 0.50) → must build + measure on silicon.

## ATTACK PLAN (frontier is a real gamble: TP=256 route ~30-45% × DMA-feeds-it)
Three must land together for +40-49%:
1. TP=256 (halve compute) — the hard routing fight (monolithic LUT cone). Enabler ideas:
   accumulator as 2×128 partial cones; per-SLR pblock giving the cone room.
2. NUM_READ_OUTSTANDING 2→16 (bd.tcl) — feed the array at 1 beat/cycle. Cheap.
3. Contention mgmt (loadstore vs weight reads on shared bank) — harder; defer, measure first.
Phases: (P1) TP=256 + outstanding=16, functional gates (test_emulator MUST pass — TP changes
matmul tiling). (P2) OOC make vivado route probe (~3h). (P3) full pynqvivado_au250 if OOC clean.
(P4) SILICON measure on fulladd (only silicon reveals the real TP=256 DDR efficiency / tok/s).
Density note (agent 1): TP=256 competes with BatchSize for density — can't have both; TP=256
(+40-49%) > BS=8 (+30%) IF it lands.

## Log
- 2026-07-24: branch MatmulDdrFrontier off validated state. Sweep confirms +49% prize
  (TP=256 + eff 0.9). Agent 3: matmul is COMPUTE-bound at TP=128 → TP=256 is the primary
  lever. Awaiting agent 1 (TP=256 routing feasibility) + agent 2 (real DMA beat-rate).

## Idea B: 2nd vreg read port (non-matmul reduction, density-safe, 300 MHz)
Measured the non-matmul 24%: rowwise dominates (838K cyc), and binary ops (mul 559K, add 111K,
sub 19K = 689K, all 3N) are 3N because they serialize 2 operand reads + 1 write through the SINGLE
vreg port (confirmed ternip_rowwise_operation.sv:268-320). NOT VP-bound, NOT compute-bound -> a 2nd
BRAM read port (idle/free on U250, inferred single-port today) makes binary ops 3N->2N.
- Model gain: 2130.9 -> 2185.0 = **+2.5%** (1943 -> ~1992 tok/s). Modest because matmul overlap
  hides most binary-op work; only non-overlapped (24% window) counts.
- Density-CHEAP (free 2nd BRAM port), stays 300 MHz -> avoids the H2C wall entirely (unlike VP=8).
- Hazard: read-during-write (in-place binary op) needs interlock/forwarding — being handled.
- Implementing: 2nd read port dedicated to rowwise_operation; port A unchanged for all other FUs.
- BUILT (2026.08.02-1126): CLOSES +0.022 @ 300MHz, silicon throughput 1998 tok/s (+2.9%, matches
  model), basic test all 4 CUs PASS. BUT demo = GARBAGE (reproducible) -> FUNCTIONALLY BROKEN.
  ROOT: the MUL/DIV path used a fragile "pair-by-order collapsing holding register" scheme that
  desyncs across the full model's 720 back-to-back MULs -> operand mispairing -> garbage. ADD/SUB
  (robust cycle-aligned, read_ready pinned high both ports) is CORRECT. test_emulator + cocotb +
  rowwise_operation_tb + basic ALL PASSED but MISSED it (no back-to-back-MUL stress) -> demo
  (coherent text) is the real gate; unit tests need a back-to-back-MUL sequence + a full-model
  RTL-sim harness. FIX: rewrite MUL to ADD's lockstep approach (both ports same latency -> feed
  responses directly, no holding regs). Mechanism is SOUND (ADD works, +2.9% real); only MUL impl
  buggy. Fix in progress with a reproduce-first mandate. Broken commit de93a70 stays on
  MatmulDdrFrontier; shippable deliverable remains validated 1943 (release 2026.07.11-2126).
