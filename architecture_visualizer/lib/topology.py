"""Topology builder — converts (architecture variant, parameters) into
a list of Node + Edge dicts. See lib/api.py for the contract.

OWNED BY: Phase 1 Agent B (Topology + Cells Builder).

Three variants:

  NumSeparateAxiInstances
      N parallel kernel cells, each its own ternip_core + tmatmul +
      DDR bank + instruction-decode + AXI instruction DMA. Nothing
      shared between cells.

  NumDdrBanksPerTmatmul
      1 ternip_core, 1 tmatmul module fed by N DDR banks. A single
      MOA / importvector / exportvector consume all N data streams.

  NumTmatmulBanksPerCore
      1 ternip_core, N column-slice tmatmul UNITS, each reading from
      one DDR bank. Each tmatmul_dma broadcasts its R-channel to all
      tmatmul_units. If BatchSize > 1, the ternip_core (and its N
      tmatmul_units) is replicated BatchSize times; DRAM banks and
      tmatmul_dma stay singular per bank (shared across cores).
"""
from __future__ import annotations

from lib.api import ArchVariant, Edge, Node, ParamsDict
from lib.cell_estimates import estimate_cells


# ---------------------------------------------------------------------------
# Default parameters
# ---------------------------------------------------------------------------

_DEFAULTS: dict = {
    "TmatmulParallelism": 128,
    "VectorParallelism": 4,
    "BatchSize": 1,
    "NumDdrBanksUsed": 4,
    "D": 1024,
    "FixedPointPrecision": 16,
    "NumVectorRegisters": 4,
    "DdrDataWidth": 512,
    "InstructionWidth": 128,
    "DramNumBanks": 4,
}


def _merge_defaults(params: ParamsDict) -> dict:
    merged = dict(_DEFAULTS)
    merged.update(params or {})
    return merged


# ---------------------------------------------------------------------------
# Node / edge factories
# ---------------------------------------------------------------------------

def _make_node(
    *,
    node_id: str,
    label: str,
    node_type: str,
    params: ParamsDict,
    bank: int | None = None,
    core: int | None = None,
    slr: int | None = None,
    bs_factor: int = 1,
) -> Node:
    cell = estimate_cells(node_type, params, bs_factor=bs_factor)
    return {
        "id": node_id,
        "label": label,
        "type": node_type,
        "bank": bank,
        "core": core,
        "cell_count": cell["count"],
        "slr": slr,
    }


def _make_edge(source: str, target: str, bus_bits: int, formula: str) -> Edge:
    return {
        "source": source,
        "target": target,
        "bus_bits": int(bus_bits),
        "formula": formula,
    }


# ---------------------------------------------------------------------------
# Shared infrastructure (DRAM banks, instruction decode, AXI instruction DMA)
# ---------------------------------------------------------------------------

def _add_dram_banks(
    nodes: list[Node],
    params: ParamsDict,
    n_banks: int,
) -> None:
    """DRAM banks are pinned: bank b -> SLR b on the AU250."""
    for b in range(n_banks):
        nodes.append(_make_node(
            node_id=f"dram_b{b}",
            label=f"DRAM[{b}]",
            node_type="DRAM",
            params=params,
            bank=b,
            slr=b,
        ))


# ---------------------------------------------------------------------------
# Variant: NumSeparateAxiInstances
# ---------------------------------------------------------------------------

