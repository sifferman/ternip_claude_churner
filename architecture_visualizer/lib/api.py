"""Shared API contract for architecture_visualizer modules.

Every module in this package adheres to the TypedDicts and function
signatures defined here. **Do not modify this file without coordinating
across all modules** — it is the single source of truth that parallel
agents agree on.

Architecture variants:
    NumSeparateAxiInstances:    N parallel kernel cells, 1 tmatmul each
    NumDdrBanksPerTmatmul:      1 kernel, 1 tmatmul, N DDR banks feeding it
    NumTmatmulBanksPerCore:     1 kernel, N tmatmul units (column-slice)
"""
from __future__ import annotations

from typing import Literal, Optional, TypedDict


# ---------------------------------------------------------------------------
# Architecture variants
# ---------------------------------------------------------------------------

ArchVariant = Literal[
    "NumSeparateAxiInstances",
    "NumDdrBanksPerTmatmul",
    "NumTmatmulBanksPerCore",
]

ARCH_VARIANTS: tuple[ArchVariant, ...] = (
    "NumSeparateAxiInstances",
    "NumDdrBanksPerTmatmul",
    "NumTmatmulBanksPerCore",
)


# ---------------------------------------------------------------------------
# Node + Edge data shapes
# ---------------------------------------------------------------------------

class Node(TypedDict):
    """One graph node — a hardware block."""
    id: str                  # unique e.g. "tmatmul_dma_b0_c0"
    label: str               # display e.g. "tmatmul_dma[0]"
    type: str                # for coloring: "DRAM", "tmatmul_dma",
                             # "MOA", "importvector", "exportvector",
                             # "tmatmul_unit", "ternip_core",
                             # "RMS", "loadstore", "rowwise_op",
                             # "vector_registers", "instruction_decode",
                             # "axi_dma_instr"
    bank: Optional[int]      # 0..N-1 if bank-affined, else None
    core: Optional[int]      # 0..BS-1 if core-affined, else None
    cell_count: int          # estimated cell count (LUT-equivalent)
    slr: Optional[int]       # 0..3 if SLR-fixed (DRAM banks), else None


class Edge(TypedDict):
    """One graph edge — an inter-module bus."""
    source: str              # Node.id
    target: str              # Node.id
    bus_bits: int            # wire count
    formula: str             # for hover tooltip, e.g. "TmatmulParallelism * 2"


# ---------------------------------------------------------------------------
# Parameter dict — what topology/cell_estimates consume
# ---------------------------------------------------------------------------

class ParamsDict(TypedDict, total=False):
    """All numeric parameters needed by topology + cell_estimates.

    Slider-driven (user-controllable):
        TmatmulParallelism
        VectorParallelism
        BatchSize
        NumDdrBanksUsed         # meaning depends on variant

    Constant defaults (read from MaxCores config on launch):
        D                       # vector dimension (1024)
        FixedPointPrecision     # FxP width (16)
        NumVectorRegisters      # (4)
        DdrDataWidth            # (512)
        InstructionWidth        # (128)
        TmatmulUnitIdWidth      # log2(N) when N>=2
        DramNumBanks            # (4 for AU250)
    """
    # Slider-driven
    TmatmulParallelism: int
    VectorParallelism: int
    BatchSize: int
    NumDdrBanksUsed: int

    # Constants
    D: int
    FixedPointPrecision: int
    NumVectorRegisters: int
    DdrDataWidth: int
    InstructionWidth: int
    DramNumBanks: int


# ---------------------------------------------------------------------------
# Topology API (lib/topology.py)
# ---------------------------------------------------------------------------

def build_topology(
    variant: ArchVariant,
    params: ParamsDict,
) -> tuple[list[Node], list[Edge]]:
    """Compute the graph for a given architecture variant + parameters.

    The returned lists are immutable from the GUI side — Dash's callback
    rebuilds them on every relevant input change.

    Raises ValueError when (variant, params) is inconsistent, e.g.
    NumTmatmulBanksPerCore with D % NumDdrBanksUsed != 0.
    """
    raise NotImplementedError  # implemented in lib/topology.py


# ---------------------------------------------------------------------------
# Cell-count formula API (lib/cell_estimates.py)
# ---------------------------------------------------------------------------

class CellEstimate(TypedDict):
    """Formula-driven estimate for one node."""
    count: int               # the computed cell count
    formula: str             # human-readable, e.g. "TP * FxP * log2(TP)"
    breakdown: str           # populated values, e.g. "256 * 16 * 8 = 32768"


def estimate_cells(
    node_type: str,
    params: ParamsDict,
    bs_factor: int = 1,      # multiplied in for whole-core nodes scaled by BS
) -> CellEstimate:
    """Per-node-type cell-count estimate."""
    raise NotImplementedError  # implemented in lib/cell_estimates.py


# ---------------------------------------------------------------------------
# Throughput API (lib/throughput.py)
# ---------------------------------------------------------------------------

class ThroughputResult(TypedDict):
    singlecore: float
    multicore: float
    clk_freq_mhz: float


def compute_tokens_per_sec(
    config: dict,            # superset of ParamsDict (full config keys)
    model: str = "MMfreeLM-370M",
) -> ThroughputResult:
    """Re-implementation of report_instruction_timing.py logic.

    Pure Python; does not subprocess the original script.
    """
    raise NotImplementedError  # implemented in lib/throughput.py


# ---------------------------------------------------------------------------
# Stylesheet API (lib/style.py)
# ---------------------------------------------------------------------------

def generate_stylesheet(
    nodes_by_type_colors: dict[str, str],   # node_type -> hex color
    highlight_set: set[str],                # node ids to override
) -> list[dict]:
    """Return cytoscape stylesheet list with:
       - node radius = sqrt(cell_count) scaled
       - node fill from nodes_by_type_colors[node['type']]
       - highlighted nodes get an override color
       - edge width = linear in bus_bits
    """
    raise NotImplementedError  # implemented in lib/style.py
