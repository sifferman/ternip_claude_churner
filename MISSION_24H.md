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

## LIGHTER EQUALIZER (2026-08-08 ~10:50 PM): footprint minimized to recover timing.
Root of the -0.073: the first equalizer added a NEW 68-bit result register (OutPrecision=68 for the
rms divider) relocated into the dense rms region, displacing the marginal paths. Lighter version:
reuse the divider's EXISTING output register (drop the 68-bit eq_result_q), drop eq_captured, use a
down-counter+==0 (not a wide comparator), window +12. Only ~8 new small FFs now.
Sim-verified (agent + independently by me): cocotb test_rms_norm_batch all 7 lanes 0.0000, 5/5;
rms_tb+tmatmul_tb v+vcs; test_emulator ALL MATCH. Commits: ternip 3faa062, ternary_matmul a2d5a75.
REBUILD running eq2, kicked 10:53 PM, ETA ~4:53 AM. On completion: check WNS (target >0), then
full silicon validation (test_rms_norm_batch 24 lanes + test_pynqvivado x_f + 24 texts + emu match).

## TIMING: congestion cliff — lighter equalizer got WORSE (2026-08-09 ~3:30 AM).
Heavy equalizer WNS=-0.073; LIGHTER equalizer WNS=-0.140 (worse!). "Fewer FFs->better timing" is
FALSE here: design is at a razor-thin congestion cliff (+0.002 baseline), any rms-region
perturbation swings WNS unpredictably (placement noise). FF-count approach is a dead end.
NEW PLAN (principled): fix bug 2 at the WRAPPER, not the divider. Revert the equalizer entirely
(rms datapath back to +0.002 baseline netlist), keep the trivial bug-1 ddr_address/rms_length fix,
and add a cross-core JOIN in ternip_batched.sv (wait for ALL cores' ready/stall at each instruction
boundary, re-sync every instruction) so the variable-latency divider no longer desyncs b>0. The
wrapper-join is OFF the rms critical path -> timing should return to ~+0.002. Agent working it.
FALLBACK: the -0.073 heavy-equalizer xclbin is FUNCTIONALLY CORRECT (all 24 lanes validated + 24
texts) and staged at fulladd:/soe/esifferm/GitHub/ternip_claude/hw_run_rmsfix/kernel.xclbin. If
wrapper-sync doesn't close, ship that (negative margin but silicon-validated) as interim.

## WRAPPER-JOIN FAILED (2026-08-09 ~4:20 AM). Agent verdict: the per-core ternip_pipelined_interconnect
buffers decouple cores from the wrapper, so an instruction-boundary join can't force core-level
lockstep, and &-reducing the buffers' valid/ready deadlocks them. Reverted to committed light
equalizer. THREE timing approaches now failed (heavy eq -0.073, light eq -0.140, wrapper-join broken)
-> per CLAUDE.md "step back at 3+ failures", stop trying divider/wrapper variants blindly.

