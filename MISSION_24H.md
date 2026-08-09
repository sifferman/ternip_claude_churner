# 24-Hour Mission (started 2026-08-08)

**User goals (both required by return):**
1. **Emulator and FPGA MATCH** (exactly, or the divergence understood+fixed).
2. **All 24 lanes (NumSeparateAxiInstances=4 × BatchSize=6) generate INDEPENDENT valid text.**

Deliverable under test: nk=4 BS=6 (release 2026.07.11-2126), xclbin staged at
/soe/esifferm/GitHub/ternip_claude/hw_run/kernel.xclbin + nsk11_config.svh.

## Verified so far (this investigation)
- tmatmul: bit-EXACT to emulator, NOT transposed, across full range [-106,110]. All 24 lanes.
- sig: bit-EXACT to emulator. silu: within 1 quant step.
- distinct per-lane data round-trips correctly on all 24 lanes (element-wise add).
- FULL-MODEL divergence: output.0.x_f ~77% elem-diff vs emulator, x_c ~60%. Localized:
  x_f = sig(tmatmul(x_f_in)); tmatmul+sig are EXACT -> divergence is in x_f_in, produced by
  **rms_norm + mul** (the only un-isolated ops in the x_f chain). << ROOT-CAUSE HUNT HERE.
- 24-text harness (test_pynqvivado_multiprompt + multi_backend): b=0 of each CU correct text;
  b>0 identical garbage. HARDWARE proven correct for 24 lanes at layer 0 -> this is a HOST bug
  in the multiprompt recurrent loop (not silicon).

## Plan (single shared U250 -> serialize card access)
### GOAL 1 first (prereq for goal-2 validation): make emu == FPGA
- [ ] Isolate rms_norm vs mul (test_rmsnorm_mul.py) -> which diverges, by how much, why.
- [ ] Root cause: emulator's rms_norm uses float64 + rounding; RTL uses fixed-point divider
      (DIV_BSG) + sqrt. Likely an EMULATOR-modeling gap (host fix, fast) vs real RTL imprecision
      (5h rebuild). Determine which.
- [ ] Fix whichever is wrong so emu bit-matches FPGA on rms_norm/mul -> x_f then matches.
- [ ] Confirm full-model test_pynqvivado: x_f/x_c 0 failures after fix.
### GOAL 2: 24 independent texts
- [ ] Resume/redirect the multiprompt agent (a34643b4f28d54136) to fix the b>0 loop bug.
- [ ] Produce 24 distinct prompts -> 24 distinct coherent texts, each matching the (now-trusted)
      emulator per lane.

## Card discipline
Only ONE process on fulladd at a time. Background multiprompt agent PAUSED (msg sent) while I
run goal-1 diagnostics. Hand card back for goal-2 after goal-1 fixed. RTL rebuilds (if needed)
run on eq2 and only touch the card at validation.

## BREAKTHROUGH (2026-08-08): root cause = batched rms_norm b>0 hardware bug
Both mission goals trace to ONE bug. Isolation results (all on silicon, HW vs emulator):
- tmatmul EXACT (not transposed), sig EXACT, silu ~1 quant step, mul EXACT, add/ldv/sv all lanes OK.
- **rms_norm: lane b=0 EXACT; lanes b=1..5 WRONG/ZERO** — identical across all 4 CUs, fails even
  with IDENTICAL broadcast input. => a batch-specific hardware bug in the rms datapath for b>0.
- This is the root cause of BOTH: (goal1) full-model x_f divergence [x_f=sig(tmatmul(mul(rms_norm)))],
  and (goal2) 24-lane text collapse (b>0 garbage).
- Regression test added + committed: test_pynqvivado_basic.py::test_rms_norm_batch (ternip_matmul
  NumSeparateKernels e05b6d5). Reproduces cleanly (b=0 OK, b>0 FAIL, all 4 CUs).
- Puzzle: cores are identical genvar replicas fed identical data, yet b>0 differs => structural
  asymmetry tying rms to core[0] (shared resource / core[0]-only stall+desync via data-dependent
  divider latency) OR synthesis/physical. ternip_batched.sv only reads core[0] control/stall.
