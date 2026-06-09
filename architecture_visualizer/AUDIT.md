# AUDIT.md — visualizer topology vs RTL reality

Per-variant audit of `av_lib/topology.py` and `av_lib/cell_estimates.py` against the RTL pinned in `architectures/<variant>/`. Each section was written by an independent agent reading the RTL whole, not via grep snippets.

**RTL submodule pins**:

| Variant | Commit | Branch |
|---|---|---|
| NumSeparateAxiInstances | d6a5491 | main |
| NumDdrBanksPerTmatmul   | 6b08f75 | NumDdrBanksPerTmatmul |
| NumTmatmulBanksPerCore  | cf7838a | NumTmatmulBanksPerCore |

Findings below. **No fixes have been applied** — this is the raw audit.

---

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

---

# AUDIT_NDB.md — NumDdrBanksPerTmatmul variant

Commit audited: 6b08f75 (NumDdrBanksPerTmatmul branch of ternary_matmul_claude;
"build_56: revert core pblocks, keep VP=1 BS=20 + Q1_LANES=4 + SSI directive")

Date: 2026-06-05

Bus-width parameters used throughout this audit (matching the visualizer
defaults and the audit prompt's stipulation):
TmatmulParallelism (TP) = 128, VectorParallelism (VP) = 4,
FixedPointPrecision (FxP) = 16, D = 1024, NumDdrBanksPerTmatmul (N) = 4.

(The actual MaxCores config on disk uses TP=256, VP=1, BS=20. Where this
matters for an order-of-magnitude check I note it inline. The visualizer
exposes TP/VP/BS as sliders, so the formula audit is independent of any
single config snapshot — what matters is whether the formula matches the
RTL signal's declared width.)

Derived constants at those defaults:
- `ternary_t` = 2 bits
- `fixed_point_t` = FxP = 16 bits
- `vector_chunk_t` = VP * FxP = 4 * 16 = 64 bits
- `tmatmul_stream_data_t` = TP * 2 = 256 bits per bank
- `ImportVectorRowWidth` = min(TP, D) = min(128, 1024) = 128
- `RowParallelism` = (TP < D) ? 1 : (TP/D) = 1 (since 128 < 1024)
- `tmul_result_t` = FxP + 1 = 17 bits
- `DdrDataWidth` = 512
- `DdrAddressWidth` = 64
- `InstructionWidth` = 128 (visualizer default) — config has 64
- `InstrFetchWidth` = 32 (axis_instruction t-data side)

Structural observations from the RTL (set context for the rest of the
audit):

- `axi_ternip_batched.sv` is the top kernel. It instantiates `ternip_batched
  batched (.*)`. Both `tmatmul_dma` and the descriptor-slice `tmatmul_desc_slice`
  are generate-block-replicated per-bank (`NumDdrBanksPerTmatmul` instances
  each). The loadstore DMA path is singular (one `dma_rw_loadstore`).
- `ternip_batched.sv` replicates `ternip_buffered buffered` BatchSize
  times (`generate for ... begin : core`). Each replica gets its own
  instruction copy, its own loadstore data slot, and a shared (broadcast)
  view of the per-bank tmatmul stream. Loadstore R/W data is per-core
  (one vector_chunk_t per core in `loadstore_ddr_r_data_i[BatchSize-1:0]`).
- `ternip_buffered.sv` is a thin shell around one `ternip_core` plus a
  set of `ternip_pipelined_interconnect` register slices that pipeline
  cross-SLR signals (instruction, loadstore ddr_stream/r/w/debug, and
  per-bank tmatmul_ddr_stream / tmatmul_ddr_r). Per-bank tmatmul buffers
  are generate-replicated.
- `ternip_core.sv` instantiates exactly five children: `ternip_vector_registers`,
  `ternip_loadstore`, `ternip_rowwise_operation`, `ternip_rms`, and
  `ternip_tmatmul`. It also contains a small instruction-decode FSM
  (`instr_fsm_q`) — this FSM is per-core, inline, NOT a separate module.
- `ternip_tmatmul.sv` is the key NumDdrBanksPerTmatmul module. Within
  the single tmatmul:
  - A SHARED single importvector (one `ternip_pipelined_mem` instance,
    `ImportVectorRowWidth * fxp` = 128 * 16 = 2048 bits wide).
  - A SHARED single gbfifo_import (vector_chunk_t -> importvector).
  - Per-bank `bank_lane[bank_GEN]` generate block, each containing:
    - `ternip_multioperand_accumulator multioperand_accumulator` — actually
      `RowParallelism` of these per bank (so 1 MOA per bank at default
      parameters; 1*4 = 4 MOAs total in the variant).
    - One `ternip_gearbox_fifo gbfifo_export` per bank.
    - One `ternip_pipelined_mem exportvector` per bank, sized
      `ChunksPerBankExport = RowsPerBank / VectorParallelism =
      (D/N)/VP = (1024/4)/4 = 64` entries.
  - Plus per-bank `go_ddr_capture[b].lanes[l]` of
    `ternip_wide_capture_lane` (currently GO_DDR_LANES_PER_BANK=1, so
    one lane per bank holding the 256-bit ternary chunk).
  - Q1 skid (`q1_lanes[0..3].lane`) — 4 instances of `ternip_skid_lane`
    splitting the 2048-bit importvector_read_data into 512-bit lanes.

This means: the visualizer's "MOA per bank" + "importvector per bank" +
"exportvector per bank" cardinality is **partially right**. In the RTL:
- MOA: NumDdrBanksPerTmatmul × RowParallelism instances per tmatmul (4 × 1 = 4 at defaults).
- importvector: **1 SHARED instance per tmatmul** (not per-bank).
- exportvector: NumDdrBanksPerTmatmul instances per tmatmul (4 per tmatmul).
- gbfifo_export: NumDdrBanksPerTmatmul instances per tmatmul (4 per tmatmul).
- gbfifo_import: **1 SHARED instance per tmatmul**.

And critically the visualizer creates a moa/iv/ev per *core* (BatchSize),
while the RTL has 1 tmatmul per core. So both axes of cardinality are
involved: the visualizer is correct that the tmatmul subsystem replicates
with BatchSize (one full tmatmul per core), and it is correct to have
per-bank MOAs/EVs inside one tmatmul — but it is wrong to make
importvector also per-bank.

## Nodes

### Nodes the visualizer creates but RTL doesn't have

- **`importvector` per bank per core.** The visualizer creates
  `importvector_c{c}` (1 per core), but the per-core construction
  treats it as a single block — actually OK at that scope, but
  conceptually labeled "importvector" without bank cardinality.
  HOWEVER, the visualizer's *NumTmatmulBanksPerCore* sibling creates
  importvector per-bank-per-core (3-arg id with `_u{u}`). Reading
  `_build_NumDdrBanksPerTmatmul()` carefully, I see it creates ONE
  importvector per core (`importvector_c{c}`) — so this one matches
  the RTL: importvector is shared inside the single tmatmul. **No
  spurious importvector nodes in this variant.** Marking this row
  for completeness; net result is "matches RTL".

- **`exportvector` per core (singular).** The visualizer creates
  `exportvector_c{c}` (1 per core). The RTL has
  `NumDdrBanksPerTmatmul` exportvector instances per core (one per
  bank). The visualizer is collapsing N actual instances into 1.
  See "wrong cardinality" below.

- **`MOA` per core (singular).** Same as exportvector — visualizer
  has 1 MOA per core, but RTL has `NumDdrBanksPerTmatmul *
  RowParallelism` MOAs per core (4 at defaults). See "wrong
  cardinality" below.

- **Loadstore edge directly to `dram_b0`.** Visualizer ties
  `loadstore_c{c}` to `dram_b0` in both directions. The RTL routes
  the loadstore data through `dma_rw_loadstore` (an Alex Forencich
  `axi_dma` instance) to the **external** `m_axi_loadstore_*`
  ports — which are pinned to a DDR bank by the platform
  (typically DDR[0] on the AU250 with xilinx_u250_gen3x16_xdma).
  So conceptually the edge is right, but it short-circuits through
  the implicit `dma_rw_loadstore` + the build_43/44 pipelined
  buffer slices (`buffer_loadstore_ar`, `buffer_loadstore_r`,
  `buffer_loadstore_aw`, `buffer_loadstore_w`, `buffer_loadstore_b`)
  that physically separate the kernel from the DRAM. Whether to
  model these as nodes is a judgement call (the user spec only
  lists tmatmul_dma as a peripheral node), but the edge label
  "DdrDataWidth (loadstore R/W-channel)" with bus_bits=512 (DdrDataWidth)
  is reasonable.

- **`axi_dma_instr` node.** Visualizer creates one. The RTL does NOT
  have a module called `axi_dma_instr`. Instead `axi_ternip_batched`
  has the `s_axis_instruction_*` port (an AXI-stream from XRT) and
  feeds it into a `ternip_gearbox_fifo gbfifo_instruction` that
  widens 32-bit beats to 64-bit instruction_t beats. There is NO
  AXI-DMA on the instruction channel; XRT pushes the stream directly.
  The visualizer's `axi_dma_instr -> instruction_decode` edge is
  meant to represent that stream, but the node label is misleading
  and the edge width (`InstructionWidth`, 128 in the visualizer
  default; 64 in the config) doesn't match either side.

- **`instruction_decode` node.** Similarly, there's no module by
  that name. The RTL has `gbfifo_instruction` (a gearbox FIFO) at
  the top level, and per-core inline FSMs (`instr_fsm_q` in
  `ternip_core.sv`). The closest analogue is `gbfifo_instruction +
  the per-core decode FSMs`. Treating these as a single node is a
  reasonable simplification, but the node should be labeled
  accordingly.

- **`xrt_shell` node.** The visualizer creates one. There is no
  separate XRT-shell *module* in this RTL — XRT is the platform
  (`xilinx_u250_gen3x16_xdma`) and is wrapped around the kernel
  externally by Vitis at link time. Modeling it as a node is fine
  for the visualization (it's where the instruction stream and the
  DRAM-side AXI requests come from), but it should be flagged as a
  platform construct, not a kernel module.

### Nodes RTL has but visualizer doesn't model

- **`ternip_buffered`** — the per-core wrapper that contains all
  the `ternip_pipelined_interconnect` register slices for cross-SLR
  pipelining. At BatchSize=20 this is 20 instances, each carrying
  ~12 pipelined-interconnect register slices (1 for instruction, 4
  for loadstore ddr_stream/r/w/debug, 2 per bank for tmatmul stream/r
  — so 4 + 8 = 12 per buffered, plus 4 per-bank descriptor slices
  outside the wrapper). At ~CoreInterconnectNumStages=6 each, this
  is the dominant FF mass in the design. Not modeled at all.

- **`ternip_pipelined_interconnect`** instances:
  - **Per-core instruction slice:** `buffer_instruction` (128 bits
    instruction_t wide, 6 stages).
  - **Per-core loadstore slices:** `buffer_loadstore_ddr_stream`
    (`DdrAddressWidth + 1 + 32` = 97 bits), `buffer_loadstore_ddr_r`
    (vector_chunk_t = VP*FxP = 64 bits), `buffer_loadstore_ddr_w`
    (vector_chunk_t = 64 bits), `buffer_loadstore_ddr_debug` (64
    bits).
  - **Per-core, per-bank tmatmul slices:** `buffer_tmatmul_ddr_stream`
    (DdrAddressWidth+32 = 96 bits), `buffer_tmatmul_ddr_r` (`$bits(
    tmatmul_ddr_r_data_i[b])` = TP*2 = 256 bits).
  - **Top-level per-bank descriptor slices:** `tmatmul_desc_slice[b].
    buffer_tmatmul_desc` (DdrAddressWidth+32 = 96 bits, 6 stages).
  - **Top-level loadstore m_axi AR/R/AW/W/B slices:**
    `buffer_loadstore_ar` (98 bits AR), `buffer_loadstore_r` (523
    bits R), `buffer_loadstore_aw` (98 bits AW), `buffer_loadstore_w`
    (577 bits W), `buffer_loadstore_b` (10 bits B), all 6 stages.
  - **Top-level per-bank tmatmul m_axi R-channel slices:**
    `tmatmul_dma[b].buffer_m_axi_tmatmul_r` (523 bits, NumStages=8).

  None of these are modeled. They are arguably internal to the
  visualizer's "edge" representation (i.e. the edges between
  `ternip_core` and the external AXI ports should implicitly
  carry the pipeline stages as a property), but the visualizer
  currently has no representation of them at all.

- **`axi_dma_rd dma_r_tmatmul`** (Alex Forencich read-DMA) — one
  per bank inside `tmatmul_dma[b]`. The visualizer's `tmatmul_dma`
  node lumps this together with `gbfifo_tmatmul`, which is OK
  but worth noting because the cell estimate is dominated by
  `axi_dma_rd`.

- **`ternip_gearbox_fifo gbfifo_tmatmul`** — one per bank, width
  conversion from DdrDataWidth (512) to TmatmulParallelism *
  ternary_t (TP*2 = 256). At TP=128 this is a 2:1 narrower, at
  TP=256 it's a 1:1. Lumped into `tmatmul_dma` node — that's fine.

- **`ternip_gearbox_fifo gbfifo_instruction`** — top-level gearbox
  from InstrFetchWidth (32) to InstructionWidth (config-dependent,
  64 in MaxCores). Not modeled separately — could conceptually
  fold into the visualizer's `instruction_decode` node.

- **`ternip_gearbox_fifo gbfifo_loadstore_r` / `gbfifo_loadstore_w`** —
  top-level gearboxes that transpose between DdrDataWidth (512)
  and `fixed_point_t [VectorParallelism-1:0][BatchSize-1:0]`
  (VP*BS*FxP = 4*20*16 = 1280 bits at MaxCores). Not modeled.
  Could fold into the loadstore<->DRAM edge.

- **`axi_dma dma_rw_loadstore`** — Alex Forencich read+write DMA.
  Not modeled; folded into the visualizer's `loadstore -> dram_b0`
  edge.

- **`ternip_wide_capture_lane`** instances inside `ternip_tmatmul.go_ddr_capture[b].lanes[l]`
  — these are explicit `(d->q)` flop arrays for the 256-bit ternary
  capture (`go_ddr_data_q`). Not modeled, and reasonably so — they
  are part of the tmatmul's internal microarchitecture, below the
  granularity of the visualizer's nodes.

- **`ternip_skid_lane`** instances inside `ternip_tmatmul.q1_lanes[i].lane`
  (4 of them) and `ternip_tmatmul.vector_read_skid` (1 instance, a
  `ternip_pipelined_interconnect` actually). Below-FU granularity;
  acceptable that they aren't modeled.

- **`s_axi_ternip_rst`, `s_axi_ternip_const_rd`, `s_axi_stall_wr`,
  `s_axi_debug`** — top-level AXI helper IP for control/stall/debug.
  Small (~500 LUT each); could be lumped into the XRT-shell node or
  ignored. Not modeled.

### Nodes that exist in both but with wrong cardinality

- **MOA** — visualizer: 1 per core (`moa_c{c}`). RTL: `NumDdrBanksPerTmatmul
  × RowParallelism` per tmatmul, which is `4 × 1 = 4` per core at
  defaults. So in this variant, the visualizer should create
  `moa_c{c}_b{b}` with b in [0..N), and there is one MOA per (core,
  bank) pair.

  Counter-argument: the visualizer's CLAUDE.md explicitly lists
  `multioperand_accumulator[bank0..3]` as nodes at the top level
  (i.e. one per bank, not per core), implying that the user's
  conceptual model is "MOAs are bank-resident, not core-resident."
  The actual RTL puts them inside `ternip_tmatmul`, which is
  per-core. **Open question for the user** — see bottom of audit.

  At MaxCores config (TP=256, VP=1, RowParallelism=max(1, 256/1024)=1),
  still 4 MOAs per core. At TP=2048 (hypothetical), RowParallelism
  would be 2 and there would be 4*2 = 8 MOAs per core.

  Bus width also misses this:  the visualizer says `moa -> ternip_core`
  is `RowParallelism * FixedPointPrecision` = 1 * 16 = 16 bits per
  edge. The actual `accumulator_result[bank][row]` aggregate that
  feeds the rest of the design is `N * RowParallelism * fxp` = 4 *
  1 * 16 = 64 bits, BUT each MOA only contributes its own
  `RowParallelism * fxp` = 16 bits — so the per-edge formula is
  fine IF the visualizer has N MOA nodes each with a 16-bit edge.
  With only 1 MOA node, the cumulative edge bandwidth is off by
  a factor of N.

- **importvector** — visualizer: 1 per core (`importvector_c{c}`).
  RTL: 1 shared per tmatmul (i.e. per core). **MATCHES.** (Noted
  here in "wrong cardinality" only because both NumSeparateAxiInstances
  and NumTmatmulBanksPerCore variants get this differently — for
  NumDdrBanksPerTmatmul it's correct.) The user's CLAUDE.md
  enumeration of `tmatmul_importvector[bank0..3]` actually
  CONFLICTS with what the RTL does — the RTL shares one
  importvector across all banks. This is a real architectural
  difference between variants worth flagging: in
  NumDdrBanksPerTmatmul the importvector is single-instance, not
  per-bank.

- **exportvector** — visualizer: 1 per core. RTL: N per core
  (`bank_lane[b].exportvector`). So at defaults, 4 per core, not 1.
  Same as MOA: the visualizer needs `exportvector_c{c}_b{b}` (or
  the equivalent) and N edges.

  Bus width: the per-bank exportvector read port emits a
  `vector_chunk_t = VP*FxP = 64 bits`, and one bank's worth at a
  time is selected via the response-side bank counter
  (`exportvector_read_data[export_bank_response_q]`). So the
  per-edge width is 64 — consistent with the visualizer's
  `VectorParallelism * FixedPointPrecision`. Off only in cardinality
  (N edges instead of 1).

- **gbfifo_export** (visualizer does not model, but worth noting): N per
  core, sized fixed_point_t * RowParallelism on the input side and
  vector_chunk_t on the output side. Buried inside the tmatmul, so
  perhaps OK to leave unmodeled.

- **tmatmul_dma** — visualizer: N (one per bank). RTL: N (one per
  bank). **MATCHES**. But the cell-count is shared across all
  BatchSize cores, not replicated per core — and the visualizer's
  formula treats it as a fixed bank-resident node. Good.

- **ternip_core / loadstore / RMS / rowwise_op / vector_registers**
  — visualizer: 1 per core (BatchSize copies). RTL: 1 per core
  (BatchSize copies of `ternip_buffered.core`). **MATCHES**.

## Edges

For each edge in `_build_NumDdrBanksPerTmatmul()`, I'll quote the
visualizer's call site (rough line ref into `topology.py`) and check
the bus_bits formula against the actual RTL signal.

### Region: DRAM / AXI surface

**E1: `dram_b{b}` -> `tmatmul_dma_b{b}` (per bank b)** (topology.py line ~339)
- RTL signal:
  `m_axi_tmatmul_<b>_rdata` (DdrDataWidth = 512 bits) — XRT's DDR
  controller drives this back as the R-channel response of the
  per-bank AXI4 read transaction. Note: this is the *response*
  direction, despite the edge being drawn dram->dma (which is the
  physical direction of data). The address (`araddr`, 64 bits) goes
  the other way. The visualizer only models one direction.
- Correct bus_bits: 512 (DdrDataWidth) — also there is the AR channel
  going the other way at ~98 bits. Picking just R-data at 512 is a
  reasonable simplification.
- Visualizer's formula: `DdrDataWidth` = 512. **MATCHES** (for the R
  direction).
- DISCREPANCY: minor — the AR direction (64-bit address +
  ~30 bits sideband) isn't modeled. Acceptable simplification.

**E2: `ls_id` -> `dram_b0` and `dram_b0` -> `ls_id`** (topology.py lines ~425-432)
- RTL signals: `m_axi_loadstore_wdata` (DdrDataWidth=512) for kernel->DRAM,
  `m_axi_loadstore_rdata` (DdrDataWidth=512) for DRAM->kernel.
  Plus AR/AW (64+~30 bits each) and B (~10 bits) sideband channels.
- Correct bus_bits: 512 each direction (data) is fine as a
  simplification.
- Visualizer's formula: `DdrDataWidth` for both. **MATCHES**.
- DISCREPANCY: minor — model assumes loadstore goes to bank 0
  (`"dram_b0"` hardcoded). At Vitis link time, the platform's
  `sp=...:DDR[0]` option in `kernel.cfg` does pin loadstore to
  DDR[0] on the AU250, so this is correct in practice. Not flagged
  as a discrepancy.

### Region: shared (instruction, axi_dma_instr)

**E3: `axi_dma_instr` -> `instruction_decode`** (topology.py line ~345)
- RTL signal: there is no `axi_dma_instr` in the RTL. The closest
  analogue is the `s_axis_instruction_*` AXI4-stream from XRT into
  `gbfifo_instruction`. The stream is `InstrFetchWidth = 32` bits
  wide on the XRT side and `InstructionWidth = 64` bits wide
  (config) on the kernel side.
- Correct bus_bits (kernel side): InstructionWidth = 64 in config,
  but the visualizer's default is 128.
- Visualizer's formula: `InstructionWidth` = 128 (visualizer
  default). **DISCREPANCY** (factor of 2 vs config). Also the
  source node is fictitious.
