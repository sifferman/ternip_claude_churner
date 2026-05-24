---
name: yosys-fanout
description: Fast-iteration FPGA fanout analysis. Run yosys (~1-3 min) to synthesize the RTL with the xc7 cell library, then parse the netlist JSON to report the top-N highest-fanout nets. Use this skill whenever the user wants to quickly check which signals have high fanout, whether an RTL change reduced fanout, or "where are the wide-CE / wide-fanout signals" -- without waiting for a full Vivado synth/place run (~30+ min). Triggered by /yosys-fanout.
---

# yosys-fanout

A **10-30× faster** alternative to running Vivado synth for the specific
question "where are the high-fanout signals in my RTL?". Yosys
synthesizes the design with `synth_xilinx -family xc7` (Artix/Kintex/
Spartan-7 cells) in a few minutes, writes a JSON netlist, and a Python
script walks the netlist counting per-bit fanouts.

## When to use

Trigger when the user wants any of:

- "where are the high-fanout signals"
- "did my MAX_FANOUT attribute take effect"
- "show me the top N fanout nets after this RTL change"
- "is `<signal>` still driving a wide fanout"
- "can we iterate on fanout-reduction RTL changes without waiting for Vivado"

Use this instead of `vivado-read-reports` when the user wants
*structural* / *RTL-level* fanout, not Vivado's post-place timing
report.

## When NOT to use

- For **net delays** or **WNS/TNS** numbers — yosys doesn't do placement
  or routing, so those don't exist in the JSON. Use `vivado-read-reports`.
- For **post-phys_opt** fanout — yosys can't replicate via
  `force_replication_on_nets` like Vivado's phys_opt does. The reported
  fanout is the pre-replication (logical) value.
- For **exact cell counts** — yosys's xc7 library differs from Vivado's
  xcu250. Logical fanout is comparable; LUT/FF counts are approximate.

## Inputs

Two optional arguments:

1. **`<config>`** — config name like `xcu250_D=1024_OneCore` (default:
   whichever is in `$CONFIG` env var, or `xcu250_D=1024_MaxCores` if
   no env).
2. **`<top_n>`** — how many nets to report (default 50).

If the user just says `/yosys-fanout`, run with the most recent CONFIG.

## How it works

Three sequential steps, each contained:

1. **Preprocess RTL**: `make build/$(CONFIG)/rtl.sv2v.v` — runs sv2v to
   produce a single Verilog file from all the SystemVerilog. Cached;
   re-runs only when RTL changes. Takes ~10-30s on first run, near-zero
   on subsequent.

2. **Yosys synth + JSON dump**:
   ```bash
   yosys -p 'tcl SKILL_DIR/scripts/synth_json.tcl <rtl.sv2v.v> <out.json>'
   ```
   Reads the preprocessed RTL, runs `synth_xilinx -top ternip_core
   -family xc7`, then `opt -full`, then `write_json`. Takes 1-3 minutes
   on this design. (For comparison, full Vivado synth + impl is 30-60+
   min.)

3. **Parse JSON for fanout**:
   ```bash
   python3 SKILL_DIR/scripts/fanout_report.py <out.json> <top_n>
   ```
   Walks every cell's input-port connections, counts sinks per bit,
   maps back to netnames, sorts descending by fanout, prints top N.

The Python script does NO synthesis of its own — it just parses the JSON
yosys produced. So step 3 is millisecond-scale.

## Invocation

```bash
# step 1 (sv2v preprocess, cached)
make build/<config>/rtl.sv2v.v CONFIG=<config>

# step 2 (yosys ~1-3 min)
mkdir -p .claude/skills/yosys-fanout/build
yosys -p "tcl .claude/skills/yosys-fanout/scripts/synth_json.tcl \
          build/<config>/rtl.sv2v.v \
          .claude/skills/yosys-fanout/build/<config>.json" \
      -l .claude/skills/yosys-fanout/build/<config>.log

# step 3 (parse, instant)
python3 .claude/skills/yosys-fanout/scripts/fanout_report.py \
    .claude/skills/yosys-fanout/build/<config>.json \
    <top_n>
```

Resolve `<config>` from the user's request or `$CONFIG` env. Default to
`xcu250_D=1024_MaxCores`.

## Output format

```
Top 50 highest-fanout net bits in .claude/skills/yosys-fanout/build/<config>.json

   FO  Net Name [bit]                                                 Sample sinks
----------------------------------------------------------------------------------------------------
 4147  rms/parallel_squares[0].square/mul_star.starmul/in_valid_q1 [0]  cell_a.D[0], cell_b.D[0], ... (+4145 more)
 2718  tmatmul/state_q [1]                                              cell_c.D[0], cell_d.D[0], ... (+2716 more)
  ...
```

Each row: the LOGICAL fanout of one bit of a net, and a sample of the
sinks. "Net Name" is the post-synth hierarchical name; bracketed bit
index is the bit position within a multi-bit net.

## Interpretation hints

- **A 32-bit data bus** where each bit goes to one register has 32
  separate rows, each FO=1. It WON'T dominate the top of the list.
- **A 1-bit control / state / valid signal** driving wide CE typically
  shows up at the top with FO = number of sinks.
- **MAX_FANOUT attributes** applied at the source-RTL level *will* show
  in yosys (synth-time replication is honored). Post-place replication
  by Vivado's phys_opt is NOT in yosys output.
- **Names may be flattened or mangled** — yosys's `synth_xilinx`
  flattens by default. Hierarchical names appear as dotted paths in net
  names.
- **`_q1` / `_q` / `_replica` suffixes**: not added by yosys itself —
  those are RTL-declared names that propagate through. The `_replica`
  suffix appears post-phys_opt in Vivado but not in yosys.

## Caveats

- **Per-build cost: 1-3 min.** Run only when you actually want a fresh
  report. Skill should NOT auto-run yosys on every RTL change unless
  the user asks.
- **Different cell library than Vivado-xcu250.** LUT/FF counts and exact
  primitive selections will differ. Logical fanout transfers; physical
  delays do not.
- **Top-level kernel-flow infrastructure (axi_ternip_batched, DMAs,
  AXI interconnect) is NOT in this report.** The Yosys script only
  synthesizes ternip_core. Use `vivado-read-reports` to get fanout
  numbers that include the full Vitis-integrated design.

## Testing

Smoke test on the current RTL state:

```bash
make build/xcu250_D=1024_MaxCores/rtl.sv2v.v CONFIG=xcu250_D=1024_MaxCores
mkdir -p .claude/skills/yosys-fanout/build
yosys -p "tcl .claude/skills/yosys-fanout/scripts/synth_json.tcl \
          build/xcu250_D=1024_MaxCores/rtl.sv2v.v \
          .claude/skills/yosys-fanout/build/test.json" \
      -l .claude/skills/yosys-fanout/build/test.log
python3 .claude/skills/yosys-fanout/scripts/fanout_report.py \
    .claude/skills/yosys-fanout/build/test.json 20
```

Expected: top 5-10 rows are the same wide-fanout signals you've been
chasing (rst_nq, MOA state, tmatmul state_q, etc.), with FO numbers in
the thousands.
