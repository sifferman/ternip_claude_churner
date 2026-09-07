# NumSeparateAxiInstances.md — autonomous loop plan for NSAI variant

The NSAI architecture is what already lives on `main` of `sifferman/ternary_matmul_claude` — N independent `axi_ternip_batched_$i` block-design cells in one Vitis kernel, each with its own `axi_dma_$i` for instructions, its own m_axi_tmatmul/m_axi_loadstore, and `BatchSize` cores internally. Per the visualizer audit, every node belonging to instance `i` has `bank=i` set, and the visualizer pins each instance entirely to SLR `i` (matching DDR[`i`] → SLR[`i`]). The key property the user wants to exploit:

> *"It doesn't require SLR crossing, which is nice."*

When `axi_ternip_batched_$i` is pblock'd into SLR `$i`, **no signal inside the instance ever crosses an SLR boundary** — DDR[$i] is in the same SLR, instruction_decode is per-instance, and the FUs are all local. The single-instance design's existing `ternip_buffered` pipelined_interconnect (`CoreInterconnectNumStages=8`) was sized for the NTB design's worst-case 4-SLR span; for NSAI it likely degenerates to "register slices in the same SLR" — pure timing-margin overhead that should be trimmable.

## What this plan IS NOT

- Not an architectural refactor like `NumTmatmulBanksPerCore.md`. The RTL on main is already what we want.
- Not a replacement for `CLAUDE.md` — that document still describes the iteration mechanics (build invocation, polling, release-notes format, hw_emu pass criterion, etc.). This doc describes the **NSAI-specific deltas**.

## What this plan IS

- The branch setup for NSAI churn
- The floorplan that pins each `axi_ternip_batched_$i` to SLR `$i`
- The allowed-to-modify parameter list for NSAI
- The first few iteration ideas

---

## Branch setup (do this once before iteration starts)

Three branches, all named `NumSeparateAxiInstances`, all based off each repo's `main`:

```bash
# 1) ternip submodule (inside ternary_matmul/third_party/ternip).
#    main of sifferman/ternip_claude is at 187957b (pinned by ternary_matmul main).
cd ternary_matmul/third_party/ternip
git fetch origin
git checkout -b NumSeparateAxiInstances origin/main
git push -u origin NumSeparateAxiInstances

# 2) ternary_matmul submodule. main of sifferman/ternary_matmul_claude
#    is at d6a5491 — the commit the visualizer's NSAI submodule pins.
cd ../../..   # back to ternary_matmul/
git fetch origin
git checkout -b NumSeparateAxiInstances origin/main
# Pin third_party/ternip to its new branch:
cd third_party/ternip && git checkout NumSeparateAxiInstances && cd ../..
git add third_party/ternip
git commit -m "Pin ternip submodule to NumSeparateAxiInstances branch"
git push -u origin NumSeparateAxiInstances

# 3) Outer ternip_claude churner. New branch off main.
cd /soe/esifferm/GitHub/ternip_claude
git fetch origin
git checkout -b NumSeparateAxiInstances origin/main
cd ternary_matmul && git checkout NumSeparateAxiInstances && cd ..
git add ternary_matmul
git commit -m "Pin ternary_matmul to NumSeparateAxiInstances branch"
git push -u origin NumSeparateAxiInstances
```

After this, the working state is: outer-repo `NumSeparateAxiInstances` → ternary_matmul `NumSeparateAxiInstances` → ternip `NumSeparateAxiInstances`. All three branches start identical to their `main`; iteration commits land on top.

**Note**: the existing `NumTmatmulBanksPerCore` branches are NOT a parent here. Optimization work over there (out_fifo MAX_FANOUT, force_replication on tmatmul ports, pblocks targeting `tmatmul_dma[b]`) was tuned for cross-SLR routing pressure that simply doesn't exist in NSAI. Starting from main is cleaner.

---

## Config

Use `config/xcu250_D=1024_MaxCores.svh` (main has it baked already). Initial values:

- D = 1024
- TmatmulParallelism = 256
- VectorParallelism = 4
- LutParallelism = 1
- FixedPointPrecision = 16
- BatchSize = 1
- NumVectorRegisters = 4
- **NumSeparateAxiInstances = 4** (always; see hard rule below)
- CoreInterconnectNumStages = 8

### Hard rules

Three parameters are immutable in this branch:

- **`NumSeparateAxiInstances = 4`** — same logic as NTB's `NumTmatmulBanksPerCore = 4`. The AU250 has 4 DDR banks; we use all of them, one per SLR. Per PG059 Table 2-2, growing to 8 falls off a timing cliff (8×4 SAMD @ 512b = 245 MHz, below 300 MHz target). Lowering is a regression.
- **`TmatmulParallelism = 256`** — matches NSAI main. The wide IV→MOA bus inside an instance (256 × 16 = 4096 bits) lives entirely in one SLR thanks to the floorplan; no cross-SLR pressure to relieve.
- **`D = 1024`** and the fixed-point parameters (`FixedPointPrecision = 16`, `FixedPointExponent = -5`) — model-aligned, not iteration-tunable.

### Allowed-to-modify parameters

In `xcu250_D=1024_MaxCores.svh`:

- `BatchSize` — **the dominant lever**. Each instance has BS cores; total throughput = NSAI × BS × singlecore. Target 20+.
- `VectorParallelism` — per-core compute parallelism. Tunable 4 → 8.
- `LutParallelism` — per-core LUT count for ternary multiply. Tunable 1 → 2.
- `NumVectorRegisters` — BRAM-backed. Tunable 4 → 6 → 8 if BRAM is free.
- `CoreInterconnectNumStages` — **starts at 4 in NumSeparateAxiInstances_1** (bundled with the floorplan); see "Initial iterations" below.

---

## Floorplan — pin each AXI instance to its SLR

**The single biggest new thing in this branch.** On `main`, `bd.tcl` already does the work of instantiating `axi_ternip_batched_0..3` as separate block-design cells. The only missing piece is a `pre_place_design.tcl` that pblocks each one to its assigned SLR.

Create `synth/pynqvivado_au250/pre_place_design.tcl` (NOT `.disabled` — enabled from the first build):

```tcl
# Per-instance SLR pinning. Each axi_ternip_batched_$i is pblock'd to
# SLR $i (matching DDR[$i]). With NumSeparateAxiInstances=4 this means:
#   axi_ternip_batched_0 -> SLR0  (DDR[0])
#   axi_ternip_batched_1 -> SLR1  (DDR[1])
#   axi_ternip_batched_2 -> SLR2  (DDR[2])
#   axi_ternip_batched_3 -> SLR3  (DDR[3])
#
# No signal inside an instance crosses an SLR boundary, so timing
# closure should be much easier than NTB.

puts "pre_place_design: per-instance SLR pinning for NSAI"

proc get_clockregion_range_for_slr {slr_name} {
    set slr [get_slrs -quiet $slr_name]
    if {[llength $slr] == 0} { return "" }
    set clock_regions [get_clock_regions -quiet -of $slr]
    set xs {}
    set ys {}
    foreach cr $clock_regions {
        if {[regexp {X(\d+)Y(\d+)} $cr -> x y]} {
            lappend xs $x
            lappend ys $y
        }
    }
    if {[llength $xs] == 0} { return "" }
    set min_x [tcl::mathfunc::min {*}$xs]
    set max_x [tcl::mathfunc::max {*}$xs]
    set min_y [tcl::mathfunc::min {*}$ys]
    set max_y [tcl::mathfunc::max {*}$ys]
    return "CLOCKREGION_X${min_x}Y${min_y}:CLOCKREGION_X${max_x}Y${max_y}"
}

# Project-wide invariant: NumSeparateAxiInstances is ALWAYS 4.
set N_INSTANCES 4

for {set i 0} {$i < $N_INSTANCES} {incr i} {
    # The bd.tcl names each instance cell axi_ternip_batched_$i.
    # Under XRT the hierarchical path is:
    #   level0_i/level1/level1_i/ulp/ternip_ip_1/inst/axi_ternip_batched_$i
    # Pblock everything below that cell.
    set inst_re [subst -nocommands -nobackslashes \
        {axi_ternip_batched_${i}(/|$)}]
    set inst_cells {}
    foreach cell [get_cells -quiet -hierarchical \
            -filter "NAME =~ */axi_ternip_batched_${i}*"] {
        if {[regexp -- $inst_re $cell]} {
            lappend inst_cells $cell
        }
    }

    # Also pblock the associated per-instance support cells from bd.tcl:
    # axi_dma_$i (instruction DMA), axi_interconnect_ctrl_$i,
    # axi_interconnect_bank_$i. These are bd siblings of axi_ternip_batched_$i.
    foreach pat [list \
            "axi_dma_${i}" \
            "axi_interconnect_ctrl_${i}" \
            "axi_interconnect_bank_${i}"] {
        foreach cell [get_cells -quiet -hierarchical \
                -filter "NAME =~ */${pat}*"] {
            lappend inst_cells $cell
        }
    }

    if {[llength $inst_cells] == 0} {
        puts "ERROR: no cells matched instance $i"
        continue
    }

    set crange [get_clockregion_range_for_slr "SLR${i}"]
    if {$crange eq ""} {
        puts "ERROR: empty clock-region range for SLR${i}"
        continue
    }

    set pb [create_pblock pblock_instance_${i}]
    add_cells_to_pblock $pb $inst_cells
    resize_pblock $pb -add $crange
    set_property IS_SOFT FALSE $pb
    puts "Instance $i: pblock'd [llength $inst_cells] cells to SLR$i ($crange)"
}
```

