# QUESTIONS.md

Open questions / judgement calls Claude wants the user to review out of
band. Newest at the top. Each entry: what was decided, the alternative
considered, and how to redirect if the user wants something different.

**Auto-cleanup**: when Claude can confidently answer a question itself
(or the user answers it elsewhere), the entry is removed from this list.

---

## 2026-07-01 8:44 PM PDT — Vivado AXI Register Slice IP (PG373) integration

### Status

`ternip_pipelined_interconnect.Implementation` now has three options.
The third (`"vivado_axis_register_slice"`) is a stub that fatals at
elaboration — the actual Vivado IP integration hasn't been done yet.

### What Vivado IP integration needs

1. **`create_ip` TCL hook** — probably a new file
   `synth/pynqvivado_common/create_axis_register_slice_ip.tcl` sourced
   at project-setup time (before `sv2v`/synth). Command shape:
   ```tcl
   create_ip -name axis_register_slice -vendor xilinx.com \
             -library ip -version 1.1 \
             -module_name axis_register_slice_pg373_ternip
   set_property -dict [list \
       CONFIG.TDATA_NUM_BYTES {66} \
       CONFIG.HAS_TREADY {1} \
       CONFIG.REG_CONFIG {12} \  # 12 = Multi-SLR Crossing mode
   ] [get_ips axis_register_slice_pg373_ternip]
   generate_target {synthesis simulation} [get_ips ...]
   ```
2. **Multiple width instances** — each `Implementation="vivado_axis_register_slice"`
   call site has a different `DataWidth`. Either:
   - Generate one IP per width used (need to enumerate uses at TCL time), or
   - Instantiate the raw behavioural module
     `axis_register_slice_v1_1_XX_axis_register_slice` from the IP repo
     directly (referring to the encrypted RTL by name — works if the
     `IP_CACHE` is populated).
3. **RTL instance** — replace the `initial $fatal` stub in the
   `g_vivado_axis_register_slice` genblock of
   `ternip_pipelined_interconnect.sv` with a real instantiation of the
   generated wrapper.
4. **Simulation model** — the IP has a behavioural simulation model; make
   sure the TCL `generate_target {simulation}` step emits it and
   verilator can compile the resulting `.v`.

### Alternative considered

Auto-pipelining via UG949 HDL attributes (`(* autopipeline_module="yes" *)`
+ `AUTOPIPELINE_GROUP` / `AUTOPIPELINE_LIMIT`) — same underlying
mechanism as PG373 Multi-SLR Crossing / Auto-Pipeline Insertion mode.
Doesn't need IP catalog integration; just add attributes to the module.
Rejected for now because the module has a chain of `axis_pipeline_fifo`
instances rather than a simple FF-only pipeline, which is the shape
auto-pipelining expects.

### Decision path

- If the current build's `axis_pipeline_register` (Fully-Registered
  analog) shows measurable WNS improvement over build_56, do the
  `create_ip` integration for `"vivado_axis_register_slice"` and A/B
  compare against `axis_pipeline_register` on the same channel. The IP's
  LAGUNA-aware placement + auto-pipelining should beat pure-RTL.
- If `axis_pipeline_register` is neutral / negative, the Vivado IP is
  unlikely to help either — skip the integration work, iterate on
  per-instance NumStages instead.

---



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