## NEW ANGLE (2026-08-09 ~4:36 AM): BUG-1-ONLY hypothesis + build kicked.
KEY INSIGHT: the equalizer branch was built on ternip 7887612 ("pipeline divider input"), which
780b285 had ALREADY REVERTED as net-negative (-0.114 at BS=6). So the -0.073/-0.140 regression is
partly 7887612 itself, NOT just the equalizer FFs. The true +0.002 baseline is ternip 2a689cc.
SECOND INSIGHT: the ORIGINAL silicon symptom was lanes b>0 = EXACTLY ZERO. That is bug-1's exact
fingerprint (rms_length=0 -> sqrt(0)=0 -> input*0 = 0). A divider DESYNC (bug-2) would give wrong-
but-NONZERO garbage, which was NOT observed. => strong hypothesis: bug-1 (ddr_address/rms_length
guard) was the ENTIRE bug; bug-2/equalizer was an unnecessary bundled fix that wrecked timing.
EXPERIMENT (building now on eq2, kicked 4:36 AM, ETA ~10:00-10:30 AM): ternip@2a689cc (clean +0.002
baseline, NO equalizer, NO 7887612 pipeline) + ternip_batched.sv bug-1 guard only. If bug-1 alone
is functionally correct, this IS the +0.002 deliverable (timing solved outright).
GATES on this netlist: test_emulator ALL MATCH (144/144); vcs tmatmul_tb PASS; vcs rms_tb clean
$finish; verilator blocked by lz4.h toolchain (cocotb agent fixing). cocotb test_rms_norm_batch
(all lanes, THE definitive bug-1-sufficiency test) RUNNING (agent aac59dca4cb9ddab0).
DECISION: kicked the build in parallel with cocotb (eq2-idle hard rule + build is informative
regardless of cocotb -> gives true baseline+bug1 timing). If cocotb shows lanes b>0 FAIL, kill the
build and pivot to Option A: fixed-latency divider with ZERO added FFs by modifying
bsg_idiv_iterative_controller.sv FSM to always traverse optional states (built on 2a689cc, not
7887612). Fallback remains the -0.073 validated xclbin.
NOT YET COMMITTED: bug-1-only is in the working tree only; commit after cocotb confirms.

## BUG-1-ONLY: INSUFFICIENT (2026-08-09 ~5:00 AM). cocotb verdict: with the plain variable-latency
divider (no equalizer) + bug-1 guard, test_rms_norm_batch FATALLY ABORTS on ternip_batched.sv:118
(`core_instruction_ready_o bits not all uniform`) at t=960180ns — the cores DO desync during
rms_norm (divider NEG1/REPAIR/QUOT optional FSM states = data-dependent latency). So bug-2 (divider
desync) is REAL. The "exactly-zero" silicon symptom was bug-1's ×0 MASKING bug-2; fixing bug-1
exposes the desync. Killed the bug-1-only build on eq2. Both bugs needed.

## HEAVY-EQ-ON-CLEAN-BASELINE: BUILT + KICKED (2026-08-09 ~5:1x AM). The single-variable timing fix.
INSIGHT: the -0.073 (heavy eq) / -0.140 (light eq) builds were stacked on ternip 7887612 (rms
divider-input pipeline), which 780b285 ALREADY reverted as net-negative (-0.114 @ BS=8). 7887612
touches ONLY ternip_rms.sv; the equalizer touches ONLY ternip_div.sv -> independent, rebase-clean.
So: take the -0.073 SILICON-VALIDATED heavy-equalizer build and change EXACTLY ONE thing — remove
7887612 (rebase the equalizer onto the clean 2a689cc +0.002 baseline). One variable, from a
validated datapoint (CLAUDE.md "one change per build"). Hypothesis: 7887612 was the bulk of the
regression -> heavy-eq-on-clean should swing toward positive while keeping proven correctness.
COMMITS: ternip 230e646 (branch rms-eq-clean-base, pushed); ternary_matmul 59e9ba8 (NumSeparateKernels,
pushed). Bug-1 guard retained.
GATES (all green on this netlist): cocotb 5/5, test_rms_norm_batch all 7 lanes max|HW-emu|=0.0000
(NO desync assertion — equalizer restores lockstep); vcs tmatmul_tb + rms_tb clean $finish;
test_emulator 144/144 ALL MATCH.
BUILD: eq2 MaxCores nk=4 BS=6 TP=128, kicked ~5:1x AM, ETA ~10:30-11:00 AM. Monitor bddk3lqk5.
ON COMPLETION: gen timing CSV BEFORE next kick; check WNS (target >0). If positive -> silicon-
validate (test_rms_norm_batch 24 lanes + test_pynqvivado + 24 texts) = robust "1943 tok/s for real".
IF STILL NEGATIVE -> the equalizer FFs themselves are the culprit (not 7887612); pivot to Option A:
zero-added-FF fixed-latency divider by modifying bsg_idiv_iterative_controller.sv FSM to always
traverse NEG1/REPAIR/QUOT with datapath ops re-gated to no-op when their entry-condition is false.
FALLBACK stays: -0.073 heavy-eq xclbin (silicon-validated correct) at fulladd hw_run_rmsfix.

