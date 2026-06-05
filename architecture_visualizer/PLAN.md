# PLAN.md — architecture_visualizer

Refined plan from the design discussion. `CLAUDE.md` holds the original spec;
this document captures decisions made on top of it.

## Goal

Interactive web tool to visualize the Ternip kernel topology under three
architectural variants, with sliders that affect node sizes (cell counts) and
edge thicknesses (bus widths), and on-demand tokens/sec estimation.

## Tech stack

- **Dash + dash-cytoscape** (Python-only, browser-rendered)
- `pip install dash dash-cytoscape`
- Launch: `python -m architecture_visualizer.app` → `localhost:8050`
- **Remote-server access**: README must document SSH port forwarding —
  `ssh -L 8050:localhost:8050 <user>@<hostname>` from the user's laptop,
  then open `http://localhost:8050` in the laptop browser. The Dash
  process binds to `127.0.0.1:8050` (default) so it's not exposed to
  the public network.

## Architecture variants (dropdown)

All three are supported; topology differs significantly between them.

| Variant | Meaning | Topology sketch |
|---|---|---|
| `NumSeparateAxiInstances` | N parallel kernel instances, each with 1 tmatmul + 1 DDR bank | N independent `axi_ternip_batched_<i>` cells, no shared logic |
| `NumDdrBanksPerTmatmul` | 1 kernel, 1 tmatmul module, N DDR banks feeding it | 1 ternip_core, 1 tmatmul, N `m_axi_tmatmul_<b>` AXI ports |
| `NumTmatmulBanksPerCore` | 1 kernel, N tmatmul units (column-slice), each reads from 1 DDR bank | 1 ternip_core, N `tmatmul_units[u]`, shared broadcast R-channel |

The slider `NumDdrBanksUsed` means **N** in all three variants.

## Sliders (4)

| Slider | Range | Default | Notes |
|---|---|---|---|
| `TmatmulParallelism` | 16..512 (powers of 2) | 128 | Halves MOA tree width when reduced |
| `VectorParallelism` | 1..16 (powers of 2) | 4 | Affects vector_chunk_t width |
| `BatchSize` | 1..20 | 1 | Replicates ternip_core BS times |
| `NumDdrBanksUsed` | 1..4 (factors of D) | 4 | Topology-altering; for column-slice clamps to D-divisors |

**`NumVectorRegisters` is a constant** (4), not exposed as a slider.

**Initial slider state** is read from `config/xcu250_D=1024_MaxCores.svh` on
launch — first view matches "current repo state".

## Nodes

Per architecture variant. `[b]` indexes DRAM banks (0..N-1); `[c]` indexes cores
(0..BS-1); `[u]` indexes tmatmul units inside a core (0..N-1 for column-slice).

### Common to all variants
- `DRAM[b]` — fixed position (top row, pinned)
- `instruction_decode` — instruction DMA + fifos + broadcast root
- `axi_dma_instr` — XRT-side instruction DMA fetcher
- Per-core compound: `core[c]` containing:
  - `RMS`
  - `loadstore`
  - `rowwise_op`
  - `vector_registers` (separate node — heavy mux-shared port)

### Variant-specific
| Variant | Adds |
|---|---|
| `NumSeparateAxiInstances` | `tmatmul[c]` (one per core), `tmatmul_dma[c]`, `MOA[c]`, `importvector[c]`, `exportvector[c]`, `gbfifo_tmatmul[c]` |
| `NumDdrBanksPerTmatmul` | `tmatmul_dma[b]` per DDR bank, single `tmatmul` per core that consumes all N streams, single `MOA`/`importvector`/`exportvector` |
| `NumTmatmulBanksPerCore` | `tmatmul_dma[b]`, `tmatmul_buffers[u]`, `tmatmul_units[u]` (with embedded `MOA[u]`, `importvector[u]`, `exportvector[u]`) |

## Edges

Single Python module `lib/topology.py` holds all bus-width formulas, keyed
by (src_type, dst_type). Examples:

