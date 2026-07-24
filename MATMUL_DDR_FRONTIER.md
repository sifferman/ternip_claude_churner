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

## Log
- 2026-07-24: branch MatmulDdrFrontier off validated state. Sweep confirms +49% prize
  (TP=256 + eff 0.9). Launching P0 investigation (TP=256 congestion, DDR efficiency, real bound).