def _build_NumSeparateAxiInstances(
    params: ParamsDict,
) -> tuple[list[Node], list[Edge]]:
    n = int(params["NumDdrBanksUsed"])
    if n < 1:
        raise ValueError("NumDdrBanksUsed must be >= 1")
    bs = max(int(params.get("BatchSize", 1)), 1)
    tp = int(params["TmatmulParallelism"])
    vp = int(params["VectorParallelism"])
    fxp = int(params["FixedPointPrecision"])
    iw = int(params["InstructionWidth"])
    dw = int(params["DdrDataWidth"])

    nodes: list[Node] = []
    edges: list[Edge] = []

    _add_dram_banks(nodes, params, n)

    for i in range(n):
        # Per-AXI-instance shared blocks (1 of each per AXI cell):
        # tmatmul_dma, axi_dma_instr, instruction_decode, the DRAM bank.
        idc_id = f"instruction_decode_i{i}"
        adi_id = f"axi_dma_instr_i{i}"
        tmd_id = f"tmatmul_dma_i{i}"
        dram_id = f"dram_b{i}"

        nodes.append(_make_node(
            node_id=adi_id, label=f"axi_dma_instr[{i}]",
            node_type="axi_dma_instr", params=params,
        ))
        nodes.append(_make_node(
            node_id=idc_id, label=f"instruction_decode[{i}]",
            node_type="instruction_decode", params=params,
        ))
        nodes.append(_make_node(
            node_id=tmd_id, label=f"tmatmul_dma[{i}]",
            node_type="tmatmul_dma", params=params, bank=i,
        ))

        # Instance-level edges
        edges.append(_make_edge(
            adi_id, idc_id, iw,
            "InstructionWidth (AXI DMA -> decoder)",
        ))
        edges.append(_make_edge(
            dram_id, tmd_id, dw,
            "DdrDataWidth (R-channel)",
        ))

        # Per-core hardware INSIDE this AXI instance — BatchSize copies.
        # Each core has its own MOA/IV/EV/RMS/loadstore/rowwise/vector_regs/
        # tmatmul_unit. The tmatmul_dma[i] broadcasts its ternary stream
        # to every core's MOA in this instance.
        for c in range(bs):
            ternip_id = f"ternip_core_i{i}_c{c}"
            moa_id = f"moa_i{i}_c{c}"
            iv_id = f"importvector_i{i}_c{c}"
            ev_id = f"exportvector_i{i}_c{c}"
            rms_id = f"rms_i{i}_c{c}"
            ls_id = f"loadstore_i{i}_c{c}"
            rw_id = f"rowwise_op_i{i}_c{c}"
            vr_id = f"vector_registers_i{i}_c{c}"

            nodes.append(_make_node(
                node_id=ternip_id, label=f"ternip_core[{i},{c}]",
                node_type="ternip_core", params=params, bank=i, core=c,
            ))
            nodes.append(_make_node(
                node_id=moa_id, label=f"MOA[{i},{c}]",
                node_type="MOA", params=params, bank=i, core=c,
            ))
            nodes.append(_make_node(
                node_id=iv_id, label=f"importvector[{i},{c}]",
                node_type="importvector", params=params, bank=i, core=c,
            ))
            nodes.append(_make_node(
                node_id=ev_id, label=f"exportvector[{i},{c}]",
                node_type="exportvector", params=params, bank=i, core=c,
            ))
            nodes.append(_make_node(
                node_id=rms_id, label=f"RMS[{i},{c}]",
                node_type="RMS", params=params, bank=i, core=c,
            ))
            nodes.append(_make_node(
                node_id=ls_id, label=f"loadstore[{i},{c}]",
                node_type="loadstore", params=params, bank=i, core=c,
            ))
            nodes.append(_make_node(
                node_id=rw_id, label=f"rowwise_op[{i},{c}]",
                node_type="rowwise_op", params=params, bank=i, core=c,
            ))
            nodes.append(_make_node(
                node_id=vr_id, label=f"vector_registers[{i},{c}]",
                node_type="vector_registers", params=params, bank=i, core=c,
            ))

            # instruction broadcast to this core
            edges.append(_make_edge(
                idc_id, ternip_id, iw,
                "InstructionWidth (decoded -> core)",
            ))

            # tmatmul_dma broadcast to this core's MOA
            edges.append(_make_edge(
                tmd_id, moa_id, tp * 2,
                "TmatmulParallelism * 2 (ternary stream, broadcast)",
            ))
            edges.append(_make_edge(
                moa_id, ternip_id, fxp * tp,
                "FixedPointPrecision * TmatmulParallelism (MOA result)",
            ))

            # IV/EV wide buses to/from MOA
            edges.append(_make_edge(
                iv_id, moa_id, tp * fxp,
                "TmatmulParallelism * FixedPointPrecision (wide activation)",
            ))
            edges.append(_make_edge(
                moa_id, ev_id, tp * fxp,
                "TmatmulParallelism * FixedPointPrecision (wide accumulator out)",
            ))

            # Loadstore connects to this instance's DRAM bank
            edges.append(_make_edge(
                ls_id, dram_id, dw,
                "DdrDataWidth (loadstore W-channel)",
            ))
            edges.append(_make_edge(
                dram_id, ls_id, dw,
                "DdrDataWidth (loadstore R-channel)",
            ))

            # Core <-> shared per-core functional units
            edges.append(_make_edge(
                ternip_id, rms_id, vp * fxp,
                "VectorParallelism * FixedPointPrecision",
            ))
            edges.append(_make_edge(
                ternip_id, ls_id, vp * fxp,
                "VectorParallelism * FixedPointPrecision",
            ))
            edges.append(_make_edge(
                ternip_id, rw_id, vp * fxp,
                "VectorParallelism * FixedPointPrecision",
            ))
            edges.append(_make_edge(
                ternip_id, vr_id, vp * fxp,
                "VectorParallelism * FixedPointPrecision",
            ))
            edges.append(_make_edge(
                vr_id, iv_id, vp * fxp,
                "VectorParallelism * FixedPointPrecision",
            ))
            edges.append(_make_edge(
                ev_id, vr_id, vp * fxp,
                "VectorParallelism * FixedPointPrecision",
            ))

    # XRT shell — single platform-side node; every per-instance decoder
    # receives its instruction stream from here.
    nodes.append(_make_node(
        node_id="xrt_shell", label="XRT shell",
        node_type="xrt_shell", params=params,
    ))
    for i in range(n):
        edges.append(_make_edge(
            "xrt_shell", f"instruction_decode_i{i}", iw,
            "InstrFetchWidth (instructions from host via XRT)",
        ))

    return nodes, edges