- FIX AGENT launched (a37c54fdccc3cf823, sim-only): deep RTL trace + reproduce in cocotb (batched
  verilator sim, unlike single-core SV tbs) + fix. If it repros in cocotb -> RTL logic bug, fixable.

## Once fixed: (1) validate emu==FPGA via test_rms_norm_batch + test_pynqvivado; (2) rebuild if RTL
## changed (5h eq2); (3) resume multiprompt agent for 24 independent texts vs the now-correct emulator.

## FIX FOUND + SIM-VERIFIED (2026-08-08). Two bugs in batched rms_norm b>0:
1. ddr_address masking (ternip_batched.sv): wrapper masked instruction.ddr_address='x for cores
   b>0 (area opt for DDR FUs), but RMS REUSES that field as rms_length (the divider dividend). So
   b>0 got rms_length=0 -> sqrt(0)=0 -> rms_norm out = input*0 = EXACTLY 0. Index-based -> explains
   the broadcast-also-fails case. FIX: `if (i!=0 && instruction_i.fu != RMS) mask` -> keep it for RMS.
2. Variable-latency DIV_BSG divider (ternip_div.sv): data-dependent latency desyncs the lockstep-
   assuming cores (wrapper drives control/stall from core[0] only). FIX: fixed-latency equalizer
   (hold quotient to a data-independent cycle count) -> all cores finish same cycle. (User's idea.)
COMMITS (NumSeparateKernels, pushed): ternip 3ef7a0f, ternary_matmul 60279b1.
SIM-VERIFIED (independently by me): test_emulator ALL MATCH; cocotb batched 5/5 incl test_rms_norm_batch
all 7 lanes max|HW-emu|=0.0000; rms_tb+tmatmul_tb v+vcs pass.
REBUILD: kicked eq2 5:17 PM, ETA ~11:17 PM (nk4 BS6 TP128 deliverable). Card free during build.
NEXT (after build): silicon-validate test_rms_norm_batch all lanes + test_pynqvivado (emu==FPGA);
then 24 texts (multiprompt collapse WAS the rms bug -> should now work). GOAL1 ~done pending silicon.

## SILICON-VALIDATED (2026-08-08 ~10:30 PM): rms fix WORKS on hardware.
- test_rms_norm_batch on silicon: ALL 24 lanes (4 CU x 6 batch) match emulator at 0.0000. Bug FIXED.
- 24-prompt generation: 24 DISTINCT coherent prompt-appropriate texts (France->Paris, Japan->Tokyo,
  Italy->Rome, Germany->Berlin, Spain->Madrid, sun rises->east, largest planet->Jupiter, ...). NO
  b>0 collapse. GOAL 2 = ACHIEVED. Lane 0 matches emulator 10/10 greedy tokens.
- So 1943 tok/s is now REAL: all 24 lanes correct (was 4/24 before).
REMAINING for a robust deliverable:
- WNS = -0.073 (rebuild missed timing). Equalizer FFs perturbed the razor-thin +0.002 baseline;
  worst failing paths are the PRE-EXISTING marginal rms datapath (square->MOA -0.071, FSM->norm_mul
  -0.070, divider opC -0.070), not the equalizer's own logic. Need to recover ~0.075ns.
- Full-generation emu bit-match: lane 0 = 10/10; other sampled lanes match prefix then diverge
  (accumulated tiny rounding / the -0.073 violations). A clean-timing build should tighten this.
PLAN: lighten the equalizer (min FF footprint) to un-perturb placement -> rebuild -> re-validate
(all 24 lanes + WNS>0 + emu match). Fix committed NumSeparateKernels: ternip 3ef7a0f, tm 60279b1.

## Log
- 2026-08-08: mission start. Isolated rms_norm b>0 root cause. Fix agent found 2 bugs
  (ddr_address/rms_length masking + variable-latency div desync), fixed + sim-verified. Rebuilt +
  SILICON-VALIDATED: all 24 lanes correct + 24 distinct texts (GOAL 2 done). Timing regressed to
  -0.073 (equalizer perturbed razor-thin baseline); recovering for robust +margin deliverable.