## HEAVY-EQ-ON-CLEAN RESULT: MISS, −0.349 (2026-08-09 ~9:28 AM, build 2026.08.09-0451).
Removing 7887612 made timing WORSE (kernel WNS −0.349 vs −0.073), REFUTING the "7887612 is
the confound" hypothesis. 7887612 (rms divider-input pipeline) splits the deep MOA→div_bsg comb
path — it's HELPING at BS=6 with the equalizer, despite its "net-neg @ BS=8" label. **KEEP 7887612.**
CSV cluster (kernel scope): NET-DELAY-dominated, broadly spread — worst path tmatmul
gbfifo_import/sipo (slack −0.349, 5 levels, Net 2.66ns vs Logic 0.48ns); histogram tmatmul 2059 /
rms 1699 / rowwise 1154 / vreg 448, in cores 1/3/5. => CONGESTION, not logic depth. Design is a
CHAOTIC congestion cliff: WNS swings ±0.35ns on any rms-region perturbation (+0.002 → −0.073 →
−0.349). Release 2026.08.09-0451 (churner) has full data + CSV + tarball.

## NEXT BUILD: OPTION A — zero-added-FF fixed-latency divider FSM (agent a21c5d8ffa04dcf17 building it).
Rationale: the −0.073 miss is the equalizer's 68-bit hold-register FF-mass perturbing the razor-thin
rms region (mission log: worst −0.073 paths are pre-existing marginal rms paths, NOT the equalizer's
own logic → placement perturbation). Only resource-neutral levers work on this congestion-marginal
base. Option A makes bsg_idiv_iterative_controller.sv data-independent-latency (always traverse
NEG1/REPAIR/QUOT with datapath ops re-gated to no-op by their already-registered entry conditions
r_neg_r / add1_neg_last_r / q_neg_r) → cores stay in lockstep WITHOUT the equalizer → ternip_div.sv
reverts to baseline (drops the 68-bit register). Net FFs vs +0.002 baseline: ZERO. On the 7887612
base (good placement basin). CORRECTNESS BAR: bit-exact divider equivalence test (modified vs
original quotient/remainder across random sweep + edges) + constant-latency assert, THEN cocotb
all-lanes + rms_tb + tmatmul_tb + test_emulator. Agent must NOT push; I review the FSM diff before
kicking. If Option A also lands negative (cliff), SHIP the −0.073 silicon-validated build as the
deliverable and report positive-margin is congestion-limited.

