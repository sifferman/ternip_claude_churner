# AUDIT_NSAI.md — NumSeparateAxiInstances variant

Commit audited: d6a5491 (main branch of ternary_matmul_claude)
Date: 2026-06-05

## Architectural summary (read this first)

In NSAI, `NumSeparateAxiInstances = N` (=4 for MaxCores). The Vitis
block-design (synth/pynqvivado_au250/bd.tcl) instantiates **N copies of
`axi_ternip_batched`** as separate Vitis kernels. Each top-level instance
`axi_ternip_batched_$i`:

- has its OWN `m_axi_tmatmul` and `m_axi_loadstore` (each 512-bit DdrDataWidth)
- has its OWN Xilinx `axi_dma:7.1` (`axi_dma_$i`) that fetches instructions
  from DDR and drives a `s_axis_instruction` AXI-stream port at
  `InstrFetchWidth=32` bits
- internally contains 1× `ternip_batched`, which itself contains
  `BatchSize` copies of `ternip_buffered` → `ternip_core`. Each
  ternip_core has 1× tmatmul, 1× rms, 1× rowwise_op, 1× loadstore,
  1× vector_registers. There is NO column-slice (no per-bank
  tmatmul_units inside one core); the entire tmatmul sees
  `ImportVectorLength = D`.

That means for NSAI with NumDdrBanksUsed=N and BatchSize=BS, the
**total core count is N × BS** (each AXI instance hosts BS independent
cores running in lockstep on different loadstore data but the same
ternary stream). The tmatmul DMA in instance i broadcasts its R-data to
ALL BS cores within that instance.

The MaxCores config (`xcu250_D=1024_MaxCores.svh`) sets
`TmatmulParallelism=256`, `VectorParallelism=4`, `BatchSize=1`,
`NumSeparateAxiInstances=4`, `FixedPointPrecision=16`, `D=1024`,
`NumVectorRegisters=4`, `DdrDataWidth=512`, `InstructionWidth=128`.