- Interpretation: this is the "decoded -> kernel" wire conceptually,
  and matching InstructionWidth is the right idea. The mismatch is
  in (a) the visualizer's default value (128 vs config's 64) and
  (b) the missing `gbfifo_instruction` widening step. If the
  visualizer treats `axi_dma_instr` as "everything between XRT and
  the per-core decoder including the gearbox", then 64 is the
  correct outgoing width — and the visualizer's formula symbol is
  right but the default value is wrong.

**E4: `xrt_shell` -> `instruction_decode`** (topology.py line ~465)
- RTL signal: `s_axis_instruction_tdata` is `InstrFetchWidth = 32`
  bits wide.
- Correct bus_bits: 32 (`InstrFetchWidth`) — NOT InstructionWidth.
- Visualizer's formula: `iw` (= `InstructionWidth` = 128 by default).
  **DISCREPANCY**: should be `InstrFetchWidth`, not `InstructionWidth`.
  The formula string says "InstrFetchWidth (instructions from host
  via XRT)" — note the *string label* is correct, but the
  *computed value* (`iw = InstructionWidth`) is wrong. Two off-by-N
  factors: (i) symbolically should be InstrFetchWidth (4x smaller
  than the visualizer's default 128), and (ii) the visualizer
  doesn't expose `InstrFetchWidth` in `_DEFAULTS` so the formula
  cannot get it right today.

**E5: `instruction_decode` -> `ternip_core_c{c}`** (topology.py line ~404, per core)
- RTL signal: there is no separate `instruction_decode` module that
  drives ternip_core directly. The top-level signal `instruction_i`
  fans out from `gbfifo_instruction` through `ternip_batched`'s
  always_comb (broadcast to all cores) through each
  `ternip_buffered`'s `buffer_instruction`
  (`ternip_pipelined_interconnect` with InstructionWidth = 64 bits
  data) into `ternip_core`'s instruction port.
- Correct bus_bits: `InstructionWidth` = 64 (config) or 128
  (visualizer default).
- Visualizer's formula: `iw` = `InstructionWidth` = 128 (default).
  **MATCHES** in symbol but DISCREPANCY in default value (64
  vs 128). The right symbol; the wrong default.

### Region: tmatmul subsystem (tmatmul_dma[b] -> MOA path)

**E6: `tmatmul_dma_b{b}` -> `moa_c{c}` (per bank b, per core c)** (topology.py line ~398)
- RTL signal path: `dma_r_tmatmul.m_axis_read_data_tdata` ->
  `gbfifo_tmatmul.in_data_i` (DdrDataWidth=512) ->
  `gbfifo_tmatmul.out_data_o` (`ternary_t [TmatmulParallelism-1:0]`
  = TP*2 = 256 bits) -> `tmatmul_ddr_r_data_i[bank]` ->
  per-core `ternip_buffered.tmatmul_ddr_buffers[b].buffer_tmatmul_ddr_r`
  (`ternip_pipelined_interconnect` with DataWidth = TP*2 = 256 bits)
  -> `ternip_core.tmatmul.ddr_r_data_i[bank]` ->
  `ternip_tmatmul.go_ddr_capture[b].lanes[*].ternip_wide_capture_lane`
  -> `go_ddr_data_q[b]` -> ternary_mul tree ->
  `accumulator_operands_q2[b]` -> `bank_lane[b].row[*].multioperand_accumulator`.
- Correct bus_bits at the visible-edge boundary (tmatmul_dma to a
  core's MOA): the FU-boundary signal width is **TP * 2** (i.e.
  TmatmulParallelism * $bits(ternary_t) = TmatmulParallelism * 2)
  per bank per core.
- Visualizer's formula: `tp * 2` ("TmatmulParallelism * 2
  (ternary stream, broadcast)"). **MATCHES**.
- Notes: the broadcast is BatchSize-wide (every core's MOA sees
  this stream, modeled correctly: edges from each bank to each
  core's moa). Wire-length cost in the model thus scales as
  `BatchSize * NumDdrBanksPerTmatmul * (tp*2)` — which matches the
  RTL pressure (each tmatmul_dma fans out to BS cores).

  HOWEVER, the visualizer also DOESN'T account for cardinality of
  MOAs within a core (see "wrong cardinality" above). In the RTL,
  the bank-b R-channel goes only to the bank-b MOA inside each
  core's tmatmul (not to all MOAs). With 1 visualizer MOA per
  core, the visualizer ends up tying each bank to "the MOA",
  which OVERCOUNTS the broadcast within a core but UNDERCOUNTS
  the lane separation. Net bits are roughly comparable; the topology
  is just wrong shape.

**E7: `iv_id` -> `moa_id` (importvector -> MOA, per core)** (topology.py line ~414)
- RTL signal: `importvector.read_data_o` is
  `fixed_point_t [ImportVectorRowWidth-1:0]` = 128 * 16 = 2048
  bits, fed into `q1_lanes` skid, then driven combinationally into
  `accumulator_operands[b][r][i] = ternary_mul(go_ddr_data_q[b][i],
  importvector_read_data_q1[i])`. So importvector_read_data_q1's
  full 2048 bits drive ALL N banks (broadcast) — not just one MOA.
- Correct bus_bits: `ImportVectorRowWidth * fxp` = min(TP,D) * fxp =
  128 * 16 = 2048 bits, broadcast across N MOAs.
- Visualizer's formula: `tp * fxp` =
  `TmatmulParallelism * FixedPointPrecision` = 128 * 16 = 2048 bits.
  **MATCHES** the bus width (because min(TP,D) = TP when TP<D, which
  holds for the visualizer default). At TP=256 with D=1024
  (MaxCores config), `tp*fxp` = 4096 but `ImportVectorRowWidth*fxp`
  = min(256, 1024)*16 = 4096 — still matches. At TP=2048 (very
  hypothetical, TP>D), `tp*fxp` would be 32768 but
  `ImportVectorRowWidth*fxp` = D*fxp = 16384, so the formula would
  break for TP>D.
- DISCREPANCY: subtle — formula symbol uses `tp`, RTL uses
  `min(tp, d)`. Fine for typical configs (TP ≤ D), broken for
  TP > D. Also: visualizer creates only ONE edge per core
  (iv -> moa), but RTL has the bus broadcast to N MOAs. With only
  1 MOA in visualizer, the topology is shape-wrong but the per-edge
  bus_bits is right.

**E8: `moa_id` -> `ternip_core_c{c}` (per core)** (topology.py line ~410)
- RTL signal: this edge is meant to represent the MOA result
  flowing back to the core. But looking at the RTL, the MOA result
  does NOT flow to `ternip_core` directly — it goes through the
  per-bank `gbfifo_export` (fixed_point_t * RowParallelism wide
  input, vector_chunk_t wide output) into the per-bank
  `exportvector` (a `ternip_pipelined_mem` of `ChunksPerBankExport`
  entries, each `vector_chunk_t = VP*FxP` wide). The actual
  back-to-core edge would be **exportvector -> ternip_core via
  `vector_request_w_data_o`**, which is `vector_chunk_t = VP*FxP =
  64 bits` per beat (with bank selection done by
  `export_bank_response_q`).
- Correct bus_bits: `RowParallelism * fxp` = 1 * 16 = 16 bits per
  MOA output (so per-MOA this is correct), but the aggregate
  feeding the next stage (gbfifo_export -> exportvector) is wider
  in time (each bank's MOA emits a beat, accumulated in its
  per-bank gbfifo_export then written to its per-bank exportvector).
- Visualizer's formula: `moa_out_bits = row_parallelism * fxp` = 1 *
  16 = 16. **MATCHES** the per-MOA width — but the *interpretation*
  is off: this is the MOA -> gbfifo_export -> exportvector path
  inside the bank lane, not a direct edge to ternip_core. The path
  exists; it's just shaped differently.
- DISCREPANCY: edge semantics. The MOA result physically goes
  MOA[b] -> gbfifo_export[b] -> exportvector[b] -> (bank-mux) ->
  vector_request_w_data_o -> vector_registers -> ternip_core. The
  visualizer skips the intermediate hops, which is a defensible
  simplification, but the bus width should reflect the visible-FU
  boundary edge between MOA and the next FU. Between MOA and
  gbfifo_export the width is RowParallelism * fxp = 16. Between
  exportvector and ternip_core (via vector_registers) the width is
  VP*fxp = 64. The visualizer uses 16, which is the narrower of
  the two — defensible but inconsistent with the next bullet.

**E9: `moa_id` -> `ev_id` (MOA -> exportvector, per core)** (topology.py line ~418)
- RTL signal: MOA -> gbfifo_export.in_data (per-bank, fixed_point_t
  * RowParallelism = 1 * 16 = 16 bits wide).
  gbfifo_export.out_data -> exportvector.request_w_data
  (vector_chunk_t = VP*FxP = 64 bits wide).
- Correct bus_bits: 16 bits on the in side, 64 bits on the out side
  (gbfifo_export does the rate conversion). Visualizer takes the
  narrower of the two.
- Visualizer's formula: `moa_out_bits` = `row_parallelism * fxp` = 16.
  **MATCHES** the in-side width.
- DISCREPANCY: again, only one MOA -> exportvector edge per core
  in the visualizer, but RTL has N (one per bank). And the
  visualizer collapses the gbfifo_export stage. Both minor.

### Region: vector_registers <-> FUs

**E10: `ternip_id` -> `rms_id`** (topology.py line ~436)
- RTL signals between `ternip_core.rms` and the core's
  `vector_registers` arbitration:
  - rms drives `vector_request_w_data_o` (vector_chunk_t = VP*fxp =
    64 bits) into core's mux
  - rms reads `vector_read_data_i` (vector_chunk_t = 64 bits) from
    core's vector_read fanout
- Correct bus_bits: 64 bits each direction (`VP*FxP`).
- Visualizer's formula: `vp * fxp` = 4 * 16 = 64. **MATCHES**.

**E11: `ternip_id` -> `ls_id` (loadstore)** (topology.py line ~440)
- Same shape: vector_chunk_t = VP*FxP = 64 bits each direction. The
  visualizer's `vp * fxp` formula **MATCHES**.

**E12: `ternip_id` -> `rw_id` (rowwise_op)** (topology.py line ~444)
- Same: vector_chunk_t = 64 bits. **MATCHES**.

**E13: `ternip_id` -> `vr_id` (vector_registers)** (topology.py line ~448)
- RTL: ternip_core's arbitrated vector_request mux drives
  vector_registers.request_*; vector_registers.read_data_o feeds
  back into ternip_core's vector_read distribution. Both directions
  vector_chunk_t = 64 bits.
- Visualizer's formula: `vp * fxp` = 64. **MATCHES**.

**E14: `vr_id` -> `iv_id` (vector_registers -> importvector)** (topology.py line ~452)
- RTL signal: there is no direct path from vector_registers to
  importvector. The actual path is:
  `vector_registers.read_data_o` -> `ternip_core.vector_read_data` ->
  `ternip_tmatmul.vector_read_data_i` ->
  `ternip_tmatmul.vector_read_skid` (a
  `ternip_pipelined_interconnect`, vector_chunk_t = 64 bits wide) ->
  `gbfifo_import.in_data_i` (vector_chunk_t = 64 bits) ->
  `gbfifo_import.out_data_o` (`fixed_point_t [ImportVectorRowWidth-1:0]`
  = 128 * 16 = 2048 bits) -> `importvector.request_w_data_i` (2048
  bits).
- Correct bus_bits at the visible-FU edge between vector_registers
  and importvector: the **input** side of the gbfifo_import is 64
  bits (vector_chunk_t); the **output** side feeding the importvector
  is 2048 bits (ImportVectorRowWidth * fxp). The visible
  vector_registers -> importvector edge is conceptually the gbfifo's
  output width on the importvector side, since the gbfifo is a
  serial-to-parallel widener that lives inside the tmatmul. But
  treating the edge as "vector_chunk_t" (the slow side) is also
  defensible because that's the actual data wire transferred per
  beat.
- Visualizer's formula: `vp * fxp` = 64 bits. **MATCHES** the
  in-side (slow) width. **DISCREPANCY**: if you wanted to capture
  the wire pressure inside the tmatmul where it writes the
  importvector BRAM, that's `ImportVectorRowWidth * fxp` = 2048
  bits, much wider. The wide write happens internally to the
  tmatmul, not on the visible vr<->iv edge, so 64 bits is the
  right model-level number.

**E15: `ev_id` -> `vr_id` (exportvector -> vector_registers)** (topology.py line ~456)
- RTL signal: `exportvector.read_data_o` (vector_chunk_t = 64 bits)
  -> `vector_request_w_data_o = exportvector_read_data[bank_response_q]`
  (vector_chunk_t = 64 bits) -> `vector_registers.request_w_data_i`
  (vector_chunk_t = 64 bits).
- Correct bus_bits: 64 bits.
- Visualizer's formula: `vp * fxp` = 64. **MATCHES**.

### Region: loadstore <-> DRAM (already covered E2)

### Missing edges (signals that exist in RTL but no visualizer edge)

- **Per-bank descriptor stream from kernel to tmatmul_dma**: the
  pipelined `tmatmul_desc_slice[b].buffer_tmatmul_desc` carries
  the kernel-side `tmatmul_ddr_stream_address_o[b]` (64 bits) +
  `tmatmul_ddr_stream_length_o[b]` (32 bits) = 96 bits per bank
  from the core's tmatmul out to each tmatmul_dma. The visualizer
  has only the DATA edge (dram -> tmatmul_dma); the
  REQUEST/descriptor edge (ternip_core -> tmatmul_dma) is missing.
  This is a critical edge for cross-SLR pressure (build_32/_37
  comment block in the RTL).

- **Per-bank tmatmul_dma -> all-cores fanout**: the visualizer
  correctly draws an edge from each `tmatmul_dma_b{b}` to each
  `moa_c{c}`. But the actual fanout in RTL is to each core's
  *tmatmul*, which then internally distributes across the per-bank
  MOAs. Modeling at MOA granularity is fine; just want to flag
  that the visualizer's edge target is "one MOA per core" while RTL
  has "one MOA per (core, bank)" — see cardinality.

- **stall network (stall_active_o / stall_clear_i)**: 1-bit
  per-core back to top-level. Not modeled, fine.

- **Reset distribution tree**: `axi_ternip_rst -> rst_ni -> per-module
  rst_ni_q`. Not modeled, fine.

- **debug AXI from XRT** (s_axi_debug / s_axi_stall): not modeled,
  fine.

### Spurious edges (visualizer edges that don't correspond to any RTL signal)

- **`ev_id` -> `vr_id` and `vr_id` -> `iv_id` as direct edges**:
  these are syntactically correct (data does flow that way), but
  they skip the gbfifo_import / gbfifo_export rate-conversion FIFOs
  and the inline arbitration mux in ternip_core. The visualizer's
  edge graph implies a direct point-to-point bus, but the RTL has
  a multi-FU shared bus arbitrated by the inline FSM in ternip_core.
  Not "spurious" per se; just simplified.

- **`moa_id` -> `ternip_id`**: as noted in E8, this edge has no
  direct RTL realization. The MOA result physically flows
  MOA -> gbfifo_export -> exportvector -> vector_request_w_data ->
  vector_registers. The visualizer creates a direct edge labeled
  "MOA result" with `row_parallelism * fxp` bits. Functionally
  correct in spirit (the MOA's reduced result does eventually feed
  the core's vector_registers), but the wire model is wrong.

- **`axi_dma_instr` -> `instruction_decode` edge**: the source
  node doesn't correspond to anything in the RTL. The edge from
  `xrt_shell` -> `instruction_decode` would be sufficient and
  match the RTL boundary.

## Cell-count formulas (cell_estimates.py)

Brief sanity check at default params (TP=128 VP=4 FxP=16 D=1024 N=4):

- **`_est_DRAM`** returns 0 LUTs. Correct — DRAM is off-FPGA.
- **`_est_axi_dma_instr`** returns 500. RTL has no axi_dma_instr; the
  closest analogue is `gbfifo_instruction` (a small gearbox FIFO,
  maybe ~200 LUTs). 500 is in the right ballpark for "instruction
  ingress hardware including a small DMA-like state machine."
- **`_est_tmatmul_dma`** returns 512*4 + 128*8 = 2048 + 1024 = 3072.
  RTL `axi_dma_rd` (the Alex Forencich master) is ~1500-2000 LUTs
  according to typical Forencich-axi numbers for DataWidth=512;
  plus `gbfifo_tmatmul` (~500 LUTs for a 512->256 width converter)
  = ~2500. Adding the per-bank rst-sync FFs and the build_36
  R-channel pipelined buffer (NumStages=8 * 523 bits = 4184 FFs
  → ~4184 cells) puts the total around 6700 cells — so the
  visualizer's 3072 is light by ~2x. Order-of-magnitude OK.
- **`_est_MOA`** returns TP * fxp * ceil(log2(TP)) = 128*16*7 =
  14336. RTL `ternip_multioperand_accumulator` with
  NUM_OPERANDS=128, NEXT_STAGE_FANIN=4: log4(128) = ceil(3.5) = 4
  stages, each with `((4-1)*4 + 17) = 29` bit-wide registers per
  surviving operand position. Total FFs roughly 128*29 ≈ 3712 in
  the pipeline, plus the accumulator register. Adder LUTs in the
  tree ≈ same order. So ~7-8k cells per MOA at TP=128. Visualizer's
  14k is high by ~2x but right order. OK.

  Note: at TP=256 (MaxCores actual), MOA grows to 256*16*8 = 32768
  cells per MOA in the visualizer. RTL would be ~15-20k. Still
  right ballpark.

- **`_est_importvector`** returns `(D/N)*fxp*2 = (1024/4)*16*2 =
  8192`. This formula assumes the importvector is sized to one
  bank's portion — which is **wrong for this variant** (the
  importvector is SHARED, sized at the full ImportVectorRowWidth
  by DdrReadsPerRow entries = min(TP,D) * (D/min(TP,D)) =
  128 * 8 entries × FxP = 16384 bits of BRAM). The formula
  should be either `ImportVectorRowWidth * fxp * DdrReadsPerRow`
  (storage) or `D * fxp` (linear in D). 8192 happens to roughly
  match `D*fxp/2`, which is in the right order of magnitude, but
  the structural intent of `(D/N)` is wrong for this variant.
- **`_est_exportvector`** returns `(D/N)*fxp*2 = 8192`. The RTL
  exportvector (per bank) is `ChunksPerBankExport * vector_chunk_t
  = (D/N)/VP * VP*FxP = (D/N)*FxP = 1024/4 * 16 = 4096` bits of
  BRAM storage. So 8192 is ~2x but right order. Note: there are
  N exportvectors per core, so the total per-core cost is 8192*N
  = 32768. The visualizer currently only creates 1 exportvector
  per core, so the *total* per-core cell budget is undercount by
  ~N.
- **`_est_tmatmul_unit`** = MOA + IV + EV + 200 (control) = 14336
  + 8192 + 8192 + 200 = 30920. Not used in this variant
  (NumDdrBanksPerTmatmul doesn't have tmatmul_unit nodes — only
  NumTmatmulBanksPerCore does).
- **`_est_RMS`** returns `D * fxp * 4 = 1024 * 16 * 4 = 65536`.
  RTL `ternip_rms`: has fixed-point multiplier lanes
  (`parallel_squares`), divider (`DivisionImplementation = DIV_BSG`),
  sqrt LUT, accumulator. At VP=4 and D=1024 the total is roughly
  10-20k LUTs in practice. So 65k is high by ~3-5x but right
  order. Note: the formula uses `D * fxp` which is dimensionally
  the bit-width of one full vector, not really the RMS cost
  (which scales as VP for the multiplier lanes + a fixed sqrt LUT).
  Suggest `VP * fxp * 16 + 2**fxp` would be a closer model.
- **`_est_loadstore`** returns `D * fxp * 2 = 32768`. RTL
  `ternip_loadstore` is mostly a FSM + a vector counter + the
  vector_request/read interface plumbing; the Alex Forencich
  `axi_dma_rw` lives at the top level, not inside ternip_loadstore.
  Actual loadstore module is ~500-1000 LUTs. 32k is high by ~30x.
  Right order if you fold in the `dma_rw_loadstore` (~5000 LUTs)
  + the 4 build_43/44 pipelined buffer slices (each ~500-3000
  cells) = ~10-15k. Still off by ~2x but in the right zone.
- **`_est_rowwise_op`** returns `VP * fxp * 8 = 4*16*8 = 512`.
  Way low — `ternip_rowwise_operation` has `VectorParallelism`
  copies of every elementwise math FU (mul, div, sig, csig, silu).
  At VP=4 with all those FUs, easily 3-5k LUTs. So 512 is ~10x
  low. At VP=64 (an old config), the formula gives 8192, RTL
  would be ~50k. Off by ~6x. Linear-in-VP shape is right;
  coefficient is undersize.
- **`_est_vector_registers`** returns `nvr * d * fxp / 9 = 4 * 1024
  * 16 / 9 = 7281`. RTL `ternip_vector_registers` uses BRAM, not
  LUTs, for storage; the LUT cost is dominated by the
  `ternip_pipelined_mem`'s decoupled-ready buffer (~1k LUTs) +
  the BRAM-port mux. Reporting it as "LUT-equivalent for BRAM"
  with a /9 hack is fine for visualization purposes.
- **`_est_instruction_decode`** returns `InstructionWidth * 4 = 128
  * 4 = 512`. RTL: gbfifo_instruction (~200 LUTs) + per-core
  `instr_fsm_q` (~50 LUTs). 512 is on the high side but right
  zone.
- **`_est_xrt_shell`** returns 174000. Right per the comment
  ("real builds, ~174k LUTs ~ 10% of AU250"). OK.
- **`_est_ternip_core`** sums MOA + IV + EV + RMS + LS + RW + VR +
  IDC = 14336+8192+8192+65536+32768+512+7281+512 ≈ 137k. RTL
  per-core is roughly 50-80k LUTs at TP=128 (smaller than the
  estimate by ~2x). The biggest discrepancy is in the RMS estimator
  which is too high. NumDdrBanksPerTmatmul variant doesn't actually
  use this _est_ternip_core formula (it's only used by
  NumSeparateAxiInstances for its ternip_core node); the
  per-component formulas are used directly for the per-node nodes.

## Open questions for the user

1. **MOA cardinality.** The user's CLAUDE.md lists
   `multioperand_accumulator[bank0..3]` as 4 separate top-level
   nodes. But in the RTL, MOAs are not bank-resident; they are
   `bank_lane[b].row[r].multioperand_accumulator` inside *each*
   tmatmul (so they replicate with BatchSize × N). Should the
   visualizer:
   - (a) Create N MOAs at top-level (one per bank, shared across
     cores) — matching the user's mental model but not the RTL?
   - (b) Create BS * N MOAs (one per [core, bank] pair) — matching
     the RTL?
   The audit assumes (b) is the intent based on "module-level
   signals on FU boundaries only" — but flagging because the
   user's spec text is (a).

2. **importvector cardinality** is genuinely different between
   variants:
   - NumSeparateAxiInstances: each AXI instance has its own
     importvector. BS * NumAxi instances total.
   - **NumDdrBanksPerTmatmul: ONE shared importvector per
     core (per tmatmul). Total = BatchSize.** This is the RTL
     reality but conflicts with the user's CLAUDE.md
     `tmatmul_importvector[bank0..3]` enumeration.
   - NumTmatmulBanksPerCore: N importvectors per core (one per
     column-slice tmatmul_unit). Total = BatchSize * N.
   Visualizer currently has it right for the NumDdrBanksPerTmatmul
   variant (1 per core). User should confirm this matches their
   architectural intent for this variant.

3. **InstrFetchWidth vs InstructionWidth.** Should the
   `xrt_shell -> instruction_decode` edge use `InstrFetchWidth`
   (32, the AXI-stream width from XRT) or `InstructionWidth`
   (64-128, the decoded opcode width)? The visualizer's formula
   string says "InstrFetchWidth" but the computed value is
   `iw = InstructionWidth`. Resolving this requires picking which
   side of the gbfifo_instruction the edge represents.

4. **Should `tmatmul_desc_slice[b].buffer_tmatmul_desc` (the
   build_37 descriptor pipeline) be modeled as an edge from
   `ternip_core` -> `tmatmul_dma_b{b}`?** This is a real
   request-direction edge (address+length, 96 bits per bank,
   pipelined across SLRs) that the visualizer is missing. It
   matters for the "total wire length" metric since it's a
   cross-SLR edge.

5. **Should the visualizer's `loadstore -> dram_b0` edges
   include the build_43/44 pipelined-slice instances** as
   separate nodes (since they are explicit SLR-crossings with
   significant FF cost), or should they be folded into the edge
   property? Affects accuracy of the cell-count breakdown for the
   loadstore subsystem.

6. **Pipelined-interconnect node-or-edge?** The dominant FF mass
   in this design is in the `ternip_pipelined_interconnect`
   instances. They are pure cross-SLR pipeline registers — no FU
   logic — so should they be:
   - (a) Promoted to nodes (so the visualizer shows the SLR-to-SLR
     hops physically),
   - (b) Modeled as edge properties (each edge gets a "stages"
     attribute that affects rendered length and FF count without
     adding a node),
   - (c) Ignored entirely.
   The current model is (c); the realistic FF cost of the design
   makes (b) probably the right answer.

7. **`xrt_shell` as a node:** is the intent for this to be a
   *visual placeholder* (representing the static region of the
   AU250 platform that has fixed cell cost ~174k LUT), or a *true
   source node* in the graph (with edges representing kernel ports
   the platform actually drives)? The current model is closer to
   the second — the xrt_shell node only has the instruction edge.
   If the intent is the first, additional XRT edges for the
   m_axi data channels (DRAM-to-kernel back through XRT) should
   exist.

---

# AUDIT_NTB.md — NumTmatmulBanksPerCore variant

Commit audited: cf7838a (NumTmatmulBanksPerCore branch of ternary_matmul_claude)
Date: 2026-06-05

## Configuration assumed for numerical checks (MaxCores)

From `architectures/NumTmatmulBanksPerCore/config/xcu250_D=1024_MaxCores.svh`:

| Parameter                  | Value |
|----------------------------|------:|
| D                          | 1024  |
| TmatmulParallelism (TP)    | 128   |
| VectorParallelism (VP)     | 4     |
| LutParallelism             | 1     |
| FixedPointPrecision (FxP)  | 16    |
| BatchSize (BS)             | 1     |
| NumVectorRegisters         | 4     |
| NumTmatmulBanksPerCore (N) | 4     |
| DdrAddressWidth            | 64    |
| InstructionWidth           | 128   |
| InstrFetchWidth            | 32    |
| DdrDataWidth (DW)          | 512   |
| CoreInterconnectNumStages  | 8     |

Note: in `_DEFAULTS`, the visualizer also reads `NumDdrBanksUsed` (default 4).
The RTL does not expose a `NumDdrBanksUsed` parameter — `NumTmatmulBanksPerCore`
plays both roles: it sizes the number of per-bank DDR interfaces at the kernel
boundary (`m_axi_tmatmul_0..N-1`) AND the number of tmatmul UNITS per core.
The visualizer's `_build_NumTmatmulBanksPerCore` uses `NumDdrBanksUsed` (n) as
the bank count and also as the unit-per-core count (`for u in range(n)`). This
happens to match RTL semantics only because RTL uses a single parameter.
If the user ever drives the visualizer with `NumDdrBanksUsed != 4` or with
`NumDdrBanksUsed != NumTmatmulBanksPerCore`, the model will silently
diverge from RTL behavior.

### Key derived widths (MaxCores, N=4)

- `ImportVectorLength` (per unit) = D / N = 256
- `TP < ImportVectorLength` (128 < 256) → branch:
  - `RowParallelism = 1`
  - `DdrReadsPerRow = ImportVectorLength / TP = 2`
  - `ImportVectorRowWidth = min(TP, ImportVectorLength) = 128`
- `tmul_result_t` = `signed [FxP : 0]` = **17 bits** (note: `[FxP:0]` is FxP+1 bits)
- `tmatmul_stream_data_t` = `ternary_t [TP-1:0]` = TP × 2 = **256 bits**
- `vector_chunk_t` = `fixed_point_t [VP-1:0]` = VP × FxP = **64 bits**
- `instruction_t` = InstructionWidth = **128 bits**
- `ddr_address_t` = 64 bits
- `loadstore_ddr_stream` packed payload (addr + write_not_read + length) = 64 + 1 + 32 = **97 bits**
- `tmatmul_ddr_stream` packed payload per unit (`{addr, length}`) = 64 + 32 = **96 bits**
- `loadstore_ddr_r_data_i` / `loadstore_ddr_w_data_o` width
  = `vector_chunk_t [BatchSize-1:0]` = BS × VP × FxP = **64 bits** at BS=1
- For a hypothetical BS=2 the loadstore R/W width would be 128 bits.
- For RowParallelism=1 (MaxCores), `moa_out_bits = 1 × FxP = 16 bits`.
  The visualizer computes `row_parallelism = max(1, tp // ivl_per_bank)
  = max(1, 128 // 256) = max(1, 0) = 1`, so its `moa_out_bits = 16` — matches.

## Nodes

### Nodes the visualizer creates but RTL doesn't have

- **`xrt_shell`**: There is no `xrt_shell` module in the RTL submodule.
  XRT is the platform-side static region (PCIe, SmartConnect, the
  `xilinx_u250_gen3x16_xdma_4_1_202210_1` shell) that lives in the Vitis
  XSA — not in the kernel RTL. Modeling it as a single graph node is
  defensible for a deliverable-bitstream visualization but should be
  understood as "platform overhead block," not "kernel RTL module."
- **`axi_dma_instr`**: There is no module named `axi_dma_instr` in
  `axi_ternip_batched.sv` or the ternip tree. The actual kernel ingests
  instructions over an **AXI-Stream** slave port (`s_axis_instruction_*`)
  fed by a `ternip_gearbox_fifo` instance named `gbfifo_instruction` that
  width-converts `InstrFetchWidth=32` → `InstructionWidth=128`. So the
  instruction ingress is `s_axis_instruction (AXIS) → gbfifo_instruction
  → instruction_t`. Calling that ingress "axi_dma_instr" is a
  misleading label: there is no DMA — there is a streaming FIFO. The
  visualizer would be more honest with `instruction_axis_in` /
  `gbfifo_instruction`.
- **`instruction_decode`**: There is no standalone `instruction_decode`
  module in the RTL. Decoding is just the
  `unique case (instruction_i.fu) … endcase` in `ternip_core.sv`
  (`always_comb` at line 524) that fans out the `instruction_t` struct
  fields into per-FU `in_valid`/operand wires. There is no FIFO and no
  registered decode stage. The closest standalone object is the
  `gbfifo_instruction` width-converter mentioned above, which is in
  `axi_ternip_batched.sv`, not in the core. The visualizer's choice to
  model this as a node is fine for visualization, but it represents
  "comb fanout in ternip_core", not a real module instance.

### Nodes RTL has but visualizer doesn't model

- **`gbfifo_instruction`** (in `axi_ternip_batched.sv` line 169): a
  `ternip_gearbox_fifo` that width-converts `s_axis_instruction_tdata`
  (32 b) → `instruction_t` (128 b). Folded into `axi_dma_instr` by the
  visualizer.
- **`s_axi_ternip_rst`** (line 111): the AXI-Lite slave that captures
  resets from the host. Not modeled.
- **`s_axi_stall_rd` / `s_axi_stall_wr`** (lines 503, 528): the AXI-Lite
  slave port that exposes stall status and the unstall write. Not
  modeled.
- **`s_axi_debug`** (line 564): the AXI-Lite slave for the 0x00..0xb0
  debug register file. Not modeled.
- **`axi_dma_rd` (`dma_r_tmatmul`)**, replicated per bank (line 232):
  Alex Forencich's `axi_dma_rd` module — the actual AXI4 master that
  walks the AR/R channels to read the ternary matrix. The visualizer's
  `tmatmul_dma_b{b}` node represents this **plus** its associated
  `gbfifo_tmatmul` width-converter. Lumping them is reasonable as long
  as the area estimate covers both.
- **`gbfifo_tmatmul`** per bank (line 210): a `ternip_gearbox_fifo`
  that width-converts `DdrDataWidth=512` → `tmatmul_stream_data_t=256`
  (i.e. TP × ternary bits). This is the actual conversion logic the
  cell estimator's `dw*4 + tp*8` formula is trying to capture.
- **`axi_dma` (`dma_rw_loadstore`)** (line 381): the AXI4 master for the
  loadstore port — completely missing from the topology. The visualizer
  draws `loadstore[c] -> dram_b0` and `dram_b0 -> loadstore[c]` direct
  edges, but the real path is:
  - `ternip_loadstore` (inside `ternip_core`) drives the abstract
    `loadstore_ddr_*` ports
  - those ports cross `ternip_buffered`'s
    `buffer_loadstore_ddr_{stream,r,w}` pipelined interconnects
  - then go to `gbfifo_loadstore_r` (VP-wide ↔ DW) and
    `gbfifo_loadstore_w` (DW ↔ VP-wide) in `axi_ternip_batched`
  - then to `axi_dma` (`dma_rw_loadstore`), which is the actual AXI
    master
  - which drives `m_axi_loadstore_*` to DDR
- **`gbfifo_loadstore_r` / `gbfifo_loadstore_w`** (lines 322, 359):
  width-converters between DW=512 and `vector_chunk_t[BatchSize-1:0]`
  (= VP × FxP × BS = 64 bits at BS=1, 128 at BS=2). Not modeled.
- **`ternip_buffered`** wrapper: the visualizer treats `ternip_core`
  and `ternip_buffered` as one node. In reality `ternip_buffered`
  contains 4 + N×2 `ternip_pipelined_interconnect` instances:
  - `buffer_instruction` (DataWidth = $bits(instruction_i) = 128)
  - `buffer_loadstore_ddr_stream` (DataWidth = 97 — addr+wnr+length)
  - `buffer_loadstore_ddr_r` (DataWidth = $bits(loadstore_ddr_r_data_i)
    = 64 at BS=1)
  - `buffer_loadstore_ddr_w` (DataWidth = $bits(loadstore_ddr_w_data_o)
    = 64 at BS=1)
  - `buffer_loadstore_ddr_debug` (DataWidth = 64)
  - per unit u in [0..N): `buffer_tmatmul_ddr_stream[u]`
    (DataWidth = 96) and `buffer_tmatmul_ddr_r[u]`
    (DataWidth = $bits(tmatmul_stream_data_t) = 256)
  - These buffers exist precisely because SLR crossings between the
    kernel's per-bank DMA logic (placed near the DDR controllers, one
    per SLR) and the core's per-unit MOA inputs need
    `CoreInterconnectNumStages=8` registered stages. They are the
    NumTmatmulBanksPerCore variant's primary timing-closure mechanism
    and the natural place to attach SLR boundaries in a visualizer.
- **`ternip_vector_registers`**: the visualizer DOES create a
  `vector_registers` node per core, but its internal structure (a single
  `ternip_pipelined_mem` with `DECOUPLED_READY=1`) is hidden. That's
  expected and OK.
- **`ternip_pipelined_mem` (`importvector`, `exportvector`)**: the
  visualizer correctly creates these as nodes per unit. Each is a
  `ternip_pipelined_mem` of a specific depth (DdrReadsPerRow=2 for IV,
  NumChunksPerVector=256 for EV at D=1024,VP=4).
- **`ternip_gearbox_fifo` (`gbfifo_import` and `gbfifo_export`) inside
  every `ternip_tmatmul`**: the in-unit width adapters between
  vector_chunk_t and the ImportVectorRowWidth-wide row format. Not
  modeled directly — folded into IV/EV or the unit boundary.
- **Math support modules** (`ternip_mul`, `ternip_div`,
  `ternip_fixed_point_convert`, `ternip_sqrt`, sigmoid/silu LUTs):
  used inside `ternip_rms` and `ternip_rowwise_operation`. Folded into
  the RMS / rowwise_op nodes, which is reasonable.

### Nodes that exist in both but with wrong cardinality

- **`tmatmul_dma[b]`**: visualizer creates `n = NumDdrBanksUsed = 4`
  instances and RTL also instantiates the `tmatmul_dma` generate block
  N = `NumTmatmulBanksPerCore` = 4 times. **Cardinality matches** when
  `NumDdrBanksUsed == NumTmatmulBanksPerCore`, which is the only
  supported configuration. If `NumDdrBanksUsed != n` is ever set, the
  visualizer will diverge.

- **`tmatmul_unit[u]` per core**: visualizer creates `n=4` tmatmul_units
  per core (`for u in range(n)`). RTL instantiates `N_TMATMUL =
  NumTmatmulBanksPerCore` units per core (`for u_GEN = 0; u_GEN <
  N_TMATMUL`). **Cardinality matches** with the same caveat — the
  visualizer is iterating over `NumDdrBanksUsed`, not over
  `NumTmatmulBanksPerCore`. They happen to be the same number, but the
  conceptual confusion is real: the iteration in the visualizer is
  "for each DDR bank, place a unit", while the iteration in RTL is
  "for each column slice (= each bank), place a unit". This is correct
  in this variant but is the kind of confusion that breaks when
  visualizer parameters are stretched.

- **`MOA[u] (core c)`**: visualizer creates one MOA node per (c,u). RTL
  per unit instantiates `RowParallelism = max(1, TP/ImportVectorLength)`
  MOA instances (in a `for (genvar i_GEN = 0; i_GEN < RowParallelism)`
  loop inside `ternip_tmatmul`). At MaxCores (TP=128, IVL=256),
  RowParallelism = 1, so the visualizer's "one MOA per unit" matches
  RTL. **But the MOA cell-count formula in `_est_MOA` uses TP**
  unconditionally (`count = tp * fxp * depth`, depth = `clog2(TP)`)
  while the RTL MOA's NUM_OPERANDS is `ImportVectorRowWidth =
  min(TP, IVL) = 128` (at MaxCores) — happens to equal TP, so the
  number matches. If a future config has IVL < TP, the per-MOA size
  would be `ImportVectorRowWidth * FxP * clog2(IVL)`, not
  `TP * FxP * clog2(TP)`, AND there would be `TP/IVL` MOA instances
  per unit, none of which the visualizer models. (At MaxCores, this
  doesn't bite — but flagging it for the discrepancy ledger.)

- **`importvector[u] (core c)`**: visualizer creates one per (c,u).
  RTL: each tmatmul unit instantiates one `ternip_pipelined_mem`
  named `importvector` of depth `DdrReadsPerRow = max(1, IVL/TP) = 2`.
  **Cardinality matches**. The visualizer's `_est_importvector`
  formula is `(D / NumDdrBanksUsed) * FixedPointPrecision * 2`. The
  RTL data width per entry is `ImportVectorRowWidth * FxP = 128 * 16
  = 2048 bits`, and depth is `DdrReadsPerRow = 2`. Total cell-eq:
  ~`ImportVectorRowWidth * FxP * DdrReadsPerRow = 4096`. The
  visualizer's number `(D/N)*FxP*2 = 256 * 16 * 2 = 8192` is 2× too
  high (it's using IVL = D/N as the multiplicand when the RTL
  effective width × depth = `ImportVectorRowWidth * DdrReadsPerRow`,
  which always equals `ImportVectorLength = D/N`, BUT the second `*2`
  in the formula appears to be an arbitrary fudge factor). Coarse
  estimate — accept-able as a visualization size, just noting.

- **`exportvector[u] (core c)`**: visualizer creates one per (c,u).
  RTL: one `ternip_pipelined_mem` per unit, depth =
  `NumChunksPerVector = D/VP = 256` (at MaxCores), data width =
  `vector_chunk_t = VP * FxP = 64` bits. Total cell-eq:
  256 * 64 = 16384 bits stored. The visualizer's formula
  `(D / NumDdrBanksUsed) * FixedPointPrecision * 2 = 256 * 16 * 2 =
  8192` is **wrong-shaped** for the export vector: it depends on
  `D/N` but the actual EV depth depends only on `NumChunksPerVector =
  D/VP`, not on N. At MaxCores N=4 and VP=4 the numbers coincidentally
  match in `D/4`-vs-`D/4` ratio, but if N≠VP, the visualizer's EV
  size scales with N while the RTL EV size doesn't. Flagging as a
  cell-estimate cardinality/shape discrepancy.

- **`vector_registers[c]`** (per core, NOT per unit): visualizer
  correctly creates ONE per core. RTL: `ternip_vector_registers
  vector_registers` is instantiated once inside `ternip_core`, with a
  SINGLE port (request_* + read_*). All FUs — `loadstore`,
  `rowwise_operation`, `rms`, and all N `tmatmul_units[u]` — share that
  single port through an OR-mux + `unique case (1)` priority arbiter
  in `ternip_core.sv` (lines 462–500). The visualizer model is
  correct on cardinality (1 per core), but the topology connects only
  `iv` and `ev` per unit to `vr` per core (see Edges below) — it does
  NOT model the request/read arbitration for the other 3 FUs
  separately. See Edges discussion for the missing edges.

- **`ternip_core[c]`**: visualizer creates one per core (per BS). RTL:
  `ternip_buffered` wraps `ternip_core` and is instantiated `BatchSize`
  times in `ternip_batched`. The visualizer correctly models 1
  `ternip_core` per BS replicate (no `ternip_buffered` separately).

- **`rms[c]` / `loadstore[c]` / `rowwise_op[c]`**: visualizer creates
  one per core. RTL: one `ternip_rms`, one `ternip_loadstore`, one
  `ternip_rowwise_operation` per `ternip_core`. **Matches.**

- **DRAM banks**: visualizer creates `n = NumDdrBanksUsed = 4`.
  RTL: there are 4 `m_axi_tmatmul_<b>` masters plus 1 `m_axi_loadstore`
  master. In Vitis the kernel.cfg maps each `m_axi_*` to one DDR4 bank
  in the U250 platform. So `dram[0..3]` correspond to four banks; the
  loadstore convention in the visualizer (`ls -> dram_b0`) treats DDR0
  as the loadstore-attached bank, while the four tmatmul DMA masters
  use DDR0..DDR3. **In practice** Vitis kernel.cfg pins one master per
  bank: `dma_r_tmatmul[b]` to DDR[b], and `dma_rw_loadstore` may or
  may not collide with DDR[0]. The visualizer's choice to draw the
  loadstore edge to `dram_b0` is a placeholder. **Discrepancy is minor**
  but worth a note: the visualizer doesn't surface whether loadstore
  shares bank-0 with tmatmul_dma[0] (which has implications for DDR
  bandwidth contention).

## Edges

For the MaxCores config (BS=1, N=4), the visualizer's
`_build_NumTmatmulBanksPerCore` produces these edges. I'll group by
region and compare each to RTL.

### Region: DRAM / AXI surface (per bank)

#### `dram_b{b}` -> `tmatmul_dma_b{b}` (n=4 edges)

- **Visualizer**: `bus_bits = DdrDataWidth = 512`, formula =
  "DdrDataWidth (R-channel)".
- **RTL**: `axi_dma_rd` (`dma_r_tmatmul[b]`) issues AR/R on
  `m_axi_tmatmul_<b>`. The R-channel data bus is `DdrDataWidth = 512`
  bits, which then feeds `gbfifo_tmatmul.in_data_i` (line 249) — that
  matches.
- **Matches**.
- Side note: this edge silently aggregates the AW/AR/R/W/B channel
  widths down to "just R-channel data" — fine for visualization, the
  control channels are <100 bits each.

#### `ls_c{c}` -> `dram_b0` and `dram_b0` -> `ls_c{c}` (1 edge each, BS=1)

- **Visualizer**: `bus_bits = DdrDataWidth = 512` in both directions.
- **RTL**: `m_axi_loadstore_*` is the master interface. Data widths
  are `DdrDataWidth = 512` for `wdata`/`rdata`. The actual logical path
  is `loadstore -> ternip_buffered.buffer_loadstore_ddr_w ->
  gbfifo_loadstore_w -> axi_dma -> m_axi_loadstore_wdata -> DRAM` (write)
  and the inverse for read.
- **Discrepancy (minor)**: the W-channel from `ternip_loadstore` (inside
  the core) is **not** DW=512 wide. It is
  `vector_chunk_t[BatchSize-1:0] = VP*FxP*BS = 64` bits at BS=1 (or 128
  at BS=2). The gearbox converts 64 → 512, but at the **loadstore
  module's** output, the wire is 64. The visualizer collapses the whole
  loadstore↔DRAM path to a single "DdrDataWidth (R-channel)" edge,
  which over-reports the bus_bits inside the kernel (where most of the
  layout / routing actually matters). At the kernel-to-DRAM boundary,
  DW=512 is correct.

### Region: Shared infrastructure (instruction path)

#### `xrt_shell` -> `instruction_decode` (1 edge)

- **Visualizer**: `bus_bits = InstructionWidth = 128`, formula =
  "InstrFetchWidth (instructions from host via XRT)".
- **RTL**: the host-facing path is an AXI-Stream
  (`s_axis_instruction_*`) with `tdata` width = `InstrFetchWidth = 32`
  bits (per the config). After width-conversion in `gbfifo_instruction`,
  the consumer sees an `instruction_t = 128` bits.
- **Discrepancy**: bus_bits is wrong. The wire crossing the
  XRT-kernel boundary is **32 bits** (`InstrFetchWidth`), not 128.
  The formula text says "InstrFetchWidth (instructions from host via
  XRT)" but the value passed is `iw = InstructionWidth = 128`. Either
  the formula label is wrong or the bus_bits is wrong; they're
  inconsistent with each other.

#### `axi_dma_instr` -> `instruction_decode` (1 edge)

- **Visualizer**: `bus_bits = InstructionWidth = 128`, formula =
  "InstructionWidth (AXI DMA -> decoder)".
- **RTL**: After `gbfifo_instruction` width-converts 32→128, the
  full-width `instruction_t = 128` bits goes into the core's
  `instruction_i` port. There is no separate `axi_dma_instr` module;
  this edge represents the width-converted bus inside
  `axi_ternip_batched`. **Width matches** (128); only the source-node
  label is fictional.

#### `instruction_decode` -> `ternip_core_c{c}` (BS edges, BS=1)

- **Visualizer**: `bus_bits = InstructionWidth = 128`, formula =
  "InstructionWidth (decoded -> core)".
- **RTL**: `ternip_buffered.buffer_instruction` is a
  `ternip_pipelined_interconnect` with `DataWidth = $bits(instruction_i)
  = 128`, `NumStages = CoreInterconnectNumStages = 8`. So the instruction
  bus DOES cross 8 register stages on the way to the core. The bus_bits
  matches at 128.
- **Hidden detail**: the visualizer doesn't show the 8-stage
  pipelined interconnect. The full RTL cost is `128 * 8 = 1024 FFs`
  for this one bus alone. That's invisible in the topology.
- **Matches** numerically; structurally simplified.

### Region: Tmatmul broadcast (the critical region for this variant)

#### `tmatmul_dma_b{b}` -> `tmatmul_unit_c{c}_u{u}` for all (b, c, u)

This is the **broadcast R-channel**. Visualizer emits one edge per
(b, c, u) tuple → for BS=1, N=4: 4 banks × 1 core × 4 units = **16
edges**.

- **Visualizer**: `bus_bits = TmatmulParallelism * 2 = 256`, formula =
  "TmatmulParallelism * 2 (ternary stream, broadcast)".
- **RTL semantics**: per `axi_ternip_batched.sv` line 159 and
  `ternip_batched.sv` lines 230–240, `tmatmul_ddr_r_data_i[b]` (one per
  bank b) is broadcast to every core's per-bank handle, i.e.
  `core_tmatmul_ddr_r_data_i[i][u] = tmatmul_ddr_r_data_i[u]`.
  Reading that line carefully: **the b<->u index is identical here**.
  The fabric does NOT cross-broadcast every bank to every unit. It
  pairs them up: `tmatmul_dma[b]` only feeds the `u=b` slot in each
  core's array. The same bank-index is used on both sides.
- **CRITICAL DISCREPANCY**: the visualizer's N×N×BS broadcast is
  WRONG. The actual topology is N×BS edges (`b -> (c, u=b)` for every
  (c, b)), not N×N×BS. Concretely, with N=4, BS=1, the RTL has 4
  edges (`tmatmul_dma_b0 -> tmatmul_unit_c0_u0`,
  `tmatmul_dma_b1 -> tmatmul_unit_c0_u1`, ...,
  `tmatmul_dma_b3 -> tmatmul_unit_c0_u3`). The visualizer draws 16.
  **The user's spec in the audit prompt says "broadcast R-channel from
  tmatmul_dma[b] -> all N tmatmul_units" and that the visualizer
  should have N×N edges, but the RTL is NOT a broadcast across all
  units — it's a 1-to-1 pairing by bank index between tmatmul_dma[b]
  and tmatmul_unit[u=b]**. The only broadcast is across CORES (so for
  BS>1, the same bank's data goes to core[0..BS-1]'s `u=b` unit).
  Visualizer's code:
  ```python
  for u in range(n):
      ...
      for b in range(n):
          edges.append(_make_edge(
              f"tmatmul_dma_b{b}", unit_id, tp * 2,
              "TmatmulParallelism * 2 (ternary stream, broadcast)",
          ))
  ```
  This nests b inside u, drawing every bank to every unit.
- The bus_bits value (TP*2 = 256) **is correct** for one of these
  edges (`tmatmul_stream_data_t = ternary_t[TP-1:0] = 2*128 = 256`).
- Bottom line: **width per edge is right, edge-count per core is 4×
  too high (16 instead of 4)**. This is the single largest topology
  error in this variant.

(For completeness: even granting the spec interpretation "user wants
N×N broadcast" — see prompt — the RTL does not behave that way. The
prompt says "Each tmatmul_unit handles a column-slice of the matmul
(ImportVectorLength = D / N)" and then "tmatmul_dma[b] modules sit
OUTSIDE the core and broadcast their R-channel to all N
tmatmul_units". The RTL contradicts the second clause: there is no
N-way broadcast, only a 1-to-1 bank-to-unit pairing inside each core,
combined with a BS-way core broadcast for the same bank. The variant
gets its bandwidth benefit from the fact that each unit handles its
own column slice from its own bank, NOT from any unit seeing data
from all banks.)

#### `tmatmul_dma_b{b}` -> `instruction_decode` / etc. — NONE

- The visualizer correctly does NOT add edges between `tmatmul_dma`
  nodes and `instruction_decode`, which is right — they're orthogonal
  paths.

### Region: Per-unit tmatmul internals (per (c, u))

For each (c, u), the visualizer emits:

#### `iv_c{c}_u{u}` -> `moa_c{c}_u{u}`

- **Visualizer**: `bus_bits = TP * FxP = 128 * 16 = 2048`, formula =
  "TmatmulParallelism * FixedPointPrecision (wide activation)".
- **RTL**: the actual signal feeding the MOA from importvector is
  `importvector_read_data` which is
  `fixed_point_t [ImportVectorRowWidth-1:0]`.
  At MaxCores, `ImportVectorRowWidth = min(TP, IVL) = min(128, 256) =
  128`, so width = 128 * 16 = 2048. **Matches**.
- However the MOA's actual `in_operands_i` is `tmul_result_t
  [ImportVectorRowWidth-1:0]` = `[16:0][127:0]` = **17 × 128 = 2176**
  bits, since `tmul_result_t = signed [FixedPointPrecision:0]` (FxP+1
  bits). The visualizer's 2048 is the importvector read width;
  the multiplied values are 2176. Close enough for visualization but
  the MOA INPUT bus is technically 2176 not 2048.

#### `moa_c{c}_u{u}` -> `ev_c{c}_u{u}`

- **Visualizer**: `bus_bits = moa_out_bits = RowParallelism * FxP =
  1 * 16 = 16`, formula references "RowParallelism * FixedPointPrecision".
- **RTL**: the MOA's `out_result_o` per row is one
  `fixed_point_t = 16 bits`, and there are `RowParallelism = 1` rows,
  so the bus into `gbfifo_export` is `fixed_point_t[RowParallelism-1:0]
  = 16 bits` total. After `gbfifo_export` it's `vector_chunk_t = VP*FxP
  = 64` bits going into exportvector. The visualizer collapses the
  gbfifo and shows 16 b directly. **Matches** for the MOA→gbfifo half;
  the gbfifo→EV half (64 b) is hidden.

#### `moa_c{c}_u{u}` -> `tmatmul_unit_c{c}_u{u}` (the "MOA result -> unit")

- **Visualizer**: `bus_bits = moa_out_bits = 16`, formula = "MOA result".
- **RTL**: This edge doesn't represent a distinct wire in the RTL — the
  MOA is **inside** the `ternip_tmatmul` unit. The "unit" is the
  module that contains the MOA, IV, EV, gbfifos, and the state machine.
  So saying "MOA -> unit" is modeling the MOA's result going to the
  unit-level state machine wires; that's fine for a hierarchical
  visualization. The bus_bits = 16 is technically what the FSM
  observes after MOA but before gbfifo_export (the
  `accumulator_result` signal). **OK** as a conceptual edge.

#### `tmatmul_unit_c{c}_u{u}` -> `ternip_core_c{c}`

- **Visualizer**: `bus_bits = moa_out_bits = 16`, formula = "unit
  result, MOA-reduced".
- **RTL**: This edge is **wrong-shape**. There is no `unit -> core`
  result bus. The unit's output goes through the `gbfifo_export`
  inside it, into the exportvector BRAM (inside the same unit), and
  then on EXPORT instruction, exportvector reads chunks back out to
  the **vector_registers** (via the vector_request/vector_read port).
  So the actual data flow from `tmatmul_unit[u]` to anywhere outside
  the unit is:
  - For "compute output": exportvector reads → `vector_request_w_data_o`
    of width `vector_chunk_t = 64` bits → vector_registers (shared per
    core).
  - There is no per-unit bus into the `ternip_core` node itself
    carrying MOA results.
- **DISCREPANCY**: the edge `tmatmul_unit -> ternip_core` with 16 bits
  is spurious. The unit communicates with `ternip_core` only through:
  - in_ready_o / in_valid_i / in_operation_i / in_go_matrix_address_i
    / in_vector_select_i (the instruction dispatch — ~70 bits)
  - vector_request_* and vector_read_* (which actually go to
    `vector_registers`, not to `ternip_core` directly)
  - ddr_stream_* and ddr_r_* (which exit the core to the per-bank DMA)
- The "MOA result" bus 16 bits never crosses the unit boundary.

#### `vr_c{c}` -> `iv_c{c}_u{u}` (for each u)

- **Visualizer**: `bus_bits = VP * FxP = 64`, formula = "VP * FxP".
- **RTL**: The IMPORT-phase data flow is: `vector_registers.read_data_o`
  = `vector_chunk_t = VP*FxP = 64` bits → `gbfifo_import.in_data_i`
  (in `ternip_tmatmul`) → after width-conversion, into
  `importvector.request_w_data_i` at width `ImportVectorRowWidth*FxP =
  2048`. The visualizer collapses the gbfifo and draws a 64-bit edge
  from VR to IV directly. **Bus_bits matches at the VR side (64)**.
- However: the path from `vector_registers` to `importvector` is NOT
  point-to-point per unit. The `vector_request_*` / `vector_read_*`
  ports are shared (OR-muxed) across **all** FUs (loadstore, rms,
  rowwise_operation, and all N tmatmul_units). The visualizer draws
  4 edges (one per u from `vr` to each `iv_u`) but in RTL there is
  literally ONE port out of `vector_registers`, fanned out to all
  consumers comb. The 4 edges over-represent dedicated point-to-point
  buses; the real RTL is one bus with combinational fanout. (Whether
  this matters for the visualization is a judgment call; for "wires in
  this bus" the answer is "VP*FxP = 64 to each consumer", and there
  are indeed 4 destinations.)

#### `ev_c{c}_u{u}` -> `vr_c{c}` (for each u)

- **Visualizer**: `bus_bits = VP * FxP = 64`, formula = "VP * FxP".
- **RTL**: EXPORT-phase flow: `exportvector.read_data_o` =
  `vector_chunk_t = 64` bits → `vector_request_w_data_o` of width
  `vector_chunk_t = 64` bits → `vector_registers.request_w_data_i`.
  **Bus_bits matches**.
- Same caveat as above: there are N=4 export sources, but they all
  share the vector_request_* port via OR-mux. The visualizer's 4
  separate edges represent the 4 conceptual flows but not the actual
  arbitrated wire.

### Region: Other per-core FUs

#### `ternip_core_c{c}` -> `rms_c{c}`

- **Visualizer**: `bus_bits = VP * FxP = 64`, formula = "VP * FxP".
- **RTL**: the actual `ternip_core` -> `ternip_rms` instruction
  dispatch is:
  - `rms_in_valid` (1 bit)
  - `rms_in_op` (`rms_op_e` = 3 bits)
  - `rms_in_vector1_select` (`vector_select_t` = clog2(NVR) = 2 bits)
  - `rms_in_vector2_select` (2 bits)
  - `rms_in_length` (`immediate_t` = ImmediateWidth = 16 bits)
  - and `rms_in_ready` (1 bit) back
  - Total ≈ 25 bits forward, 1 back.
- Plus `rms` <-> `vector_registers`: `vector_chunk_t = 64` for
  request_w_data, 64 for read_data, plus address/select bits.
- The visualizer's 64-bit edge from `core` -> `rms` represents
  neither the instruction-dispatch path (25 bits) nor the VR-data
  path (64 bits going through VR, not core). It's a placeholder
  meaning "core dispatches one chunk-sized op per cycle". **Off-shape
  but not flagrantly wrong** for sizing purposes; documenting.

#### `ternip_core_c{c}` -> `ls_c{c}` / `rw_c{c}` / `vr_c{c}`

- **Visualizer**: same 64 bits each, "VP * FxP".
- **RTL**: same kind of caveat as rms. The actual buses are
  instruction-dispatch (control bits, ~30) on the core->FU side, and
  VR-data (64 b) on the FU<->VR side. The visualizer's "core -> FU"
  64-bit edge is again a sizing placeholder.

### Missing edges (RTL has, visualizer doesn't)

1. **Per-unit dedicated `tmatmul_dma[b] -> tmatmul_unit[u=b]` (NOT
   broadcast)**: see big section above. The visualizer's broadcast is
   N×N edges where RTL has 1-to-1 pairing N edges per core.
2. **vector_registers <-> rms / loadstore / rowwise_op**: the
   visualizer draws `ternip_core -> rms` etc., but the actual data bus
   `vector_chunk_t = 64 b` flows between each FU and
   `vector_registers` (request and read), not through `ternip_core`.
   No edge between `vr` and `rms`, `vr` and `ls`, `vr` and `rw` is
   drawn — only `vr <-> iv_u` and `ev_u <-> vr` for the tmatmul
   units. This is a missing edge set: 3 edges (rms, ls, rw) × 2
   directions = 6 missing edges per core.
3. **ternip_loadstore <-> AXI-DMA-loadstore**: the chain
   `loadstore -> ternip_buffered -> gbfifo_loadstore_w -> axi_dma ->
   m_axi_loadstore_*` is collapsed into a single `loadstore -> dram_b0`
   edge. At minimum the path crosses (a) the
   `buffer_loadstore_ddr_{stream,r,w,debug}` pipelined interconnects
   in `ternip_buffered`, and (b) the `gbfifo_loadstore_{r,w}` width
   converters. None are modeled.
4. **CoreInterconnect pipelined buffers (`ternip_buffered`)**: 4 + 2N
   pipeline registers per BS replicate, with widths 128/97/64/64/64
   for the loadstore-side ones and 96/256 per unit. None of these
   stages are visualizer nodes or annotated on the edges they
   pipeline. Given that these buffers are precisely the SLR-crossing
   timing-closure mechanism for this variant, this is a major
   structural omission for any visualization intended to illuminate
   timing.
5. **`gbfifo_tmatmul` width converters** (one per bank, DW→TP*2 =
   512→256): the per-bank `tmatmul_dma_b{b}` node folds together
   `axi_dma_rd` AND `gbfifo_tmatmul`. The 512-bit half (between
   `axi_dma_rd` and `gbfifo_tmatmul`) and the 256-bit half (after
   `gbfifo_tmatmul`, going to the core) are different physical paths
   with different SLR placement implications. Both are inside the
   visualizer's `tmatmul_dma` node.
6. **`gbfifo_import` / `gbfifo_export` width converters inside each
   tmatmul unit**: 64 ↔ 2048 width changes between vector_chunk_t and
   ImportVectorRowWidth-wide row format. Folded inside the unit /
   IV / EV node. (Same critique as above — fine for a high-level
   visualization, surfaces hidden gearbox area.)
7. **`s_axi_*` control-plane ports** (`s_axi_ternip_rst`,
   `s_axi_stall_rd`, `s_axi_stall_wr`, `s_axi_debug`): four AXI-Lite
   slaves on the kernel. Drawn as nothing — they don't exist in the
   visualizer. Smallest-impact omission since these are tiny.
8. **`buffer_loadstore_ddr_debug`** (line 174 in ternip_buffered):
   the 64-bit debug bus crossing CoreInterconnectNumStages stages.
   Trivially small but the omission is real.

### Spurious edges (visualizer has, RTL doesn't)

1. **(N-1)/N of the broadcast edges from tmatmul_dma[b] to
   tmatmul_unit[u] for u≠b**: the visualizer draws all 16; RTL has
   only the 4 diagonal pairings. So 12 of 16 are spurious per core.
2. **`tmatmul_unit_c{c}_u{u}` -> `ternip_core_c{c}`** (1 per unit per
   core, 4 per core): this 16-bit "MOA-reduced unit result" edge does
   not correspond to a real bus from the unit to the core. The unit's
   results exit only via the shared vector_registers port (after
   EXPORT), not via a private wire to the core. 4 spurious edges per
   core.
3. **`xrt_shell` -> `instruction_decode`**: arguable, since the
   visualizer is modeling the platform side. The 32-bit ingress is
   real, but it's labeled with the wrong width (128) and the wrong
   formula tag. Either fix the label or accept the abstraction.
4. **`axi_dma_instr` -> `instruction_decode`**: this is the
   `gbfifo_instruction` width-converter bus — but the visualizer
   labels it as if it were a DMA, and the prior edge
   (`xrt_shell -> instruction_decode`) is the upstream of this one.
   The two edges form a path `xrt_shell -> axi_dma_instr ->
   instruction_decode`, which is correct as a chain but the labels
   are off.

## Cell-count formulas (per-node sanity check)

| node_type             | Visualizer formula                                              | RTL reality (MaxCores)                                                                                                  | Verdict |
|---|---|---|---|
| DRAM                  | 0                                                                | external                                                                                                                | OK |
| axi_dma_instr         | 500 (fixed)                                                      | Approximate; gbfifo_instruction depth × 32-to-128 width converter is more like ~500 LUTs                                | OK shape |
| tmatmul_dma           | dw*4 + tp*8 = 512*4 + 128*8 = 3072                              | axi_dma_rd ~1.5k LUTs + gbfifo_tmatmul (~500 LUTs) per bank ~2k LUTs total                                              | High by ~50%; OK shape |
| MOA                   | tp*fxp*clog2(tp) = 128*16*7 = 14336                              | RTL MOA per unit has NUM_OPERANDS = ImportVectorRowWidth (= 128 here), operand width 17 b → ~128*17*7 = 15232 LUTs       | Close, OK |
| importvector          | (D/N)*fxp*2 = 256*16*2 = 8192                                    | ternip_pipelined_mem, depth=DdrReadsPerRow=2, data width=128*16=2048 → ~2048*2 = 4096 storage bits ≈ 256 LUTs (BRAM-backed) | Over by 30× if accounting BRAM as not LUTs; "LUT-equiv" coarse OK |
| exportvector          | (D/N)*fxp*2 = 8192                                               | depth=NumChunksPerVector=256, data=VP*FxP=64 → 256*64 = 16384 storage bits ≈ ~1 BRAM                                    | Magnitude OK but shape wrong: doesn't depend on N |
| tmatmul_unit          | MOA + IV + EV + 200                                              | sum of children + the gbfifos + state machine — reasonable                                                              | OK |
| RMS                   | D*fxp*4 = 1024*16*4 = 65536                                      | Includes square-multiply tree, accumulator, sqrt, divide — large module, but D*FxP*4 is way too high (real ~10–15k LUTs) | Off by ~4–5× |
| loadstore             | D*fxp*2 = 32768                                                  | Compared to RMS this is also high; loadstore is mostly state machine + counters + some shifters ~2–3k LUTs              | Off by ~10× |
| rowwise_op            | vp*fxp*8 = 4*16*8 = 512                                          | LutParallelism=1, multiplier + divider + sigmoid LUTs + state machine; ~3–5k LUTs                                       | Low by ~10× |
| vector_registers      | nvr*D*fxp/9 = 4*1024*16/9 = 7281                                 | NumVectorRegisters*D*FxP = 65536 bits total = ~2 BRAMs; "LUT-equiv = bits/9" is the visualizer convention                | OK by convention |
| instruction_decode    | iw*4 = 128*4 = 512                                               | comb fanout in ternip_core, ~0 LUTs (it's just decode logic with no FFs)                                                | Over by infinity; placeholder |
| ternip_core           | sum of children                                                  | Yes, this is what the sum should be in the variant that uses one compound ternip_core (NumSeparateAxiInstances). Here a `ternip_core` node is created separately per BS replicate plus children — risk of double-counting if displayed |
| xrt_shell             | 174000 (fixed)                                                   | Per-build numbers from `vivado-utilization` reports, real ~150–180k LUTs                                                | OK |

**The biggest cell-count concern**: at MaxCores, the `ternip_core` node
estimates the whole compound (MOA + IV + EV + RMS + LS + RW + VR + IDC)
again, but the visualizer also creates **separate** nodes for RMS,
loadstore, rowwise_op, vector_registers, tmatmul_unit (and its children
MOA/IV/EV). If the GUI sums node cell_count for "total area", every
per-core unit's cells will be double-counted under `ternip_core`. This
is a presentation bug, not a topology bug. Worth checking the GUI's
total-cell aggregator.

## Cross-cutting observations

### Parameter aliasing — `NumDdrBanksUsed` vs `NumTmatmulBanksPerCore`

The visualizer reads `NumDdrBanksUsed` from `_DEFAULTS` and uses it for
BOTH:
- the number of DRAM bank nodes (correct for "physical DDR banks used")
- the number of tmatmul UNITS per core (incorrect — should be
  `NumTmatmulBanksPerCore`)

In MaxCores these are both 4, so things line up. But the abstraction
is wrong. If a future config wants 4 physical DDR banks with only 2
tmatmul units (or 4 units with only 2 DDR banks used), the visualizer
will model it incorrectly. The fix is to introduce a separate
`NumTmatmulBanksPerCore` parameter and use it for the per-unit loop.

### Missing: the broadcast across cores (BS dimension)

For BS>1, RTL broadcasts each bank's R-data to every core's
corresponding `u=b` unit (`core_tmatmul_ddr_r_data_i[i][u] =
tmatmul_ddr_r_data_i[u]` in `ternip_batched.sv` line 236). The
visualizer's loop is:
```python
for c in range(bs):
    for u in range(n):
        for b in range(n):
            edges.append(... tmatmul_dma_b{b} -> unit ...)
```
So as written it actually DOES create N×N edges per core × BS cores.
At BS=2, N=4 this means 32 edges, all of which would over-represent
the broadcast. The correct topology is N×BS edges total
(`tmatmul_dma[b] -> tmatmul_unit_c{c}_u{b}` for every (c, b)).

### Buffered (SLR-crossing) interconnects are invisible

`ternip_buffered` is the structural mechanism that makes this variant
work at 300 MHz across the U250's 4 SLRs. The visualizer treats it as
zero-cost, zero-edge. For the goal stated in `CLAUDE.md`
("optimize-tokens/second-via-RTL-changes"), the pipelined
interconnects are exactly what gets resized
(`CoreInterconnectNumStages`) when tuning. A visualizer that doesn't
show them is missing the lever most relevant to the build.

### `loadstore` -> `dram_b0` is convention, not law

The Vitis kernel.cfg actually pins `m_axi_loadstore` to whichever DDR
bank the platform chooses; the visualizer's hardcoded `dram_b0` is a
convention. If the user ever changes `kernel.cfg`'s `sp=` mapping,
the visualizer will get the loadstore bank wrong silently.

## Open questions for the user

1. **Intent of the broadcast**: the audit prompt says "the visualizer
   should have N×N edges, not just N parallel edges" for the
   tmatmul_dma->unit topology. The RTL clearly does NOT have an N-way
   broadcast — `core_tmatmul_ddr_r_data_i[i][u] =
   tmatmul_ddr_r_data_i[u]` is a 1-to-1 pairing by bank index. **Is
   the visualizer modeling intent (= "user spec for this variant") or
   modeling RTL (= "what's actually instantiated")?** This audit
   treats RTL as authoritative per the prompt's last rule, so the
   N×N is flagged as a spurious-edge over-count.
2. **`NumDdrBanksUsed` semantics**: should the visualizer continue
   driving `n` from this single parameter, or split it into
   `NumDdrBanksUsed` (= DRAM bank node count, = number of
   tmatmul_dma instances at the kernel boundary) and
   `NumTmatmulBanksPerCore` (= units per core)? RTL uses one
   parameter for both purposes (they HAVE to match by construction).
3. **Vector registers port model**: in RTL the VR has one shared
   request/read port, OR-muxed across loadstore + rms + rowwise +
   N×tmatmul_units. Should the visualizer model this as a true
   star (one port, fanned out) — drawing N+3 conceptual edges from VR
   — or as a single fat edge labeled "shared port across all FUs"?
   Current visualizer draws only the (vr, iv_u) and (ev_u, vr)
   edges; rms/loadstore/rowwise are missing.
4. **Should `ternip_buffered`'s pipelined interconnects be modeled?**
   They're invisible but they're the timing-closure mechanism.
   Modeling them as edges with `NumStages` annotations or as nodes
   would make the visualization much more useful for the actual
   build-optimization loop.
5. **What's a single "node" — module instance or hierarchy region?**
   The current model mixes both: `tmatmul_dma` lumps `axi_dma_rd` +
   `gbfifo_tmatmul`, but `MOA` / `importvector` / `exportvector` are
   broken out separately even though they're all instantiated inside
   `ternip_tmatmul`. Consistency would help.
6. **Edge cardinality semantics for shared ports**: when N units share
   a VR port, is the edge from VR to "each unit" (drawing N edges of
   width VP*FxP) or "to all units" (one edge of width VP*FxP with N
   destinations)? The current code does the former for `vr<->iv/ev`
   but doesn't do it for `vr <-> rms/ls/rw`. Pick one model.
7. **Should the `ternip_core` node's cell count include children?**
   Risk of double-count if the GUI sums cells. Either zero out
   `ternip_core` here (children-as-area-source model) or zero out
   children (compound model), but not both.