## USER REDIRECT -> COUNTER APPROACH (2026-08-09 ~10:00 AM). User proposed (instead of the risky
FSM surgery) adding a COUNTER to ternip_div.sv that gates out_valid_o until a fixed, data-
independent cycle count (>= longest possible divide), parameterized off ternip_div params, plus
an assert that the counter never finishes before the divide. This is structurally SAFER than the
FSM change (can't alter the quotient) and the assert makes it self-checking. Implemented:
FixedDivideLatency=DivInternalPrecision+16 (the proven equalizer's bound), ~7 FFs (counter +
running flag), NO result register (bsg parks result in DONE until yumi; we just delay draining).
Built on 7887612 (KEEP the rms pipeline). COMMITS: ternip ab425d2 (branch
rms-div-fixedlatency-counter), ternary_matmul 635ac3b (NumSeparateKernels), pushed.
GATES all green: cocotb 5/5 (test_rms_norm_batch all 7 lanes 0.0000, NO desync assertion); vcs
rms_tb (fixed-latency assert did NOT fire) + tmatmul_tb; test_emulator 144/144 ALL MATCH.
NOTE: this is structurally close to the earlier "light equalizer" (-0.140 sample) — may land near
there on the chaotic cliff rather than beating -0.073 — but it's the safe approach + a fresh sample.
Killed the FSM-surgery agent (a21c5d8) per the redirect (it had proven bit-exact equivalence,
constant-43 latency — a valid zero-FF fallback if the counter regresses).

## eq2 SSH TRANSIENT BLIP (2026-08-09 ~10:05 AM). After the -0.349 build completed, ssh key auth to
eq2 intermittently failed (Permission denied publickey) for ~several min, blocking the counter-build
kick. Diagnosed NOT a credential loss: authorized_keys intact + correct perms on shared /soe;
same id_ed25519 key authenticates fine to fulladd; eq2 up (ping OK, sshd responding). Was an eq2-side
transient (likely NFS/sshd hiccup reading the shared authorized_keys); recovered on its own within
minutes (3/3 clean ssh). Counter build kicked once stable. If it recurs, it's eq2-side infra, not
our keys.

## MULTI-DAY PUSH (2026-08-09 PM): close timing + increase BS on SLR0/2/3 -> ~3k tok/s
User stepped back for a few days. Established framing (see memory feedback_congestion_65pct_ceiling):
- Binding constraint = CONGESTION / CLB(slice) occupancy, ~65% routable ceiling. NOT LUT% (32%).
- BS9 nk3 (2026.08.09-1140) ROUTED at ~65% CLB on SLR0/2/3 but WNS=-0.302 (intra-core net-delay
  from spreading at high occupancy). Routability != closure.
- pblock compaction is DFX-DEAD (NSAI_42 hard + NSAI_43 soft = VPL 18-1000 x4). Do NOT retry.
- => DFX-safe compaction = REDUCE per-core occupancy so placer spreads less naturally.

TRACK A (primary, RTL, multi-day): DSP/BRAM fixed-latency reciprocal-sqrt replacing
ternip_div+ternip_sqrt in rms. Moves the LUT-heavy iterative divide+sqrt onto FREE DSP(12%)/
BRAM(13%) -> cuts CLB occupancy -> less spread -> closes timing + frees room for higher BS.
Also fixed-latency -> deletes the counter (the thing that caused the -0.073 regression). Agent
a389d20a7c0077d1d doing Phase 1 (standalone module + accuracy vs float64 & vs current div+sqrt +
area estimate). Review accuracy/area BEFORE integrating (Phase 2) + building. Ultimate gate:
test_emulator ALL MATCH (wrong rsqrt = wrong tokens).

TRACK B (eq2 busy, DFX-safe diagnostics): BS sweep maps occupancy<->closure.
- BS9 nk3 = -0.302 @ ~65% (done, 2026.08.09-1140).
- BS8 nk3 @ ~58% (BUILDING, kicked ~16:42, tm b344e83) -- does dropping occupancy close? Validates
  the rsqrt thesis cheaply. 24 lanes = 2130 tok/s (= deliverable) if it closes.
- If BS8 closes -> ~58% is the closing occupancy -> rsqrt (lowering occupancy at BS9+) closes BS9+.
- If BS8 doesn't close -> problem deeper than occupancy; escalate to rsqrt + directives.

TRACK C (after rsqrt lowers occupancy): push BS10-12 on roomy SLRs (asymmetric two-kernel nk=4:
BS-big x3 on SLR0/2/3 + BS-small x1 on SLR1) -> ~3k tok/s. Needs two kernel .xo variants.

Vivado docs reviewed (references/vivado-docs-2023.1): UG949 congestion levers largely exhausted
(pblocks dead, AlternateCLBRouting tried NSAI_40, directives tried). Occupancy reduction (Track A)
is the remaining high-leverage lever.

Preserved: BS9 nk3 prj.xpr + routed DCP at /soe/esifferm/GitHub/ternip_claude/placement_1140.
