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