# ---------------------------------------------------------------------------
# Variant: NumDdrBanksPerTmatmul
# ---------------------------------------------------------------------------

def _build_NumDdrBanksPerTmatmul(
    params: ParamsDict,
) -> tuple[list[Node], list[Edge]]:
    n = int(params["NumDdrBanksUsed"])
    if n < 1:
        raise ValueError("NumDdrBanksUsed must be >= 1")
    bs = max(int(params.get("BatchSize", 1)), 1)
    tp = int(params["TmatmulParallelism"])
    vp = int(params["VectorParallelism"])
    fxp = int(params["FixedPointPrecision"])
    iw = int(params["InstructionWidth"])
    dw = int(params["DdrDataWidth"])

    nodes: list[Node] = []
    edges: list[Edge] = []

    _add_dram_banks(nodes, params, n)

    # Shared (single-instance) infrastructure
    nodes.append(_make_node(
        node_id="axi_dma_instr", label="axi_dma_instr",
        node_type="axi_dma_instr", params=params,
    ))
    nodes.append(_make_node(
        node_id="instruction_decode", label="instruction_decode",
        node_type="instruction_decode", params=params,
    ))

    # Per-bank tmatmul_dma (shared across all BS cores; broadcast)
    for b in range(n):
        tmd_id = f"tmatmul_dma_b{b}"
        nodes.append(_make_node(
            node_id=tmd_id, label=f"tmatmul_dma[{b}]",
            node_type="tmatmul_dma", params=params, bank=b,
        ))
        edges.append(_make_edge(
            f"dram_b{b}", tmd_id, dw,
            "DdrDataWidth (R-channel)",
        ))

    # Instruction-fetcher edges
    edges.append(_make_edge(
        "axi_dma_instr", "instruction_decode", iw,
        "InstructionWidth (AXI DMA -> decoder)",
    ))

    # Per-core hardware — BatchSize copies. Each core has its OWN
    # MOA / IV / EV / RMS / loadstore / rowwise / vector_registers / tmatmul_unit.
    # All N tmatmul_dma streams broadcast to every core's MOA.
    for c in range(bs):
        ternip_id = f"ternip_core_c{c}"
        moa_id = f"moa_c{c}"
        iv_id = f"importvector_c{c}"
        ev_id = f"exportvector_c{c}"
        rms_id = f"rms_c{c}"
        ls_id = f"loadstore_c{c}"
        rw_id = f"rowwise_op_c{c}"
        vr_id = f"vector_registers_c{c}"

        nodes.append(_make_node(
            node_id=ternip_id, label=f"ternip_core[{c}]",
            node_type="ternip_core", params=params, core=c,
        ))
        nodes.append(_make_node(
            node_id=moa_id, label=f"MOA[{c}]",
            node_type="MOA", params=params, core=c,
        ))
        nodes.append(_make_node(
            node_id=iv_id, label=f"importvector[{c}]",
            node_type="importvector", params=params, core=c,
        ))
        nodes.append(_make_node(
            node_id=ev_id, label=f"exportvector[{c}]",
            node_type="exportvector", params=params, core=c,
        ))
        nodes.append(_make_node(
            node_id=rms_id, label=f"RMS[{c}]",
            node_type="RMS", params=params, core=c,
        ))
        nodes.append(_make_node(
            node_id=ls_id, label=f"loadstore[{c}]",
            node_type="loadstore", params=params, core=c,
        ))
        nodes.append(_make_node(
            node_id=rw_id, label=f"rowwise_op[{c}]",
            node_type="rowwise_op", params=params, core=c,
        ))
        nodes.append(_make_node(
            node_id=vr_id, label=f"vector_registers[{c}]",
            node_type="vector_registers", params=params, core=c,
        ))

        # All N tmatmul_dma streams broadcast to this core's MOA
        for b in range(n):
            edges.append(_make_edge(
                f"tmatmul_dma_b{b}", moa_id, tp * 2,
                "TmatmulParallelism * 2 (ternary stream, broadcast)",
            ))

        # instruction broadcast to this core
        edges.append(_make_edge(
            "instruction_decode", ternip_id, iw,
            "InstructionWidth (decoded -> core)",
        ))

        # MOA <-> core
        edges.append(_make_edge(
            moa_id, ternip_id, fxp * tp,
            "FixedPointPrecision * TmatmulParallelism (MOA result)",
        ))
        edges.append(_make_edge(
            iv_id, moa_id, tp * fxp,
            "TmatmulParallelism * FixedPointPrecision (wide activation)",
        ))
        edges.append(_make_edge(
            moa_id, ev_id, tp * fxp,
            "TmatmulParallelism * FixedPointPrecision (wide accumulator out)",
        ))

        # Loadstore connects to DRAM[0] (convention — single m_axi_loadstore)
        edges.append(_make_edge(
            ls_id, "dram_b0", dw,
            "DdrDataWidth (loadstore W-channel)",
        ))
        edges.append(_make_edge(
            "dram_b0", ls_id, dw,
            "DdrDataWidth (loadstore R-channel)",
        ))

        # Core <-> shared per-core functional units
        edges.append(_make_edge(
            ternip_id, rms_id, vp * fxp,
            "VectorParallelism * FixedPointPrecision",
        ))
        edges.append(_make_edge(
            ternip_id, ls_id, vp * fxp,
            "VectorParallelism * FixedPointPrecision",
        ))
        edges.append(_make_edge(
            ternip_id, rw_id, vp * fxp,
            "VectorParallelism * FixedPointPrecision",
        ))
        edges.append(_make_edge(
            ternip_id, vr_id, vp * fxp,
            "VectorParallelism * FixedPointPrecision",
        ))
        edges.append(_make_edge(
            vr_id, iv_id, vp * fxp,
            "VectorParallelism * FixedPointPrecision",
        ))
        edges.append(_make_edge(
            ev_id, vr_id, vp * fxp,
            "VectorParallelism * FixedPointPrecision",
        ))

    # XRT shell — single platform-side node that delivers instructions.
    nodes.append(_make_node(
        node_id="xrt_shell", label="XRT shell",
        node_type="xrt_shell", params=params,
    ))
    edges.append(_make_edge(
        "xrt_shell", "instruction_decode", iw,
        "InstrFetchWidth (instructions from host via XRT)",
    ))

    return nodes, edges


