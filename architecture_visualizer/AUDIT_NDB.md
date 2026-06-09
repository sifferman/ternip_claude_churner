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
