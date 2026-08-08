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

## Log
- 2026-08-08: mission start. Paused multiprompt agent. Isolated: rms_norm b>0 = ROOT CAUSE.
  Added regression test to test_pynqvivado_basic. Launched RTL root-cause+fix agent (sim-only).