Wire it up in `synth/pynqvivado_au250/kernel.cfg` (generated by `generate_kernel_cfg.tcl`):

```
prop=run.impl_1.STEPS.PLACE_DESIGN.TCL.PRE=../../../../pynqvivado_au250/pre_place_design.tcl
```

Existing `pre_opt_design.tcl` / `pre_phys_opt_design.tcl` / `post_phys_opt_design.tcl` from main can stay — they're variant-agnostic (rst MAX_FANOUT, false-path between unrelated FUs). The CLAUDE.md tactics targeting `out_fifo_wr_ptr_reg` and `tmatmul_dma` per-bank can be DROPPED — those were for NTB's cross-SLR pressure.

---

## AXI Interconnect sizing — PG059 numbers + bd.tcl inventory

Each `axi_ternip_batched_$i` instance is wrapped by three `axi_interconnect:2.1` instances per `synth/pynqvivado_au250/bd.tcl`. The interconnect is BIG (each NUM_MI slot adds a full crossbar slice plus per-channel register slices). PG059 Table 2-2 (AXI4 SAMD Crossbar Performance, Kintex UltraScale Speed Grade -2, 10% guardband applied — closest reference point for AU250's `xcu250-figd2104-2L-e`) gives the achievable frequency per (NUM_SI × NUM_MI) shape at 512-bit data width:

| Shape          | 64b MHz | 512b MHz | Notes |
|---|---:|---:|---|
| 1 SI × 4 MI    | 400 | 370 | Comfortable headroom over 300 MHz |
| 3 SI × 1 MI    | 360 | 360 | Comfortable headroom |
| 4 SI × 4 MI    | 320 | 320 | Marginal — DO NOT GROW past 4×4 |
| 8 SI × 4 MI    | 345 | 335 | Worse — same caveat |
| 16 SI × 4 MI   | 270 | 235 | Way below 300 MHz |

### What `bd.tcl` instantiates today

| Cell | Shape | Data width | Achievable MHz (Table 2-2) | Notes |
|---|---|---:|---:|---|
| `axi_interconnect_1`            | 1 SI / N MI    | 32 (instruction-fetch) | 400 | Distributes S_AXI control from XRT shell to N instances. **Crosses SLRs** — its N MI outputs land in N different SLRs. |
| `axi_interconnect_ctrl_$i`      | 1 SI / 4 MI    | 32 (control) | 400 | Per-instance, intra-SLR. Splits S_AXI control into the 4 control buses (axi_dma's S_AXI_LITE, s_axi_stall, s_axi_rst, s_axi_debug). |
| `axi_interconnect_bank_$i`      | 3 SI / 1 MI    | 512 (data)   | 360 | Per-instance, intra-SLR. Merges DMA + loadstore + tmatmul into one DDR bank's M_AXI. |
| `axi_interconnect_ddr_$d`       | n_axi SI / 1 MI | 512 (data)   | varies | ONLY when multiple AXI instances share a DDR bank (N > 4). At N=4 this isn't instantiated. |

All shapes above are at-or-above 300 MHz **if they stay in one SLR**. The unique cross-SLR risk is `axi_interconnect_1`: its 4 master outputs fan out to 4 different SLRs (instances 0..3), so its M-side register slices are the natural SLR-crossing point.

### SLR-crossing register slice modes (PG059 page 18)

The AXI Register Slice IP has these modes per channel (REG_AW / REG_AR / REG_W / REG_R / REG_B):

| Mode | Latency | Bubble cycles | Use case |
|---|---:|---:|---|
| `Bypass`              | 0 | n/a | Direct wire |
| `Light`               | 1 | 1 | **Default**. 50% bandwidth, low area. Wrong for high-throughput data paths. |
| `Full`                | 1 | 0 | 100% bandwidth, 2-deep FIFO buffer |
| `SI_Reg` / `MI_Reg`   | 1 | 0 | Single-side register |
| `SLR Crossing`        | 3 | 0 | **For SLR-spanning nets**. 3-cycle, full bandwidth. |
| `SLR TDM Crossing`    | 3 | 0 | Same as above with time-domain multiplexing for shared LAGUNA |
| `Multi SLR Crossing`  | 1-17 | 0 | Crosses >1 SLR. Latency = (#SLR boundaries crossed) × (#pipeline stages per SLR region) |

For NSAI on AU250:
- Each `axi_ternip_batched_$i` is fully intra-SLR. None of its INTERNAL paths cross SLRs.
- The DDR controller IP for `DDR[$i]` is in SLR `$i` too (per the AU250 platform). So `m_axi_tmatmul`, `m_axi_loadstore`, and the data-side `axi_interconnect_bank_$i` are all SLR-local at instance `$i`. **No SLR-crossing register slice needed on these paths.**
- `axi_interconnect_1`'s M outputs DO cross SLRs (one output → SLR 0, another → SLR 1, etc.). These are the 32-bit control buses, so the cross-SLR pressure is small, but should still be configured for SLR crossing rather than Light mode.

### bd.tcl change: replace `axi_interconnect_1` with N AXI register slices in SLR Crossing mode

`axi_interconnect_1` is a single 1 SI / 4 MI interconnect whose 4 master outputs fan out to 4 different SLRs — it IS the cross-SLR distributor. Per PG059 page 18, the AXI Register Slice has dedicated `SLR Crossing` modes (3 cycles, no bubbles, designed for SSI LAGUNA hops) that the bare interconnect's default register slices don't use.

**On the `NumSeparateAxiInstances` branch, modify `synth/pynqvivado_au250/bd.tcl`**: delete `axi_interconnect_1` and replace with N independent `axi_register_slice:2.1` instances, one per AXI instance:

```tcl
# Per-instance SLR-crossing register slices (replaces axi_interconnect_1).
# Each slice carries the 32-bit S_AXI control bus from the XRT shell (SLR1)
# to its instance's local axi_interconnect_ctrl_$i (SLR $i). The
# SLR Crossing mode is 3-cycle latency at full bandwidth (PG059 p18).
for {set i 0} {$i < $num_separate_axi_instances} {incr i} {
    set rs_ctrl_$i [ create_bd_cell -type ip \
        -vlnv xilinx.com:ip:axi_register_slice:2.1 rs_ctrl_$i ]
    set_property -dict [list \
        CONFIG.ADDR_WIDTH {16} \
        CONFIG.DATA_WIDTH $instruction_fetch_width \
        CONFIG.PROTOCOL {AXI4LITE} \
        CONFIG.REG_AW {SLR Crossing} \
        CONFIG.REG_AR {SLR Crossing} \
        CONFIG.REG_W  {SLR Crossing} \
        CONFIG.REG_R  {SLR Crossing} \
        CONFIG.REG_B  {SLR Crossing} \
    ] [get_bd_cells rs_ctrl_$i]
}

# Wire: S_AXI -> N parallel rs_ctrl_$i -> their downstream interconnect_ctrl_$i.
# (Replaces the now-removed axi_interconnect_1 fanout.)
for {set i 0} {$i < $num_separate_axi_instances} {incr i} {
    connect_bd_intf_net [get_bd_intf_ports S_AXI] \
        [get_bd_intf_pins rs_ctrl_$i/S_AXI]
    connect_bd_intf_net [get_bd_intf_pins rs_ctrl_$i/M_AXI] \
        [get_bd_intf_pins axi_interconnect_ctrl_$i/S00_AXI]
}
```

Each `rs_ctrl_$i` is then included in the corresponding `pblock_instance_$i` (add it to the `inst_cells` list in the floorplan TCL). After place_design, the register slice lives in SLR `$i`, its S-side pin connects via a single LAGUNA hop back to SLR1 (the S_AXI port's natural location), and the IP's `SLR Crossing` mode pipelines that hop at full bandwidth.

### Interconnects that stay as-is

- `axi_interconnect_ctrl_$i` (1 SI / 4 MI, 32-bit): stays. Intra-SLR. 400 MHz on Kintex UltraScale.
- `axi_interconnect_bank_$i` (3 SI / 1 MI, 512-bit): stays. Intra-SLR. 360 MHz worst case.
- `axi_interconnect_ddr_$d`: only instantiated at N>4; not relevant since N=4 is the hard rule.

### Don't increase `NumSeparateAxiInstances` beyond 4

At N>4, multiple instances start sharing a DDR bank via `axi_interconnect_ddr_$d` — an extra n-to-1 merge on the highest-bandwidth path. Per Table 2-2 an 8×4 SAMD at 512b runs at 245 MHz, below the 300 MHz target. The hard rule above is grounded in this number.

---

## Verification gates (run before every build)

Same six gates as CLAUDE.md, with the cocotb caveat below:

```bash
cd ternary_matmul
make lint    CONFIG=xcu250_D=1024_MaxCores
make sim TOP=tmatmul_tb SIMULATOR=verilator CONFIG=xcu250_D=1024_MaxCores
make sim TOP=tmatmul_tb SIMULATOR=vcs       CONFIG=xcu250_D=1024_MaxCores
make sim TOP=rms_tb     SIMULATOR=verilator CONFIG=xcu250_D=1024_MaxCores
make sim TOP=rms_tb     SIMULATOR=vcs       CONFIG=xcu250_D=1024_MaxCores
( cd dv/cocotb/axi_ternip_batched && make SIM=verilator CONFIG=xcu250_D=1024_MaxCores )
```

**Cocotb adaptation**: the existing cocotb test (`dv/cocotb/axi_ternip_batched`) was written for the NTB variant's per-bank `m_axi_tmatmul_<b>` hierarchy. On NSAI main, `axi_ternip_batched` has just `m_axi_tmatmul` + `m_axi_loadstore` (no per-bank suffix), so the test's per-bank tmatmul-import smoke loop fails at compile.

**Decision**: adapt the existing test in-place on the `NumSeparateAxiInstances` branch (don't create a `_nsai` sibling). Strip out the per-bank loop and replace with a single `m_axi_tmatmul` + `m_axi_loadstore` exercise. The AXI-Lite control and descriptor-channel scaffolding is reusable. **This adaptation must complete and pass before NumSeparateAxiInstances_1 kicks** — the CLAUDE.md "cocotb-before-every-build" rule is non-negotiable because the in-tree SV TBs stub the top-level AXI ports.

Estimated effort: 30-60 min on the cocotb edits, included as part of the `NumSeparateAxiInstances_1` preparation (along with the bd.tcl + pre_place_design.tcl changes).

---

## hw_emu pass criterion

Identical to CLAUDE.md — first layer's output is the real pass signal; the comparator's `FAILED!` is expected for downstream layers.

---

## Target frequency

300 MHz. Same as CLAUDE.md. With per-instance SLR localization, AUTO-FREQ-SCALING-04 should fire much less often than on NTB. If it still fires, the `skipTimingCheckAndFrequencyScaling=1` kernel.cfg knob is available, but in NSAI it should be a fallback, not a routine setting.

---

## Loop cadence

Same as CLAUDE.md. One change per iteration, name iterations `YYYY.MM.DD-HHMM`, stage artifacts to `artifacts/<datecode>/` before kicking the next build (no race), update the release body with timing CSV + WNS/TNS table + utilization. ETA polling via `scripts/eta.sh`. `scripts/run_build.sh` for the kick.

The release-table column conventions from CLAUDE.md apply unchanged. **Tag NSAI releases with the variant** in the title (e.g. `[XRT NSAI, MaxCores]`) so they don't get confused with NTB releases on the other branch.

---

## Initial iterations (pick from the top)

Iterations are named `NumSeparateAxiInstances_N` (not `build_N`) to keep the NTB build_NN release history clearly distinct.

1. **NumSeparateAxiInstances_1 — bundled baseline**: NSAI main + the new `pre_place_design.tcl` + the bd.tcl edit (replacing `axi_interconnect_1` with N `axi_register_slice:2.1` instances in `SLR Crossing` mode) + `CoreInterconnectNumStages=4` (down from 8). BatchSize=1, TP=256, VP=4, LP=1, NVR=4. Before kicking, run `report_instruction_timing.py` on the candidate config and record the projected multicore tokens/sec in the release body as the success target (pass criterion: achieved ≥ 90% of projected). Expected: WNS positive, multicore tokens/sec ≈ 4 × singlecore. If WNS doesn't close cleanly, the issue is either floorplan or NumStages too aggressive — bisect by reverting one half on iteration 2.

2. **NumSeparateAxiInstances_2 — `BatchSize=4`**: 4× the per-instance core replication. Should be the easiest tokens/sec quadrupling we've ever seen on this design — no new cross-instance routing pressure, each new core lives in the same SLR as its instance's other cores.

3. **NumSeparateAxiInstances_3 — `BatchSize=8`** (or higher, depending on area). Keep ramping until utilization hits ~70%.

4. **NumSeparateAxiInstances_N — `CoreInterconnectNumStages=2`**: once the design is comfortable at 4, push to 2. Beyond that, intra-SLR routing may not have enough setup margin even without LAGUNA crossings.

5. **Once BS is maxed out**: try `VectorParallelism=8` or `LutParallelism=2` to drive `cycle_counter` down. Trade-off per `report_instruction_timing.py`.

The `TO-TRY.md` mechanism from CLAUDE.md applies — drain User-Generated first, then Claude-Generated. May want a fresh `TO-TRY.md` on this branch (separate from the NTB branch's history of what worked / didn't).

---

## Things from CLAUDE.md that DON'T carry over

- The `out_fifo_wr_ptr_reg` MAX_FANOUT cluster diagnosis — that was NTB's cross-SLR-route bottleneck. NSAI per-instance SLR pinning eliminates it.
- The TmatmulParallelism=128 lesson — NTB-specific. NSAI baseline uses TP=256.
- The "pblock-only is exhausted" failures from build_7/_8/_9 in CLAUDE.md — those targeted `tmatmul_dma[b]` per-bank pinning across the SAME core. NSAI pblocks target whole-instance cells in different SLRs; the structural shape is fundamentally different.
- The R-channel pipelined slice refactor (build_33) — was tuned for NTB's cross-SLR tmatmul broadcast. NSAI has no such broadcast.

---

## Things from CLAUDE.md that DO carry over verbatim

- Operational hygiene rules (never rerun a successful build on the same RTL, never bundle ideas, don't poll faster than 60s).
- Tokens/sec optimization framing — maximize `clk_freq × BatchSize × NumSeparateAxiInstances / cycle_counter`.
- Release body format + mandatory line items (timing, tokens/sec before/after, utilization with the scaling-multiplier output).
- Build invocation pattern on eq2 (double-ssh, `disown`, `scripts/run_build.sh`).
- Polling cadence (5-15 min, never faster than 60s).
- Error handling (Vivado segfault → rerun; XRT cosmetic libxrt_core error → ignore; AUTO-FREQ-SCALING-04 → treat as didn't pass; VPL 18-1000 twice → bisect, don't blame Vivado).
- Style + code rules (every module has ready/valid; every FF has `_d`/`_q`; never use `(* dont_touch *)`; tool-specific attrs in TCL/XDC not RTL).
- hw_emu pass criterion (first-layer output, not the comparator).
- "Always keep eq2 building — never give up" — applies just as hard.
- The `make vivado` (OOC, kernel-only) rapid-iteration flow for prototyping. Same caveats (under-reports DDR-side routing pressure, over-reports area).

---

## Decisions confirmed (locked-in before iteration starts)

- **N is hard-locked at 4.** PG059 Table 2-2 confirms 8×4 SAMD @ 512b falls to 245 MHz; no point trying N>4.
- **`axi_interconnect_1` is replaced** with N AXI register slices in `SLR Crossing` mode (see "AXI Interconnect sizing" above). bd.tcl is edited directly on the `NumSeparateAxiInstances` branch.
- **TP=256 is hard-locked** (matches NSAI main).
- **`CoreInterconnectNumStages=4` lands in NumSeparateAxiInstances_1** — bundled with the floorplan. If iteration 1 fails, bisect by reverting either the floorplan or the NumStages change individually on iteration 2.
- **Tokens/sec target is computed before NumSeparateAxiInstances_1 kicks** via `report_instruction_timing.py`, recorded in the release body as the success benchmark (pass = achieved ≥ 90% of projected).
- **Cocotb test is adapted in-place** (not a new sibling): edit `dv/cocotb/axi_ternip_batched/` on the `NumSeparateAxiInstances` branch to use the unsuffixed `m_axi_tmatmul` + `m_axi_loadstore` port surface. Must pass before NumSeparateAxiInstances_1 kicks.
- **Outer `CLAUDE.md` gets a "Variant docs" section** added at the top, pointing readers to `NumTmatmulBanksPerCore.md` (NTB, currently churned) and `NumSeparateAxiInstances.md` (NSAI, this doc). Done once at branch-setup time.

## Open questions (genuinely uncertain — log here, don't block)

1. **Cocotb test adaptation**: the existing `dv/cocotb/axi_ternip_batched` was written for NTB's per-bank `m_axi_tmatmul_<b>` hierarchy. NSAI's RTL has just `m_axi_tmatmul` + `m_axi_loadstore`. Two options: (a) adapt the existing test to NSAI's port surface; (b) write a fresh `_nsai_tb`. The CLAUDE.md "cocotb-before-every-build" rule is non-negotiable — the SV TBs stub out the top-level AXI surface, so we need *some* AXI-surface gate working before we kick NumSeparateAxiInstances_1.
2. **Initial pblock granularity**: pblock just `axi_ternip_batched_$i` + sibling bd cells (current plan), OR also include any platform-side AXI plumbing in the same pblock for tighter intra-instance routing? Default until we know more: current plan (cells listed in the TCL).
3. **NumSeparateAxiInstances_1 sanity-check rubric**: if baseline doesn't close timing, the likely culprit is either (a) the pblock is too tight (over-constrains placement), or (b) NumStages=4 is too aggressive for some intra-SLR-but-LUT-heavy path. `report_design_analysis -congestion` on the routed DCP should disambiguate. If it's (a), loosen the pblock to include adjacent clock regions; if (b), bump NumStages back to 6.
4. **Register-slice mode for kernel `m_axi_*` ports**: the XRT shell's interconnect (outside the kernel) handles the cross-SLR routing from the kernel's M_AXI ports back to DDR. The current plan leaves it to the shell. If timing on the M_AXI side becomes the blocker in any iteration, the fix is adding `SLR Crossing` register slices on the kernel-side `m_axi_tmatmul`/`m_axi_loadstore` ports — but only after we have data showing those are failing.
