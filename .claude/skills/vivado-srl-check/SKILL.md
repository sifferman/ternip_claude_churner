---
name: vivado-srl-check
description: Post-build verifier that no pipelined-interconnect wrapper modules had their FFs collapsed into SRL primitives (which would defeat SLR-crossing). Invoke via the TCL script under `scripts/`.
---

# vivado-srl-check

After any `make vivado` or `make pynqvivado_au250_hw` build, run this
to verify Vivado did NOT collapse the pipelined-interconnect chains
into SRL16E / SRL32E / SRLC32E primitives.

## Why this matters

The wrappers `ternip_pipelined_interconnect`,
`axi_ternip_pipelined_interconnect_rd`, and
`axi_ternip_pipelined_interconnect_wr` exist to give Vivado N
back-to-back FF stages the placer can distribute across LAGUNA
register tiles for cross-SLR routing (UG949 §6). An SRL primitive
lives entirely within ONE SLICEM — it cannot be split across SLR
boundaries. So if any stage collapses to an SRL, the LAGUNA
distribution becomes impossible and the cross-SLR slack recovery
this design depends on falls apart.

This is normally prevented by:
- `axis_pipeline_fifo`'s inline `(* shreg_extract = "no" *)` on
  every per-stage reg (alexforencich's intent).
- `pre_synth_design.tcl` Section 3 emitting
  `set_property SHREG_EXTRACT NO [...]` on the wrapper instances
  (belt-and-suspenders).
- `FLATTEN_HIERARCHY=none` keeping each register-slice instance in
  its own module scope (so SRL inference can't see across stages).

## Usage

```bash
# Against a project file (preferred — uses the latest impl run)
vivado -mode batch -nojournal -nolog \
    -source .claude/skills/vivado-srl-check/scripts/check_srl_in_pipelines.tcl \
    -tclargs ternary_matmul/synth/pynqvivado_au250/build/xcu250_D=1024_MaxCores/hw/_x/link/vivado/vpl/prj/prj.xpr

# Against a routed DCP (for forensics on completed builds)
vivado -mode batch -nojournal -nolog \
    -source .claude/skills/vivado-srl-check/scripts/check_srl_in_pipelines.tcl \
    -tclargs artifacts/<datecode>/design_1_wrapper_routed.dcp
```

Greppable output:
```
SRL_CHECK_TOTAL=<n>
SRL_CHECK_OK=true   # safe, all stages remain as discrete FFs
SRL_CHECK_OK=false  # at least one wrapper collapsed -- investigate
```

Exit code is 0 on success (OK=true), 1 on violation. Per-violation
detail lists the offending cells with their REF_NAME.

## What to do on a violation

1. Identify which wrapper(s) collapsed (the violation message lists
   the cell hierarchy).
2. Re-check the inline `(* shreg_extract = "no" *)` attribute is
   present in the source module (alexforencich or ternip).
3. Re-check `pre_synth_design.tcl` Section 3 matched the scope.
4. If FLATTEN_HIERARCHY is enabled somewhere it shouldn't be, find
   and disable.
5. If alexforencich's `axi_register_rd.v` / `axi_register_wr.v` is
   the source, those don't carry `shreg_extract = "no"` inline; the
   TCL is the only defense. Verify the get_cells pattern matched
   the instances by running the same get_cells query manually.