# ---------------------------------------------------------------------------
# Variant: NumTmatmulBanksPerCore (column-slice)
# ---------------------------------------------------------------------------

def _build_NumTmatmulBanksPerCore(
    params: ParamsDict,
) -> tuple[list[Node], list[Edge]]:
    n = int(params["NumDdrBanksUsed"])
    if n < 1:
        raise ValueError("NumDdrBanksUsed must be >= 1")
    d = int(params["D"])
    if d % n != 0:
        raise ValueError(
            f"D ({d}) must be divisible by NumDdrBanksUsed ({n}) for "
            f"NumTmatmulBanksPerCore variant"
        )
    bs = max(int(params.get("BatchSize", 1)), 1)
    tp = int(params["TmatmulParallelism"])
    vp = int(params["VectorParallelism"])
    fxp = int(params["FixedPointPrecision"])
    iw = int(params["InstructionWidth"])
    dw = int(params["DdrDataWidth"])

    nodes: list[Node] = []
    edges: list[Edge] = []

    _add_dram_banks(nodes, params, n)

    # Shared (across all BS cores)
    nodes.append(_make_node(
        node_id="axi_dma_instr", label="axi_dma_instr",
        node_type="axi_dma_instr", params=params,
    ))
    nodes.append(_make_node(
        node_id="instruction_decode", label="instruction_decode",
        node_type="instruction_decode", params=params,
    ))

    # tmatmul_dma stays singular per bank — its R-channel is broadcast to
    # all cores' tmatmul_units.
    for b in range(n):
        tmd_id = f"tmatmul_dma_b{b}"
        nodes.append(_make_node(
            node_id=tmd_id, label=f"tmatmul_dma[{b}]",
            node_type="tmatmul_dma", params=params, bank=b,
        ))
        edges.append(_make_edge(
            f"dram_b{b}", tmd_id, dw,
            "DdrDataWidth (R-channel)",
        ))

    # Per-core replication: BS cores, each with N tmatmul_units etc.
    for c in range(bs):
        ternip_id = f"ternip_core_c{c}"
        rms_id = f"rms_c{c}"
        ls_id = f"loadstore_c{c}"
        rw_id = f"rowwise_op_c{c}"
        vr_id = f"vector_registers_c{c}"

        nodes.append(_make_node(
            node_id=ternip_id, label=f"ternip_core[{c}]",
            node_type="ternip_core", params=params, core=c,
        ))
        nodes.append(_make_node(
            node_id=rms_id, label=f"RMS[{c}]",
            node_type="RMS", params=params, core=c,
        ))
        nodes.append(_make_node(
            node_id=ls_id, label=f"loadstore[{c}]",
            node_type="loadstore", params=params, core=c,
        ))
        nodes.append(_make_node(
            node_id=rw_id, label=f"rowwise_op[{c}]",
            node_type="rowwise_op", params=params, core=c,
        ))
        nodes.append(_make_node(
            node_id=vr_id, label=f"vector_registers[{c}]",
            node_type="vector_registers", params=params, core=c,
        ))

        # Loadstore is per-core, talks to DRAM[0] by convention.
        edges.append(_make_edge(
            ls_id, "dram_b0", dw,
            "DdrDataWidth (loadstore W-channel)",
        ))
        edges.append(_make_edge(
            "dram_b0", ls_id, dw,
            "DdrDataWidth (loadstore R-channel)",
        ))

        # instruction_decode -> this core
        edges.append(_make_edge(
            "instruction_decode", ternip_id, iw,
            "InstructionWidth (decoded -> core)",
        ))

        # Shared FUs (within the core)
        edges.append(_make_edge(
            ternip_id, rms_id, vp * fxp,
            "VectorParallelism * FixedPointPrecision",
        ))
        edges.append(_make_edge(
            ternip_id, ls_id, vp * fxp,
            "VectorParallelism * FixedPointPrecision",
        ))
        edges.append(_make_edge(
            ternip_id, rw_id, vp * fxp,
            "VectorParallelism * FixedPointPrecision",
        ))
        edges.append(_make_edge(
            ternip_id, vr_id, vp * fxp,
            "VectorParallelism * FixedPointPrecision",
        ))

        # N tmatmul_units per core, each containing its own MOA/IV/EV.
        for u in range(n):
            unit_id = f"tmatmul_unit_c{c}_u{u}"
            moa_id = f"moa_c{c}_u{u}"
            iv_id = f"importvector_c{c}_u{u}"
            ev_id = f"exportvector_c{c}_u{u}"

            nodes.append(_make_node(
                node_id=unit_id, label=f"tmatmul_unit[{u}] (core {c})",
                node_type="tmatmul_unit", params=params, bank=u, core=c,
            ))
            nodes.append(_make_node(
                node_id=moa_id, label=f"MOA[{u}] (core {c})",
                node_type="MOA", params=params, bank=u, core=c,
            ))
            nodes.append(_make_node(
                node_id=iv_id, label=f"importvector[{u}] (core {c})",
                node_type="importvector", params=params, bank=u, core=c,
            ))
            nodes.append(_make_node(
                node_id=ev_id, label=f"exportvector[{u}] (core {c})",
                node_type="exportvector", params=params, bank=u, core=c,
            ))

            # Broadcast: each tmatmul_dma[b] feeds every tmatmul_unit[u]
            # across all cores. Per user spec, the bank<->unit mapping
            # itself is full broadcast.
            for b in range(n):
                edges.append(_make_edge(
                    f"tmatmul_dma_b{b}", unit_id, tp * 2,
                    "TmatmulParallelism * 2 (ternary stream, broadcast)",
                ))

            # Sub-edges within the tmatmul_unit — wide ternary buses
            edges.append(_make_edge(
                iv_id, moa_id, tp * fxp,
                "TmatmulParallelism * FixedPointPrecision (wide activation)",
            ))
            edges.append(_make_edge(
                moa_id, ev_id, tp * fxp,
                "TmatmulParallelism * FixedPointPrecision (wide accumulator out)",
            ))
            edges.append(_make_edge(
                moa_id, unit_id, fxp * tp,
                "FixedPointPrecision * TmatmulParallelism (MOA result)",
            ))

            # vector_registers <-> import/export
            edges.append(_make_edge(
                vr_id, iv_id, vp * fxp,
                "VectorParallelism * FixedPointPrecision",
            ))
            edges.append(_make_edge(
                ev_id, vr_id, vp * fxp,
                "VectorParallelism * FixedPointPrecision",
            ))

            # tmatmul_unit -> core
            edges.append(_make_edge(
                unit_id, ternip_id, fxp * tp,
                "FixedPointPrecision * TmatmulParallelism (unit result)",
            ))

    # Instruction fetcher
    edges.append(_make_edge(
        "axi_dma_instr", "instruction_decode", iw,
        "InstructionWidth (AXI DMA -> decoder)",
    ))

    # XRT shell — single platform-side node that delivers instructions to
    # the decoder. axi_dma_instr remains as the kernel-side DMA bridge.
    nodes.append(_make_node(
        node_id="xrt_shell", label="XRT shell",
        node_type="xrt_shell", params=params,
    ))
    edges.append(_make_edge(
        "xrt_shell", "instruction_decode", iw,
        "InstrFetchWidth (instructions from host via XRT)",
    ))

    return nodes, edges


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_VARIANT_DISPATCH = {
    "NumSeparateAxiInstances": _build_NumSeparateAxiInstances,
    "NumDdrBanksPerTmatmul": _build_NumDdrBanksPerTmatmul,
    "NumTmatmulBanksPerCore": _build_NumTmatmulBanksPerCore,
}


def build_topology(
    variant: ArchVariant,
    params: ParamsDict,
) -> tuple[list[Node], list[Edge]]:
    """Compute the (nodes, edges) graph for a given variant + parameters.

    Raises ValueError on inconsistent (variant, params), e.g. for
    NumTmatmulBanksPerCore when D % NumDdrBanksUsed != 0.
    """
    if variant not in _VARIANT_DISPATCH:
        raise ValueError(f"unknown architecture variant {variant!r}")
    merged = _merge_defaults(params)
    nodes, edges = _VARIANT_DISPATCH[variant](merged)
    return nodes, edges