| Edge | Bus width formula |
|---|---|
| `tmatmul_dma[b] → tmatmul_unit[u]` (column-slice) | `TmatmulParallelism × 2` (ternary bits) |
| `vector_registers → tmatmul_unit[u]` | `VectorParallelism × FixedPointPrecision × NumChunksPerVector` |
| `instruction_decode → core[c]` | `InstructionWidth` (≈ 128) |
| `loadstore → DRAM[0]` | `DdrDataWidth` (512) |
| `core[c] ↔ DRAM[b]` (BS-batched) | `vector_chunk_t × BatchSize` |

Hover on edge → tooltip with formula breakdown.

## UI components

- **Top bar**: architecture-variant dropdown
- **Left side**: 4 sliders + "Compute tokens/sec" button + tokens/sec readout
- **Center**: Cytoscape graph (DRAM banks pinned top, rest force-directed `cose`)
- **Right sidebar**: hierarchical node list with per-node highlight checkboxes
  - Hierarchy: `DRAM banks → bank[0..N-1]`, `Cores → core[c] → {RMS, loadstore, rowwise, vector_registers, tmatmul_units[u]}`, `Shared infrastructure → instruction_decode, axi_dma_instr`
- **Bottom strip**: total wire length stat (`Σ bus_bits × edge_pixel_length`),
  live on drag

## Visual encoding

| Visual | Encoding |
|---|---|
| Node radius | `√(cell_count)` (square-root scaling) |
| Node fill (unselected) | by node-type (DRAM=blue, tmatmul_dma=orange, ternip_core=green, etc.) |
| Node fill (selected) | override color from user checkbox |
| Edge thickness | linear in `bus_bits` (cytoscape `'width': 'data(bus_bits)'`) |
| Edge hover | tooltip: bus_bits, formula, current pixel length |
| Node hover | tooltip: cell_count, formula breakdown |

## On-demand actions

- **"Compute tokens/sec" button**: takes current slider values, builds a
  config dict via `Config.from_dict(...)`, calls re-implemented throughput
  math in `lib/throughput.py`, displays the result.
  - **No subprocess.** Re-implementation of `report_instruction_timing.py`
    logic; sanity-checked by unit test against the original script.

## Out of scope / explicit non-features

- No animation between slider changes (snap to new values)
- No persistent state / URL params
- No export (PNG / JSON)
- No SLR-crossing overlay (mentioned but off by default; could be added later)
- No multi-window / side-by-side variant comparison (use the dropdown to flip)

## File structure

```
architecture_visualizer/
├── CLAUDE.md            # Original user spec
├── PLAN.md              # This file
├── README.md            # How to launch (created at build time)
├── requirements.txt     # dash, dash-cytoscape, pytest
├── app.py               # Dash app: layout + callbacks
├── lib/
│   ├── __init__.py
│   ├── topology.py      # (arch_variant, params) → nodes + edges with bus_bits
│   ├── cell_estimates.py  # cell-count formula per node type
│   ├── throughput.py    # ported report_instruction_timing.py math
│   └── style.py         # cytoscape stylesheet generator
└── tests/
    ├── test_topology.py     # edge cases (D % N != 0, BS=1, etc.)
    └── test_throughput.py   # parity against the original script
```

## Upstream change required

`ternary_matmul/sw_utils/lib/config.py`:

```python
class Config:
    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        """Build a Config with field values pre-populated from a dict.
        Used by the architecture visualizer to override individual
        parameters without reading a .svh file."""
        ...
```

Backward compatible — existing `Config(configpkg_files=...)` callers unchanged.

## Implementation phases

1. **Phase 1 — Skeleton + topology** (1 session):
   - Dash app shell with sliders + dropdown + empty Cytoscape
   - `lib/topology.py` for one variant (NumTmatmulBanksPerCore)
   - `lib/cell_estimates.py` with hand-tuned formulas
   - Slider → Cytoscape update callback
2. **Phase 2 — Other variants + sidebar** (1 session):
   - NumDdrBanksPerTmatmul + NumSeparateAxiInstances topology
   - Hierarchical sidebar with highlight checkboxes
   - Hover tooltips
3. **Phase 3 — Throughput + polish** (1 session):
   - `Config.from_dict` upstream patch
   - `lib/throughput.py` re-implementation
   - "Compute tokens/sec" button + readout
   - Wire-length live-on-drag statistic
   - Unit tests
