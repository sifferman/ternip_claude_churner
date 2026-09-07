# 370M lane search, 2026-09-04 .. 09-06

Fixed: D=1024, TmatmulParallelism=128, VectorParallelism=4, LutParallelism=1,
NumVectorRegisters=8, FixedPointExponent=-5, 4 CUs -- ternip_big x3 on SLR 0/3/2
(DDR 0/3/2) and ternip_small x1 on SLR1 (DDR1). 300 MHz, no AUTO-FREQ-SCALING.

tokens/second = lanes x 86.42, where lanes = sum of per-CU BatchSize and 86.42
is singlecore tok/s at VP=4. Lanes must be summed per CU: report_timing used to
compute NumSeparateAxiInstances x BatchSize off one Config, which ignored
ternip_small entirely and understated every total by ~17%.

| big BS | small BS | lanes | tok/s | CINS | place directive | WNS | TNS | failing | outcome | artifacts |
|---:|---:|---:|---:|---:|---|---:|---:|---:|---|---|
| 12 | 6 | 42 | 3630 | 2 | ExtraNetDelay_high | +0.004 | 0.000 | 0 | CLOSED | 2026.09.04-1159 |
| 13 | 6 | 45 | 3889 | 2 | ExtraNetDelay_high | -0.044 | -2.316 | 133 | failed | 2026.09.04-1705 |
| 13 | 6 | 45 | 3889 | 3 | ExtraNetDelay_high | -0.067 | -20.679 | 707 | failed | 2026.09.04-2329 |
| 12 | 8 | 44 | 3802 | 2 | ExtraNetDelay_high | 0.000 | 0.000 | 0 | CLOSED | 2026.09.05-0823 |
| 12 | 9 | 45 | 3889 | 2 | ExtraNetDelay_high | +0.011 | 0.000 | 0 | **CLOSED, deliverable** | 2026.09.05-1451 |
| 12 | 10 | 46 | 3975 | 2 | ExtraNetDelay_high | -0.086 | -8.808 | 260 | failed | 2026.09.05-2003 |
| 12x2 + bigwide 13 | 9 | 46 | 3975 | 2 | ExtraNetDelay_high | -0.158 | -3.862 | 149 | failed | 2026.09.06-0355 |
| 12 (VP=8) | 9 | 45 | 4264 | 2 | ExtraNetDelay_high | - | - | - | PLACE FAILED | none |
| 11 | 12 | 45 | 3889 | 2 | ExtraNetDelay_high | - | - | - | PLACE FAILED | none |
| 12 | 10 | 46 | 3975 | 2 | SSI_SpreadLogic_high | -0.020 | -0.649 | 63 | failed | 2026.09.06-1458 |
| 13 | 7 | 46 | 3975 | 2 | SSI_SpreadLogic_high | running | | | | |

## What the failures established

**Two different ceilings.** ternip_big stops at BatchSize 12 on *timing*;
ternip_small stops near 9 on *capacity* -- SLR1 also holds the shell, so
small=12 could not be placed at all (12809 CLBs needed, 11327 available).
Lanes therefore cannot be rebalanced between them.

**Adding flip-flops hurts.** CoreInterconnectNumStages=3 is tok/s-neutral
(identical projection at 2, 3 and 4) so it looked free, but it made every
metric worse: TNS 9x, failing endpoints 5x, build 8h28m vs 6h19m, and the
router needed two full 1.5h iterations instead of one. This design is
CLB-occupancy bound, so extra FFs cost the placer more room than the extra
stage buys.

**VectorParallelism=8 does not fit.** 13584 CLBs required against 10439
available. DSP sat at 20.5% of budget with 3.87x headroom, which is why it
looked affordable -- but VP consumes CLBs, and CLB is the binding resource.
Buying it back by cutting lanes lands near 2000 tok/s against 3889.

**The bottleneck is per-kernel, not global.** Every failure under
ExtraNetDelay_high concentrated on ternip_big_2 (SLR3) no matter where the
lanes were added: small=10 put 9 of 10 violations there, and the asymmetric
build left the widened kernel (ternip_bigwide) with zero violations while its
12-lane siblings broke.

**SSI_SpreadLogic_high is worth about 0.066 ns.** On the identical 46-lane
config it moved WNS -0.086 to -0.020, TNS -8.808 to -0.649 and failing
endpoints 260 to 63. It is the only lever that improved a failing config, and
it adds no logic. It also moved the violations off the big kernels onto
ternip_small_1 (8 of 10), which is why big 13 / small 7 is the next probe.

**post_route_phys_opt recovered nothing, four times.** Its output equalled the
pre-opt routed summary on every failing build. Do not count on it for margin.

## Not tried

Pblock resizing is unavailable: the pblocks come from XRT's DFX regions, not
from any script in this flow. The remaining lever is RTL logic reduction on the
recurring critical paths, namely the rms divider input and the tmatmul FSM
state register.
