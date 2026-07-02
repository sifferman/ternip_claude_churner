# Interconnect SLR span map — NumDdrBanksPerTmatmul variant

Enumeration of every `ternip_pipelined_interconnect` instance in the
NDBPT design, their physical endpoints, nominal SLR span, and
recommended `NumStages` / `Implementation` per PG373 + UG949 guidance.
Used to drive per-instance parameter tuning in `ternip_buffered.sv` and
`axi_ternip_batched.sv`.

## AU250 SLR / DDR pinout (xilinx_u250_gen3x16_xdma platform)

- DDR[0] → SLR0
- DDR[1] → SLR1
- DDR[2] → SLR2
- DDR[3] → SLR3
- XRT static shell + kernel entry: SLR2 (typically)
- Kernel core region (per pblocks): SLR0+SLR1 for
  `dma_rw_loadstore` and `buffer_tmatmul_desc[b]`
- Per-bank `tmatmul_dma[b]`: pblock'd to SLR<b>

## Interconnects in `axi_ternip_batched.sv` (top of kernel)

| Instance | Source | Sink | Nominal SLR hops | NumStages | Rec. Implementation |
|---|---|---|---:|---:|---|
| `buffer_m_axi_tmatmul_r[b]` | XRT DDR[b] MMU | `axi_dma_rd[b]` (SLR<b>) | 0 nominal (both pblock'd to SLR<b>) but XRT DDR MMU historically ≥ 2 hops in practice | 8 | `axis_pipeline_register` (PG373 Fully-Registered — R burst channel) |
| `buffer_tmatmul_desc[0]` | core (SLR0+1) | `dma_r_tmatmul[0]` (SLR0) | 0 | 6 → could drop to 3 | default (`axis_pipeline_fifo`) |
| `buffer_tmatmul_desc[1]` | core (SLR0+1) | `dma_r_tmatmul[1]` (SLR1) | 0 | 6 → could drop to 3 | default |
| `buffer_tmatmul_desc[2]` | core (SLR0+1) | `dma_r_tmatmul[2]` (SLR2) | 1 | 6 (fine as-is) | default |
| `buffer_tmatmul_desc[3]` | core (SLR0+1) | `dma_r_tmatmul[3]` (SLR3) | 2 | 6 → could raise to 8 | Light-Weight (AR/AW channel) or default |
| `buffer_loadstore_ar` | `dma_rw_loadstore` (SLR0+1) | XRT | 0-1 | 6 → could drop to 3-4 | Light-Weight or default |
| `buffer_loadstore_r` | XRT | `dma_rw_loadstore` (SLR0+1) | 0-1 | 6 → could drop to 4 | `axis_pipeline_register` (R burst) |
| `buffer_loadstore_aw` | dma_rw | XRT | 0-1 | 6 → 3-4 | Light-Weight or default |
| `buffer_loadstore_w` | dma_rw | XRT | 0-1 | 6 → 4 | `axis_pipeline_register` (W burst) |
| `buffer_loadstore_b` | XRT | dma_rw | 0-1 | 6 → 3 | Light-Weight or default |

## Interconnects in `ternip_buffered.sv` (× BatchSize per kernel)

Each `ternip_buffered` instance lives inside one `core[i]` of
`ternip_batched`. At BS=20 MaxCores, cores fan across all 4 SLRs, so
each interconnect's SLR span is roughly (core's SLR) → (sink's SLR).
Assume worst-case (core in SLR0 or SLR3) for stage sizing.

| Instance | Source | Sink | Nominal SLR hops | NumStages | Rec. Implementation |
|---|---|---|---:|---:|---|
| `buffer_instruction` | XRT instruction port | core[i]'s ternip_core | 0-2 (depends on core's SLR) | 6 → 4-6 | default (control channel, low-BW) |
| `buffer_loadstore_ddr_stream` | ternip_core (core[i]'s SLR) | dma_rw_loadstore (SLR0+1) | 0-2 | 6 → 3-4 | default |
| `buffer_loadstore_ddr_r` | dma_rw_loadstore | ternip_core | 0-2 | 6 → 4 | `axis_pipeline_register` (R data) |
| `buffer_loadstore_ddr_w` | ternip_core | dma_rw_loadstore | 0-2 | 6 → 4 | `axis_pipeline_register` (W data) |
| `buffer_loadstore_ddr_debug` | ternip_core | ext | N/A | 6 → 1 (debug, unused) | default |
| `buffer_tmatmul_ddr_stream[b]` | ternip_core (core[i]'s SLR) | tmatmul_dma[b] (SLR<b>) | 0-3 | 6 uniform | default |
| `buffer_tmatmul_ddr_r[b]` | tmatmul_dma[b] (SLR<b>) | ternip_core (core[i]'s SLR) | 0-3 | 6 uniform | default (or `axis_pipeline_register` for wider R data) |

## Priorities (post build_58)

1. **build_59**: extend `Implementation("axis_pipeline_register")` to
   `buffer_loadstore_r`. Same lever as build_58, applied to the other
   XRT R-channel burst. Low blast radius.
2. **build_60**: tune `LoadstoreDdrDebugNumStages=1` on
   `ternip_buffered` (debug port, no functional need for pipelining).
   Frees ~64 × BatchSize FFs per iteration.
3. **build_61**: per-bank tmatmul stage counts:
   `TmatmulDdrRNumStages='{4, 5, 6, 8}` (or similar) — depth
   proportional to core-to-bank SLR distance under a worst-case
   placement.
4. **build_62+**: Vivado AXI Register Slice IP integration (see
   QUESTIONS.md for the create_ip TCL work).

The value of the refactor is that these all become single-parameter
edits at the parent scope, no `ternip_pipelined_interconnect` module
changes needed.