The audit task asks for **TP=128**, VP=4, FxP=16, D=1024, NumDdrBanksUsed=4
(the visualizer's `_DEFAULTS`). I evaluate widths for those defaults
and call out where the formula breaks at other TP values.

For TP=128, D=1024:
- `RowParallelism = max(1, TP/D) = max(1, 0) = 1` (Verilog: `(TP<D) ? 1 : TP/D = 1`)
- `ImportVectorRowWidth = min(TP, D) = 128`
- `tmul_result_t` = signed `[FxP : 0]` = **17 bits** (FxP+1)
- `fixed_point_t` = signed `[FxP-1 : 0]` = **16 bits**
- `vector_chunk_t = fixed_point_t [VP-1:0]` = **64 bits**
- `tmatmul_stream_data_t = ternary_t [TP-1:0]` = **256 bits** (TP×2)
- `instruction_t` = packed struct, **128 bits** (=InstructionWidth)
- `ddr_address_t` = **64 bits**

## Nodes

### Nodes the visualizer creates but RTL doesn't have

1. **`instruction_decode_i{i}`** (one per AXI instance). There is no
   module named `instruction_decode` in NSAI. The "decoder" is an inline
   `always_comb` `case (instruction_i.fu)` block inside
   `ternip_core.sv` (lines 461–518). The actual on-chip components
   between the AXI-stream input and the core are:

   - `ternip_gearbox_fifo gbfifo_instruction` (width-converts 32→128)
     in `axi_ternip_batched.sv` (line 178)
   - `ternip_pipelined_interconnect buffer_instruction` (NumStages=8
     pipeline registers, in `ternip_buffered.sv` line 107)

   The visualizer's `instruction_decode` node should be relabeled
   `gbfifo_instruction` (or merged with the buffer pipeline) to reflect
   the actual gate-level content.

2. **`importvector_i{i}_c{c}`**, **`exportvector_i{i}_c{c}`** as
   standalone nodes. There is no `ternip_importvector.sv` or
   `ternip_exportvector.sv` file in the tree (verified by
   `ls .../rtl/fus/`). The "importvector" is the
   `ternip_pipelined_mem importvector` instance INSIDE
   `ternip_tmatmul.sv` (line 211), and "exportvector" is similarly
   `ternip_pipelined_mem exportvector` (line 242). These are legitimate
   logical nodes — the model just shouldn't suggest they're separable
   functional units. (This is more a documentation issue than a
   topology bug.)

3. **`MOA_i{i}_c{c}`** as a single node per core. In RTL there are
   actually `RowParallelism` MOAs per core (a `for (genvar)`
   loop in `ternip_tmatmul.sv` line 175). For TP≤D, RowParallelism=1 so
   one node-per-core matches; for TP>D (e.g. TP=2048, D=1024 → 2 MOAs),
   the visualizer under-models. Not a problem at the audit's TP=128 but
   worth flagging.

4. **`axi_dma_instr_i{i}`** as a model node. This DOES correspond to a
   real BD-level IP (`axi_dma:7.1` instance `axi_dma_$i` from
   `bd.tcl` line 128), but it lives in the Vitis shell-side block
   design, not inside `axi_ternip_batched.sv`. The visualizer is right
   to include it; just be aware the cells are not counted in any
   kernel-side utilization report.

### Nodes RTL has but visualizer doesn't model

1. **`ternip_pipelined_interconnect`** wrappers (one per ternip_buffered
   bus, 6 buses × `CoreInterconnectNumStages=8` = ~48 pipeline
   register stages per AXI instance). These are SLR-crossing pipeline
   registers in `ternip_buffered.sv` lines 107–215 (instruction,
   loadstore_ddr_stream, loadstore_ddr_r, loadstore_ddr_w,
   loadstore_ddr_debug, tmatmul_ddr_stream, tmatmul_ddr_r). They sit
   between the AXI surface and ternip_core. They're a non-trivial FF
   mass and exist to convert N-long wires into N FF→FF hops for timing.
   The visualizer ignores them entirely. For an architecture
   visualization that highlights SLR crossings, omitting these is the
   biggest single missing nodeset.

2. **`gbfifo_instruction`** / **`gbfifo_tmatmul`** /
   **`gbfifo_loadstore_r`** / **`gbfifo_loadstore_w`**. These four
   `ternip_gearbox_fifo` instances live in `axi_ternip_batched.sv`
   (lines 178, 209, 334, 371). They handle width conversion between
   DdrDataWidth (512) on the DMA side and the kernel-internal widths
   (TP×2 for tmatmul, VP×FxP×BS for loadstore, IW for instruction).
   The visualizer's `tmatmul_dma` cell-estimate formula
   (`DW×4 + TP×8`) implicitly bundles axi_dma_rd + gbfifo_tmatmul, but
   the loadstore-side gearboxes and the instruction gearbox are not
   represented at all.

3. **`s_axi_ternip_rst`** (lines 120–139 of axi_ternip_batched.sv),
   **`s_axi_ternip_const_rd`** (×2 instances: stall + debug),
   **`s_axi_ternip_write_byte`** (stall write). These are small AXI-Lite
   adapters for control. Probably fine to omit from a high-level
   architecture diagram, but they exist.

4. **`axi_dma`** (alexforencich) `dma_rw_loadstore` instance (line 388
   of axi_ternip_batched.sv). The visualizer draws a single
   bidirectional `loadstore_c{c} <-> dram_b{i}` edge but does not
   model the DMA itself. (Counterpart to `dma_r_tmatmul` which IS
   represented by `tmatmul_dma`.)

5. **Per-instance debug bus**: `loadstore_ddr_debug_o` is a 64-bit
   output from each core, gathered by ternip_batched and exposed on
   axi_ternip_batched's debug AXI port. Probably fine to omit.

6. **Stall/clear 1-bit interconnects**: `stall_active_o`,
   `stall_clear_i` flow between every core and the top, plus a
   `s_axi_stall_*` AXI port. These are control signals, not data
   buses; ignoring them is fine.

### Nodes that exist in both but with wrong cardinality

1. **`tmatmul_dma_i{i}`**: 1 per AXI instance i ∈ [0..N). Matches RTL
   (1× `axi_dma_rd` + 1× `gbfifo_tmatmul` per `axi_ternip_batched_$i`). ✓

2. **`loadstore` (`ls_id`)**: Visualizer creates N×BS loadstore nodes
   (`loadstore_i{i}_c{c}` for each i, c). Matches RTL — each core
   contains its own `ternip_loadstore` instance via `ternip_core.sv`
   line 164. ✓

3. **`rms`, `rowwise_op`, `vector_registers`**: Same N×BS cardinality.
   Each ternip_core has its own. ✓

4. **`MOA`, `importvector`, `exportvector`**: Visualizer creates N×BS
   of each. Each ternip_core has 1× `ternip_tmatmul`, which contains
   RowParallelism (=1 at TP=128/256, D=1024) MOAs + 1 IV + 1 EV. So
   cardinality matches for the TP≤D regime. For TP>D (e.g. TP=2048,
   D=1024), RowParallelism=2 and there should be 2 MOAs per
   ternip_core — visualizer would under-count.

5. **`xrt_shell`**: Visualizer creates one. Matches reality (there is
   one XRT platform per FPGA). ✓

## Edges

Edges grouped by region for the NSAI variant. The visualizer's
`_build_NumSeparateAxiInstances()` body lives in
`av_lib/topology.py` lines 115–292.

### DRAM/AXI surface

#### Edge: `dram_b{i} -> tmatmul_dma_i{i}`

- **RTL signals**: `m_axi_tmatmul_rdata` (input port of
  `axi_ternip_batched.sv` line 21) drives `axi_dma_rd dma_r_tmatmul`'s
  `m_axi_rdata` (line 284). On the BD side, this is the DDR R-channel.
- **Correct bus_bits**: `DdrDataWidth` = 512.
- **Visualizer formula**: `dw` = `DdrDataWidth`. **Matches**.

#### Missing edge: `dram_b{i} -> axi_dma_instr_i{i}` (instruction fetch)

- **RTL**: `axi_dma_$i/M_AXI_MM2S` is connected to
  `axi_interconnect_bank_$i/S00_AXI` (bd.tcl line 216), which feeds
  the same DDR bank `M_AXI_$d` the loadstore and tmatmul also use.
  This is the path by which instructions are fetched from DDR.
- **Bus width**: `DdrDataWidth` = 512.
- **Visualizer**: NO edge. The `xrt_shell -> instruction_decode_i{i}`
  edge is the closest analogue but mis-represents the path —
  instructions live in DDR, the host (via XRT) just pre-loads them
  there once.

#### Missing edge: `dram_b{i} <-> loadstore` should be ONE edge per AXI instance, not BS edges

- **RTL**: There's a single `m_axi_loadstore_*` port per AXI instance
  (lines 27–61 of axi_ternip_batched.sv). The N×BS cores within one
  instance share one DDR connection through the gearbox FIFOs (which
  pack/unpack the BS-wide data).
- **Visualizer**: emits an edge for every (core c in [0..BS)) from
  `ls_id` to `dram_id`. With BS=1 this is correct cardinality but with
  BS>1 the visualizer would over-count by a factor of BS at the DRAM
  port (the bus is BS-packed, not BS-replicated).
- **Bus width**: At the AXI surface, `DdrDataWidth` = 512 (single bus).
  At the per-core interface inside ternip_batched,
  `vector_chunk_t = VP×FxP` = 64 bits, but this is per-core internal,
  not a DRAM-side signal. The visualizer uses `dw=512` per core which
  effectively says "every core has its own 512-bit AXI bus to DDR" —
  not true at the AXI surface, but does ~match the gearboxed total bus.

### Per-AXI-instance instruction/control

#### Edge: `axi_dma_instr_i{i} -> instruction_decode_i{i}`

- **Visualizer formula**: `iw` = `InstructionWidth` = 128.
- **RTL signals**: The path is
  `axi_dma_$i.M_AXIS_MM2S` (32-bit, `InstrFetchWidth`)
  → `axi_ternip_batched.s_axis_instruction_tdata` (32-bit)
  → `gbfifo_instruction` width converter
  → `instruction_i` (128-bit, `InstructionWidth`)
  → buffer pipeline → ternip_core.
- **Correct bus_bits**: AT THE AXIS STREAM = `InstrFetchWidth` = 32.
  AFTER gearbox = `InstructionWidth` = 128. The visualizer treats this
  as a single edge with width 128, which is fine if the "instruction
  decode" node logically sits AFTER the gearbox. Just be aware the
  physical port between axi_dma and axi_ternip_batched is 32 bits.
- **DISCREPANCY (mild)**: The label is unclear about which side of
  the gearbox the width refers to.

#### Edge: `instruction_decode_i{i} -> ternip_core_i{i}_c{c}` (×BS)

- **RTL signal**: `core_instruction_i[c]` and `core_instruction_valid_i[c]`,
  driven by `instruction_i` and `instruction_valid_i` for every c
  (ternip_batched.sv lines 147–152). Width = `$bits(instruction_t)` =
  `InstructionWidth` = 128.
- **Visualizer formula**: `iw` = 128. **Matches**.
- **Note**: There's one additional pipeline stage between
  `instruction_decode_i` (the gearbox FIFO output) and each ternip_core:
  the `ternip_pipelined_interconnect buffer_instruction` in
  ternip_buffered.sv. Not modeled; would be 128-bit wide with
  CoreInterconnectNumStages=8 register stages.

#### Edge: `xrt_shell -> instruction_decode_i{i}`

- **Visualizer formula**: `iw` = `InstructionWidth` = 128.
- **Label says**: "InstrFetchWidth (instructions from host via XRT)".
- **Actual RTL**: There is NO direct wire from the XRT shell to the
  instruction gearbox. The XRT shell writes instructions to DDR, and
  the kernel-side `axi_dma_instr` later reads them out. The XRT shell
  DOES drive a small AXI-Lite control bus to `axi_dma_$i/S_AXI_LITE`
  (bd.tcl line 201) to kick off DMA transfers; that bus is ~32 bits
  wide, not 128.
- **DISCREPANCY**: The edge is conceptually misleading. Better
  alternatives:
  - Replace with `dram_b{i} -> axi_dma_instr_i{i}` at width
    `DdrDataWidth=512` (the bulk path), plus
  - `xrt_shell -> axi_dma_instr_i{i}` at AXI-Lite width (~32) for the
    control kick.
  - Also the label `iw=InstrFetchWidth` doesn't match the value
    `iw=InstructionWidth` — InstrFetchWidth is 32, InstructionWidth
    is 128. Two different constants conflated.

### tmatmul subsystem (tmatmul_dma → MOA path, per core)

For each (i, c) ∈ [0..N) × [0..BS), the visualizer emits:

#### Edge: `tmatmul_dma_i{i} -> moa_i{i}_c{c}`

- **RTL signals**: `gbfifo_tmatmul_out_data` (line 202 of
  axi_ternip_batched.sv, type `ternary_t [TmatmulParallelism-1:0]`,
  width TP×2 = 256 at TP=128) → `tmatmul_ddr_r_data_i` (input to
  ternip_batched/ternip_buffered/ternip_core/ternip_tmatmul) →
  feeds the `accumulator_operands` inputs of the MOA via the
  combinational `ternary_mul()` in tmatmul.sv line 446–448.
- **Correct bus_bits**: `TP×2`. At TP=128 → **256 bits**.
- **Visualizer formula**: `tp * 2`. **Matches** numerically.
- **Note**: The broadcast (1 tmatmul_dma → BS cores' MOAs in instance i)
  is correctly drawn as BS separate edges. RTL line ternip_batched.sv
  213–214 confirms broadcast assignment
  `core_tmatmul_ddr_r_data_i[c] = tmatmul_ddr_r_data_i` for all c.
- **Note**: The visualizer's edge bypasses the
  `ternip_pipelined_interconnect buffer_tmatmul_ddr_r` (CoreInterconnect-
  NumStages-deep). This is fine if "edge = logical net"; not fine if
  "edge = single-hop physical wire."

#### Edge: `iv_i{i}_c{c} -> moa_i{i}_c{c}`

- **RTL signals**: `importvector_read_data` (type
  `fixed_point_t [ImportVectorRowWidth-1:0]`, width
  `IVRW×FxP` = min(TP,D) × FxP = 128×16 = **2048 bits** at TP=128)
  feeds the `ternary_mul(ddr_r_data_i[i], importvector_read_data[i % D])`
  inputs at tmatmul.sv line 447.
- **Correct bus_bits**: `min(TP, D) × FxP`. At TP=128, D=1024 →
  128 × 16 = **2048**.
- **Visualizer formula**: `tp * fxp` = 128×16 = 2048. **Matches**
  numerically for TP≤D. For TP>D the visualizer over-estimates: it
  would produce 2048×16=32768 for TP=2048, but RTL is 1024×16=16384.
- **DISCREPANCY (only at TP>D)**: formula should be `min(TP, D) × FxP`,
  not `TP × FxP`.

#### Edge: `moa_i{i}_c{c} -> ternip_core_i{i}_c{c}`

- **RTL signals**: `accumulator_result` (type
  `fixed_point_t [RowParallelism-1:0]`, width
  `RowParallelism × FxP` = 1×16 = **16 bits** at TP=128) → goes
  into `gbfifo_export_in_data` (same width) — this is INTERNAL to
  ternip_tmatmul, not exposed to ternip_core. Looking at the ports of
  ternip_tmatmul, the only "result" path back to ternip_core is via
  `vector_request_w_data_o` (=`vector_chunk_t` = **VP×FxP** =
  64 bits, the channel by which EXPORT writes the result vector
  register chunks back to vector_registers).
- **Correct bus_bits**: There's no direct MOA→ternip_core wire. The
  "MOA result" path is MOA→gbfifo_export→exportvector→vector_request
  output of tmatmul (`vector_chunk_t` = 64 bits) → into ternip_core's
  arbiter → into vector_registers.
- **Visualizer formula**: `moa_out_bits = row_parallelism * fxp` = 16.
  **DISCREPANCY**: This is the right value for the INTERNAL MOA→
  gbfifo_export wire but not for anything that crosses out of
  ternip_tmatmul. If "edge = data flow", the bus that physically
  leaves the MOA toward higher-level logic eventually reaches
  vector_registers at VP×FxP = 64 bits. The model is choosing a
  somewhat arbitrary level of detail.

#### Edge: `moa_i{i}_c{c} -> ev_i{i}_c{c}`

- **RTL signals**: `accumulator_result` (RowParallelism × FxP = 16 bits)
  → `gbfifo_export_in_data` → (gearbox FIFO converts to vector_chunk_t)
  → `gbfifo_export_out_data` (VP × FxP = 64 bits) →
  `exportvector_request_w_data` (= 64 bits, the write input of
  exportvector pipelined_mem).
- **Correct bus_bits**: Depends on which side. MOA-side = 16,
  exportvector-side = 64.
- **Visualizer formula**: `moa_out_bits = row_parallelism * fxp` = 16.
  Picks the MOA side.
- **DISCREPANCY (interpretive)**: There's a gearbox FIFO between the
  two; the bus width changes mid-path. The visualizer should either
  pick a specific side and document it, or model the gearbox as a
  separate node.

### vector_registers <-> FUs (per core)

#### Edge: `ternip_core_i{i}_c{c} <-> rms_i{i}_c{c}`

- **RTL signals**: In `ternip_core.sv` (lines 276–318), the FU port
  signals are:
  - `rms_in_*` (the instruction-side input): small (rms_op_e + 2×
    vector_select_t + immediate_t = ~24 bits at typical settings).
  - `vector_request_*` (RMS → vector_registers arbitration): valid+
    write_not_read + vector_select_t + vector_offset_t +
    `vector_chunk_t` = `VP×FxP` = 64 bits dominant component.
  - `vector_read_*` (vector_registers → RMS): `vector_chunk_t` = 64
    bits.
- **Correct bus_bits**: ~`VP × FxP` = 64 for the dominant data
  channel; the control bits add ~tens.
- **Visualizer formula**: `vp * fxp` = 64. **Matches** (dominant
  channel).
- **Note**: The edge is drawn as a single undirected `ternip_core -
  rms` link but really represents the full read/write port pair plus
  the instruction-side control input. Fine as an aggregate.

#### Edge: `ternip_core_i{i}_c{c} <-> ls_i{i}_c{c}` (loadstore)

- Same pattern as RMS. `vector_request_*` and `vector_read_*` are
  `VP × FxP` = 64 bits.
- **Visualizer formula**: `vp * fxp` = 64. **Matches**.

#### Edge: `ternip_core_i{i}_c{c} <-> rw_i{i}_c{c}` (rowwise_op)

- Same pattern. `vector_chunk_t = VP × FxP` = 64.
- **Visualizer formula**: `vp * fxp` = 64. **Matches**.

#### Edge: `ternip_core_i{i}_c{c} <-> vr_i{i}_c{c}` (vector_registers)

- **RTL**: ternip_core.sv lines 126–148 — `ternip_vector_registers
  vector_registers` instance. Its request/read interface carries
  `vector_chunk_t = VP × FxP` = 64 bits.
- **Visualizer formula**: `vp * fxp` = 64. **Matches**.
- **Note**: vector_registers is the centralized resource arbitrated
  among RMS/loadstore/rowwise/tmatmul, so logically the edge should be
  vector_registers ↔ each FU directly (not via ternip_core). The
  visualizer routes the bus through `ternip_core` as a "hub" node,
  which is the actual hierarchy in RTL — ternip_core contains the
  arbitration `always_comb` block (lines 399–442). So the edge model
  reflects the hierarchy.

#### Edge: `vr_i{i}_c{c} -> iv_i{i}_c{c}`

- **RTL**: In ternip_tmatmul.sv (IMPORT mode, lines 384–413), the
  flow is `vector_request → vector_registers.read_data_o`
  (`vector_chunk_t`, 64 bits) → tmatmul's `vector_read_data_i` →
  `gbfifo_import_in_data` (64 bits) → gearbox FIFO →
  `gbfifo_import_out_data` (`fixed_point_t [IVRW-1:0]`, 2048 bits at
  TP=128) → `importvector_request_w_data` (2048 bits) → IV pipelined_mem.
- **Correct bus_bits**: VP-side = 64. IVRW-side = 2048. With a gearbox
  FIFO in between.
- **Visualizer formula**: `vp * fxp` = 64. Picks the VP side.
- **Note**: As with MOA→EV, the bus widens via a gearbox FIFO. The
  visualizer picks one side consistently. Mark "VR-side reading."

#### Edge: `ev_i{i}_c{c} -> vr_i{i}_c{c}`

- **RTL**: In ternip_tmatmul.sv (EXPORT mode, lines 476–499), the
  flow is `exportvector_read_data` (`vector_chunk_t`, 64 bits) →
  `vector_request_w_data_o` (`vector_chunk_t`, 64 bits) → vector_registers.
- **Correct bus_bits**: 64 (no gearbox here — exportvector is already
  vector_chunk_t wide).
- **Visualizer formula**: `vp * fxp` = 64. **Matches**.

### Loadstore <-> DRAM

#### Edge: `ls_i{i}_c{c} -> dram_b{i}` (W) and `dram_b{i} -> ls_i{i}_c{c}` (R)

- **RTL**: `dma_rw_loadstore` (an alexforencich `axi_dma`) handles the
  M_AXI side. Its data flows are:
  - W: `loadstore_ddr_w_data_o` (`vector_chunk_t [BS-1:0]`, BS×64 = 64
    at BS=1) → gbfifo_loadstore_w → `m_axi_loadstore_wdata`
    (`DdrDataWidth` = 512 bits).
  - R: `m_axi_loadstore_rdata` (512 bits) → gbfifo_loadstore_r →
    `loadstore_ddr_r_data_i` (`vector_chunk_t [BS-1:0]`, 64 at BS=1).
- **Correct bus_bits**: At the AXI/DRAM surface = 512. At the per-core
  ternip_batched boundary = `VP×FxP` = 64 (per core).
- **Visualizer formula**: `dw` = 512.
- **DISCREPANCY (mild)**: With BS=1 the visualizer's edge correctly
  represents the AXI bus. With BS>1 the visualizer emits BS edges,
  each labeled `dw=512`. RTL has ONE 512-bit bus per AXI instance
  shared by all BS cores (gearboxed). So the visualizer over-states
  the total bus mass at the DRAM side by factor BS for BS>1.

### Missing edges (signals that exist in RTL but no visualizer edge)

1. **`dram_b{i} -> axi_dma_instr_i{i}`** (512 bits). Instruction fetch
   uses the same DDR bank as loadstore + tmatmul. See bd.tcl line 216.
2. **`xrt_shell -> axi_dma_instr_i{i}`** (~32 bits, AXI-Lite). Control
   kick for the DMA descriptors. Visualizer instead draws
   `xrt_shell -> instruction_decode_i{i}` which is conceptually wrong.
3. **`xrt_shell -> dram_b{i}`** (host writes weights, instructions,
   activations to DDR). The visualizer's diagram has no edge between
   XRT and DRAM at all; the host-side DDR loading path is implicit.
4. **Buffer-pipeline edges** within `ternip_buffered`: 6 buses
   (instruction, loadstore_ddr_stream, loadstore_ddr_r/w/debug,
   tmatmul_ddr_stream/r) each with their own pipelined_interconnect.
   If the model included these as nodes, there'd be 6 buses × N
   instances = 24 added pipeline-stage nodes.
5. **loadstore stream descriptors**: `loadstore_ddr_stream_address_o`,
   `_length_o`, `_write_not_read_o` — small (~100-bit) bus from
   loadstore → top → dma_rw_loadstore's descriptor port. Visualizer
   omits, fine.
6. **tmatmul stream descriptors**: `tmatmul_ddr_stream_address_o`,
   `_length_o` — same, ~100 bits, from tmatmul → top → dma_r_tmatmul.
7. **`s_axi_stall`, `s_axi_rst`, `s_axi_debug`** (3 AXI-Lite buses per
   AXI instance from `axi_interconnect_ctrl_$i`). Small control;
   omission fine.
8. **stall_active_o, stall_clear_i** wires between every core and
   axi_ternip_batched. 1-bit each; omission fine.

### Spurious edges (visualizer edges that don't correspond to any RTL signal)

1. **`xrt_shell -> instruction_decode_i{i}`** at `iw=InstructionWidth`.
   No direct wire exists. Instructions flow XRT→DDR (DMA write) →
   axi_dma_instr (DMA read) → ternip kernel. The visualizer collapses
   this path into one fictitious edge. Mild — it's a useful abstraction
   for "instructions come from somewhere outside the kernel" — but the
   width label (`InstrFetchWidth`) doesn't match the value (`iw`).

2. **`moa -> ternip_core`** at `row_parallelism × fxp`. The MOA's
   `accumulator_result` is consumed by `gbfifo_export_in_data` inside
   ternip_tmatmul. There's no MOA→ternip_core wire that crosses the
   tmatmul module boundary. The path to ternip_core (via exportvector
   and vector_request_w_data_o) is `VP × FxP` wide, not
   `row_parallelism × fxp`.

## Cell-count formulas (cell_estimates.py)

Brief sanity check on the magnitude of each formula vs the implied RTL.

### `_est_MOA`: `TP × FxP × ceil(log2(TP))`

- For TP=128, FxP=16: 128 × 16 × 7 = **14,336 cells**.
- RTL: 1 MOA per core (since RowParallelism=1), each with
  NUM_OPERANDS=128 operands of tmul_result_t (FxP+1=17 bits). Adder
  tree depth ~log2(128)=7. Total LUTs ≈ 128 × 17 × 7 ≈ 15,232. Order
  of magnitude matches. ✓
- For TP>D: visualizer formula treats MOA as one giant TP-wide
  reducer; RTL has RowParallelism distinct MOAs each with D operands.
  Total operand width is still TP×FxP but depth is log2(D)=10 not
  log2(TP). Formula slightly over-states depth for TP>D.

### `_est_tmatmul_dma`: `DW×4 + TP×8`

- For DW=512, TP=128: 512×4 + 128×8 = 2048 + 1024 = **3,072 cells**.
- RTL: axi_dma_rd (~1k LUTs of control + FIFOs) +
  ternip_gearbox_fifo (data-width-converter; FIFO depth ~16 × max(DW,
  TP×2) data bits). Order of magnitude (low thousands) matches. ✓

### `_est_importvector`: `(D / NumDdrBanksUsed) × FxP × 2`

- For D=1024, NumDdrBanksUsed=4: 256 × 16 × 2 = **8,192 cells**.
- RTL: `ternip_pipelined_mem` with DATA_WIDTH = IVRW×FxP = 128×16 =
  2048 and NUM_ENTRIES = DdrReadsPerRow = D/TP = 8 (at TP=128).
  Total storage = 2048 × 8 = 16,384 bits. As BRAM-equivalent LUTs (÷9
  the way vector_registers is) → ~1,820. As discrete FFs (one per
  bit) → 16,384.
- **DISCREPANCY**: The formula divides by `NumDdrBanksUsed`, which is
  conceptually wrong for the NSAI variant. In NSAI, each AXI instance
  has its OWN complete D-wide importvector — there is no column-slice
  splitting the IV across banks. Dividing by NumDdrBanksUsed=4 makes
  the per-core IV look 4× smaller than it actually is. The
  visualizer's formula is borrowed from the NumTmatmulBanksPerCore
  variant (where there ARE N column-slice IV instances, each holding
  D/N of the activation) and mis-applied here. For NSAI, the formula
  should drop the `/N` and just be `D × FxP × 2` (or, more accurately,
  IVRW × FxP × DdrReadsPerRow × 2 = D×FxP×2).
- Magnitude off by factor of N=4 in NSAI.

### `_est_exportvector`: `(D / NumDdrBanksUsed) × FxP × 2`

- For D=1024, NumDdrBanksUsed=4: 256 × 16 × 2 = **8,192 cells**.
- RTL: pipelined_mem with DATA_WIDTH = VP×FxP = 64 and NUM_ENTRIES =
  NumChunksPerVector = D/VP = 256 (at VP=4). Total storage = 64 × 256 =
  16,384 bits.
- **DISCREPANCY**: Same as importvector — should NOT divide by
  NumDdrBanksUsed in NSAI. Each AXI instance has its own complete
  D-wide exportvector. Formula off by factor of N.

### `_est_RMS`: `D × FxP × 4`

- For D=1024, FxP=16: 1024 × 16 × 4 = **65,536 cells**.
- RTL: ternip_rms contains VP square mults + VP norm mults + 1 ternip_div
  + 1 ternip_sqrt + 1 MOA (NUM_OPERANDS=VP). NONE of these scale with
  D in width — they scale with VP and with the RMS-internal precisions
  (RmsValueReciprocalPrecision = 2×(FxP+1) = 34 bits;
  RmsSqrtInputPrecision = 4×(FxP+1) = 68 bits). The "loop over D
  elements" is sequential in time (D/VP cycles), not parallel
  in space.
- **DISCREPANCY**: D shouldn't appear in the RMS formula at all. A
  better formula would be ~`VP × FxP × 8 + sqrt_lut_cells`. The
  current formula massively over-states RMS area (probably by a
  factor of ~16-32× at the audit defaults).

### `_est_loadstore`: `D × FxP × 2`

- For D=1024, FxP=16: 1024 × 16 × 2 = **32,768 cells**.
- RTL: ternip_loadstore has no big memory — it's a small FSM that
  routes vector_chunk_t (VP × FxP = 64 bits) data between the
  vector_registers port and the per-core DDR data port. The "D"
  appears in cycle counts, not in width.
- **DISCREPANCY**: D shouldn't appear. Reasonable formula:
  `VP × FxP × ~16` (a few hundred LUTs of FSM + chunk-counter
  registers, sized by chunk width). Currently over-states by ~ D/VP =
  256× factor.

### `_est_rowwise_op`: `VP × FxP × 8`

- For VP=4, FxP=16: 4 × 16 × 8 = **512 cells**.
- RTL: VP parallel multipliers, VP parallel dividers (or BSG-style),
  VP sig/csig/silu LUTs (each LUT has FixedPointUnaryOperationLutSize
  = 1 if HardSigmoid else 2^FxP entries). With UseHardSigmoid=1, the
  LUT cost collapses. Order of magnitude (hundreds-low thousands)
  fits. ✓

### `_est_vector_registers`: `NumVectorRegisters × D × FxP / 9`

- For NVR=4, D=1024, FxP=16: 4 × 1024 × 16 / 9 = **7,281 cells**.
- RTL: pipelined_mem with DATA_WIDTH=VP×FxP=64 and NUM_ENTRIES=
  NVR×D/VP=1024. Total storage = 64 × 1024 = 65,536 bits. As BRAM-
  equivalent LUTs (÷9 is a rough conversion since each Xilinx 36-Kb
  BRAM holds ~36k bits and consumes ~roughly equivalent LUT-area of
  several hundred). Magnitude (~7k LUT-equiv) is in the right
  ballpark. ✓
- Note: per-core vector_registers also has 1 per core (N×BS total
  in NSAI). Doesn't divide by N. ✓ (formula uses raw D, not D/N.)

### `_est_instruction_decode`: `InstructionWidth × 4`

- For IW=128: 128 × 4 = **512 cells**.
- RTL: ternip_gearbox_fifo with InDataWidth=32, OutDataWidth=128.
  Gate count is dominated by the FIFO depth × max(in, out) = ~16 × 128 =
  2048 bits of storage, plus a small FSM. A few hundred LUTs. Order
  of magnitude matches. ✓ (The "decoder" itself is inline always_comb
  and adds maybe 50 LUTs.)

### `_est_xrt_shell`: 174,000 (constant)

- Per build observations, ~174k LUTs for the AU250 platform shell. ✓

### `_est_ternip_core`: sum of children

- Used for the NSAI variant per `_est_ternip_core` docstring (line 168).
  Sums MOA + IV + EV + RMS + loadstore + rowwise + vector_registers +
  instruction_decode. Since several children are wrong (RMS over,
  loadstore over, IV/EV by factor N off), this is also wrong.
- **More importantly**: the NSAI builder DOES create individual MOA,
  IV, EV, RMS, etc. nodes per (i, c) AND also a `ternip_core_i{i}_c{c}`
  node (topology.py lines 184–215). When `_est_ternip_core` is called,
  it sums the children again, so the children's cells are counted
  TWICE (once as the ternip_core node, once as the discrete child
  nodes). **Double-counting bug**: visualizer's total kernel-cell
  count will be ~2× the per-component sum.

### `_est_tmatmul_unit`: `MOA + IV + EV + 200`

- Not used in NSAI builder (no `tmatmul_unit` node created in
  `_build_NumSeparateAxiInstances()`). Only used by NumTmatmulBanksPerCore.

## Open questions for the user

1. **Hierarchy depth for nodes**: The visualizer creates both
   `ternip_core_i{i}_c{c}` AND the per-FU children (MOA, IV, EV, RMS,
   loadstore, rowwise, vector_registers). Should the per-core "core"
   node be a CONTAINER that visually wraps the children (and contributes
   no extra cells), or should it be a SEPARATE node representing the
   ternip_core arbitration/glue logic? Currently `_est_ternip_core`
   sums all children — which means if the visualizer renders both the
   container AND the children, cells are counted twice.

2. **Granularity for IV/EV**: There's no `ternip_importvector.sv` /
   `ternip_exportvector.sv` module — they're `ternip_pipelined_mem`
   instances inside `ternip_tmatmul`. Should the visualizer still show
   them as separate nodes (as it does), or fold them into a single
   `tmatmul` node? They're a major chunk of tmatmul's BRAM footprint,
   so keeping them visible has value, but the labeling could clarify
   "tmatmul.importvector (inside ternip_tmatmul)" instead of standalone.

3. **`tmatmul_dma` granularity**: Currently `tmatmul_dma` includes both
   axi_dma_rd and gbfifo_tmatmul. The "loadstore DMA" equivalents
   (axi_dma + gbfifo_loadstore_r + gbfifo_loadstore_w) aren't shown
   as separate nodes — the visualizer draws `loadstore <-> dram`
   directly. Should the loadstore DMA be made symmetric (i.e. added as
   `loadstore_dma_i{i}` node, with separate gearbox FIFOs for R and W)?

4. **CoreInterconnectNumStages**: The pipelined_interconnect wrappers
   (~6 buses × 8 stages each = 48 stages of FFs per AXI instance) are
   omitted from the visualization. Is it intentional to abstract them
   out, or should they be represented as edge attributes (pipeline
   depth) or as in-line nodes? For an "architecture visualizer" they're
   maybe noise; for a "what's actually on the chip" visualizer they're
   significant FF mass and SLR-crossing infrastructure.

5. **Instruction-decode edge for NSAI**: Visualizer draws
   `xrt_shell -> instruction_decode_i{i}` and labels it "InstrFetchWidth
   from host via XRT", but the value is `InstructionWidth=128`, not
   `InstrFetchWidth=32`. Also, the actual hardware path is XRT→DRAM
   (host writes) followed by DRAM→axi_dma_instr→kernel (kernel-side
   DMA reads). Should the visualizer:
   (a) show the path more accurately (with `dram->axi_dma_instr` at
       DdrDataWidth and `xrt_shell->axi_dma_instr` at AXI-Lite width),
   (b) keep the simplified `xrt_shell→instruction_decode` edge but
       fix the label/width to actually be `InstrFetchWidth=32`, or
   (c) leave it as-is and just clarify in documentation?

6. **AXI-Lite control bus**: The XRT shell drives multiple AXI-Lite
   buses per AXI instance (`s_axi_stall`, `s_axi_rst`, `s_axi_debug`
   plus the axi_dma's `S_AXI_LITE`). All are small (~32 bits). None
   are visualized. Worth surfacing as a single "control" edge from
   `xrt_shell` to `axi_ternip_batched_i` per instance?

7. **Per-core loadstore→DRAM edge multiplicity**: At BS>1, the
   visualizer emits BS edges from `ls_i{i}_c{c}` to `dram_b{i}`, each
   labeled DdrDataWidth=512. In RTL there is ONE 512-bit AXI bus per
   AXI instance, shared across BS cores via the gearbox. Should this
   be one edge per AXI instance (labeled `DdrDataWidth`) with a
   per-core "tap" inside?

8. **NumDdrBanksUsed semantics in NSAI**: The visualizer parameter
   `NumDdrBanksUsed` is interpreted as N (the number of AXI instances)
   in `_build_NumSeparateAxiInstances`. But the RTL constant is
   `NumSeparateAxiInstances` (a SEPARATE config parameter from
   `DramNumBanks=4`). Should the visualizer use `NumSeparateAxiInstances`
   directly, with the constraint that N ≤ DramNumBanks? Currently
   it's correct numerically (both =4 at MaxCores) but conceptually
   conflated.
