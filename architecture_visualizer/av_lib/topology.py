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

from av_lib.api import ArchVariant, Edge, Node, ParamsDict
from av_lib.cell_estimates import estimate_cells


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
    "InstrFetchWidth": 32,
    "CoreInterconnectNumStages": 8,
    "DramNumBanks": 4,
}


# AXI-Lite control bus width (s_axi_stall, s_axi_rst, s_axi_debug bundled).
_AXI_LITE_WIDTH = 32


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


def _make_edge(
    source: str,
    target: str,
    bus_bits: int,
    formula: str,
    pipeline_stages: int = 0,
) -> Edge:
    edge: Edge = {
        "source": source,
        "target": target,
        "bus_bits": int(bus_bits),
        "formula": formula,
    }
    if pipeline_stages > 0:
        edge["pipeline_stages"] = int(pipeline_stages)
    return edge


def _add_vr_fu_edges(
    edges: list,
    vr_id: str,
    rms_id: str,
    ls_id: str,
    rw_id: str,
    iv_ids: list[str],
    ev_ids: list[str],
    vp: int,
    fxp: int,
) -> None:
    """Add the standard data-path edges between vector_registers and each FU.

    In ternip_core.sv, vector_registers has a single shared read/write port
    OR-muxed across all FUs (loadstore + rms + rowwise + N tmatmul_units' IV
    and EV). The visualizer represents this as direct VR<->FU edges, one per
    FU, all VP*FxP wide (the vector_chunk_t width).
    """
    bw = vp * fxp
    formula = "VectorParallelism * FixedPointPrecision (vector_chunk_t)"
    # RMS / loadstore / rowwise_operation: bidirectional through VR's port.
    for fu in (rms_id, ls_id, rw_id):
        edges.append(_make_edge(vr_id, fu, bw, formula))
        edges.append(_make_edge(fu, vr_id, bw, formula))
    # importvector: VR reads -> IV (per-unit or per-core IV)
    for iv in iv_ids:
        edges.append(_make_edge(vr_id, iv, bw, formula))
    # exportvector: EV writes -> VR (per-unit or per-core EV)
    for ev in ev_ids:
        edges.append(_make_edge(ev, vr_id, bw, formula))


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
    """N parallel AXI instances. Each is a self-contained kernel boundary
    with its own m_axi_tmatmul, m_axi_loadstore, axi_dma_instr, and N=1
    per-instance ternip_core (replicated BS times via the gearbox-shared
    DDR bus). ImportVectorLength = D (no column-slicing)."""
    n = int(params["NumDdrBanksUsed"])
    if n < 1:
        raise ValueError("NumDdrBanksUsed must be >= 1")
    bs = max(int(params.get("BatchSize", 1)), 1)
    tp = int(params["TmatmulParallelism"])
    vp = int(params["VectorParallelism"])
    fxp = int(params["FixedPointPrecision"])
    iw = int(params["InstructionWidth"])
    ifw = int(params.get("InstrFetchWidth", 32))
    dw = int(params["DdrDataWidth"])
    d = int(params["D"])
    pipe_stages = int(params.get("CoreInterconnectNumStages", 8))

    # Each AXI instance has its own tmatmul, full-D-wide.
    ivl = d
    ivr = min(tp, ivl)               # ImportVectorRowWidth
    row_parallelism = max(1, tp // ivl)
    moa_out_bits = row_parallelism * fxp
    iv_to_moa_bits = ivr * fxp       # IV -> MOA activation bus

    nodes: list[Node] = []
    edges: list[Edge] = []

    _add_dram_banks(nodes, params, n)

    # XRT shell — single platform-side node, drives AXI-Lite control to
    # every kernel instance and writes instructions to DRAM (host-side).
    nodes.append(_make_node(
        node_id="xrt_shell", label="XRT shell",
        node_type="xrt_shell", params=params,
    ))

    for i in range(n):
        # Per-AXI-instance shared blocks (1 of each per AXI cell):
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

        # Instruction path: host writes to DRAM; kernel DMA-reads them out.
        # DRAM -> axi_dma_instr (DdrDataWidth) -> instruction_decode
        # (InstrFetchWidth=32 AXIS surface) -> ternip_core (InstructionWidth=128).
        edges.append(_make_edge(
            dram_id, adi_id, dw,
            "DdrDataWidth (instruction DMA reads from DRAM)",
        ))
        edges.append(_make_edge(
            adi_id, idc_id, ifw,
            "InstrFetchWidth (AXIS instruction stream)",
        ))

        # Ternary weight stream: DRAM -> tmatmul_dma -> per-core MOAs
        edges.append(_make_edge(
            dram_id, tmd_id, dw,
            "DdrDataWidth (R-channel ternary weights)",
        ))

        # AXI-Lite control bundle from XRT shell (s_axi_stall, s_axi_rst,
        # s_axi_debug, axi_dma's S_AXI_LITE — all small AXI-Lite buses).
        edges.append(_make_edge(
            "xrt_shell", adi_id, _AXI_LITE_WIDTH,
            "AXI-Lite control (bundled: stall, rst, debug, dma_lite)",
        ))

        # Per-core hardware INSIDE this AXI instance — BatchSize copies.
        # Each core has its own MOA / IV / EV / RMS / loadstore / rowwise /
        # vector_registers. The cores share the per-instance AXI bus to DRAM
        # via a gearbox FIFO (one DDR<->kernel pair per AXI instance, not BS).
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

            # Instruction dispatch into this core. Crosses SLR via
            # ternip_buffered's pipelined_interconnect (CoreInterconnectNumStages).
            edges.append(_make_edge(
                idc_id, ternip_id, iw,
                "InstructionWidth (decoded -> core)",
                pipeline_stages=pipe_stages,
            ))

            # tmatmul_dma -> this core's MOA (ternary stream)
            edges.append(_make_edge(
                tmd_id, moa_id, tp * 2,
                "TmatmulParallelism * 2 (ternary stream from DDR)",
            ))

            # IV -> MOA (wide activation = ImportVectorRowWidth * FxP)
            edges.append(_make_edge(
                iv_id, moa_id, iv_to_moa_bits,
                f"min(TP,D) * FixedPointPrecision = {ivr} * {fxp} "
                f"(wide activation)",
            ))

            # MOA -> EV (reduced output)
            edges.append(_make_edge(
                moa_id, ev_id, moa_out_bits,
                "RowParallelism * FixedPointPrecision "
                "(MOA reduces ImportVectorRowWidth ternary operands "
                "to RowParallelism scalars)",
            ))

            # Vector registers <-> all FUs (single shared port in RTL,
            # rendered as direct VR<->FU edges).
            _add_vr_fu_edges(
                edges, vr_id, rms_id, ls_id, rw_id, [iv_id], [ev_id], vp, fxp,
            )

        # ONE shared loadstore<->DRAM edge per AXI instance (not BS edges).
        # The BS cores route through gbfifo_loadstore to share the AXI bus.
        # Represent this with edges from the first core's loadstore; the
        # other BS-1 cores share that path via the gearbox (modeled as
        # cell mass on the loadstore node, not as separate edges).
        if bs > 0:
            ls0_id = f"loadstore_i{i}_c0"
            edges.append(_make_edge(
                ls0_id, dram_id, dw,
                "DdrDataWidth (loadstore W-channel; "
                "shared across BS cores via gearbox)",
            ))
            edges.append(_make_edge(
                dram_id, ls0_id, dw,
                "DdrDataWidth (loadstore R-channel; "
                "shared across BS cores via gearbox)",
            ))

    return nodes, edges


# ---------------------------------------------------------------------------
# Variant: NumDdrBanksPerTmatmul
# ---------------------------------------------------------------------------

def _build_NumDdrBanksPerTmatmul(
    params: ParamsDict,
) -> tuple[list[Node], list[Edge]]:
    """One kernel, one tmatmul module fed by N DDR banks. Per RTL
    (ternip_tmatmul.sv bank_lane[b]), the tmatmul instantiates N MOAs and
    N exportvectors (one per bank) but a SINGLE shared full-D-wide
    importvector. BatchSize replicates the entire ternip_core."""
    n = int(params["NumDdrBanksUsed"])
    if n < 1:
        raise ValueError("NumDdrBanksUsed must be >= 1")
    bs = max(int(params.get("BatchSize", 1)), 1)
    tp = int(params["TmatmulParallelism"])
    vp = int(params["VectorParallelism"])
    fxp = int(params["FixedPointPrecision"])
    iw = int(params["InstructionWidth"])
    ifw = int(params.get("InstrFetchWidth", 32))
    dw = int(params["DdrDataWidth"])
    d = int(params["D"])
    pipe_stages = int(params.get("CoreInterconnectNumStages", 8))

    # Single tmatmul, full D-wide IV; per-bank MOAs each see TP/N of the
    # ternary stream and ImportVectorRowWidth = min(TP, D) of the activation.
    ivl = d
    ivr = min(tp, ivl)
    row_parallelism = max(1, tp // ivl)
    moa_out_bits = row_parallelism * fxp
    iv_to_moa_bits = ivr * fxp

    nodes: list[Node] = []
    edges: list[Edge] = []

    _add_dram_banks(nodes, params, n)

    # XRT shell + AXI-Lite control bundle.
    nodes.append(_make_node(
        node_id="xrt_shell", label="XRT shell",
        node_type="xrt_shell", params=params,
    ))

    # Shared (single-instance) infrastructure
    nodes.append(_make_node(
        node_id="axi_dma_instr", label="axi_dma_instr",
        node_type="axi_dma_instr", params=params,
    ))
    nodes.append(_make_node(
        node_id="instruction_decode", label="instruction_decode",
        node_type="instruction_decode", params=params,
    ))

    # Instruction path: DRAM[0] -> axi_dma_instr (DDR DMA reads) ->
    # instruction_decode (InstrFetchWidth AXIS surface).
    edges.append(_make_edge(
        "dram_b0", "axi_dma_instr", dw,
        "DdrDataWidth (instruction DMA reads from DRAM)",
    ))
    edges.append(_make_edge(
        "axi_dma_instr", "instruction_decode", ifw,
        "InstrFetchWidth (AXIS instruction stream)",
    ))
    edges.append(_make_edge(
        "xrt_shell", "axi_dma_instr", _AXI_LITE_WIDTH,
        "AXI-Lite control (bundled: stall, rst, debug, dma_lite)",
    ))

    # Per-bank tmatmul_dma — one per DDR bank, broadcasts a 1/N slice of
    # the ternary stream to every core's per-bank MOA.
    for b in range(n):
        tmd_id = f"tmatmul_dma_b{b}"
        nodes.append(_make_node(
            node_id=tmd_id, label=f"tmatmul_dma[{b}]",
            node_type="tmatmul_dma", params=params, bank=b,
        ))
        edges.append(_make_edge(
            f"dram_b{b}", tmd_id, dw,
            "DdrDataWidth (R-channel ternary weights)",
        ))

    # Per-core hardware — BatchSize copies. Per RTL, each tmatmul has:
    #   - 1 shared importvector (full-D-wide)
    #   - N MOAs (bank_lane[b].multioperand_accumulator)
    #   - N exportvectors (bank_lane[b].exportvector)
    #   - 1 each of RMS / loadstore / rowwise_op / vector_registers
    for c in range(bs):
        ternip_id = f"ternip_core_c{c}"
        iv_id = f"importvector_c{c}"
        rms_id = f"rms_c{c}"
        ls_id = f"loadstore_c{c}"
        rw_id = f"rowwise_op_c{c}"
        vr_id = f"vector_registers_c{c}"

        nodes.append(_make_node(
            node_id=ternip_id, label=f"ternip_core[{c}]",
            node_type="ternip_core", params=params, core=c,
        ))
        nodes.append(_make_node(
            node_id=iv_id, label=f"importvector[{c}]",
            node_type="importvector", params=params, core=c,
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

        # N per-bank MOAs and N per-bank exportvectors in this core.
        ev_ids: list[str] = []
        for b in range(n):
            moa_id = f"moa_c{c}_b{b}"
            ev_id = f"exportvector_c{c}_b{b}"
            ev_ids.append(ev_id)

            nodes.append(_make_node(
                node_id=moa_id, label=f"MOA[{c}][bank{b}]",
                node_type="MOA", params=params, bank=b, core=c,
            ))
            nodes.append(_make_node(
                node_id=ev_id, label=f"exportvector[{c}][bank{b}]",
                node_type="exportvector", params=params, bank=b, core=c,
            ))

            # tmatmul_dma[b] -> this core's per-bank MOA[c][b]
            edges.append(_make_edge(
                f"tmatmul_dma_b{b}", moa_id, tp * 2,
                "TmatmulParallelism * 2 (ternary stream, "
                "per-bank 1/N slice of TP)",
            ))

            # IV is shared across all N MOAs in this core; each MOA reads
            # its slice from the same IV port.
            edges.append(_make_edge(
                iv_id, moa_id, iv_to_moa_bits,
                f"min(TP,D) * FixedPointPrecision = {ivr} * {fxp} "
                f"(wide activation, shared from IV)",
            ))

            # MOA -> EV (reduced output, per bank lane)
            edges.append(_make_edge(
                moa_id, ev_id, moa_out_bits,
                "RowParallelism * FixedPointPrecision "
                "(MOA reduces ImportVectorRowWidth operands)",
            ))

        # Instruction dispatch into this core (crosses SLR boundary via
        # ternip_buffered's pipelined_interconnect).
        edges.append(_make_edge(
            "instruction_decode", ternip_id, iw,
            "InstructionWidth (decoded -> core)",
            pipeline_stages=pipe_stages,
        ))

        # Vector registers <-> all FUs.
        _add_vr_fu_edges(
            edges, vr_id, rms_id, ls_id, rw_id, [iv_id], ev_ids, vp, fxp,
        )

    # One shared loadstore<->DRAM[0] pair per kernel (BS cores share the AXI
    # bus through gbfifo_loadstore).
    if bs > 0:
        ls0_id = f"loadstore_c0"
        edges.append(_make_edge(
            ls0_id, "dram_b0", dw,
            "DdrDataWidth (loadstore W-channel; shared across BS cores)",
        ))
        edges.append(_make_edge(
            "dram_b0", ls0_id, dw,
            "DdrDataWidth (loadstore R-channel; shared across BS cores)",
        ))

    return nodes, edges


# ---------------------------------------------------------------------------
# Variant: NumTmatmulBanksPerCore (column-slice)
# ---------------------------------------------------------------------------

def _build_NumTmatmulBanksPerCore(
    params: ParamsDict,
) -> tuple[list[Node], list[Edge]]:
    """Column-slice variant: N tmatmul_units per core, each handling D/N
    of the inner dimension. Per ternip_batched.sv (core_tmatmul_ddr_r_data_i
    [i][u] = tmatmul_ddr_r_data_i[u]), bank b feeds unit b 1-to-1 within
    each core, and bank b feeds the SAME unit-index across all BS cores
    (broadcast across cores, NOT across units within a core)."""
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
    ifw = int(params.get("InstrFetchWidth", 32))
    dw = int(params["DdrDataWidth"])
    pipe_stages = int(params.get("CoreInterconnectNumStages", 8))

    # Column-slice: N tmatmul UNITS per core, each with ImportVectorLength = D/N.
    # RowParallelism = max(1, TP / (D/N)).
    ivl_per_unit = d // n
    ivr_per_unit = min(tp, ivl_per_unit) if ivl_per_unit else tp
    row_parallelism = max(1, tp // ivl_per_unit) if ivl_per_unit else 1
    moa_out_bits = row_parallelism * fxp
    iv_to_moa_bits = ivr_per_unit * fxp

    nodes: list[Node] = []
    edges: list[Edge] = []

    _add_dram_banks(nodes, params, n)

    # XRT shell + AXI-Lite control.
    nodes.append(_make_node(
        node_id="xrt_shell", label="XRT shell",
        node_type="xrt_shell", params=params,
    ))

    # Shared (across all BS cores)
    nodes.append(_make_node(
        node_id="axi_dma_instr", label="axi_dma_instr",
        node_type="axi_dma_instr", params=params,
    ))
    nodes.append(_make_node(
        node_id="instruction_decode", label="instruction_decode",
        node_type="instruction_decode", params=params,
    ))

    # Instruction path: DRAM[0] -> axi_dma_instr -> instruction_decode.
    edges.append(_make_edge(
        "dram_b0", "axi_dma_instr", dw,
        "DdrDataWidth (instruction DMA reads from DRAM)",
    ))
    edges.append(_make_edge(
        "axi_dma_instr", "instruction_decode", ifw,
        "InstrFetchWidth (AXIS instruction stream)",
    ))
    edges.append(_make_edge(
        "xrt_shell", "axi_dma_instr", _AXI_LITE_WIDTH,
        "AXI-Lite control (bundled: stall, rst, debug, dma_lite)",
    ))

    # tmatmul_dma stays singular per bank — its R-channel feeds the
    # u==b indexed tmatmul_unit in EACH of the BS cores (broadcast across
    # cores, 1-to-1 within a core).
    for b in range(n):
        tmd_id = f"tmatmul_dma_b{b}"
        nodes.append(_make_node(
            node_id=tmd_id, label=f"tmatmul_dma[{b}]",
            node_type="tmatmul_dma", params=params, bank=b,
        ))
        edges.append(_make_edge(
            f"dram_b{b}", tmd_id, dw,
            "DdrDataWidth (R-channel ternary weights)",
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

        # instruction_decode -> this core (SLR-crossing pipeline)
        edges.append(_make_edge(
            "instruction_decode", ternip_id, iw,
            "InstructionWidth (decoded -> core)",
            pipeline_stages=pipe_stages,
        ))

        # N tmatmul_units per core, each containing its own MOA / IV / EV.
        iv_ids: list[str] = []
        ev_ids: list[str] = []
        for u in range(n):
            unit_id = f"tmatmul_unit_c{c}_u{u}"
            moa_id = f"moa_c{c}_u{u}"
            iv_id = f"importvector_c{c}_u{u}"
            ev_id = f"exportvector_c{c}_u{u}"
            iv_ids.append(iv_id)
            ev_ids.append(ev_id)

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

            # 1-to-1 within a core: tmatmul_dma[u] -> tmatmul_unit[u].
            # (Broadcast across the BS cores comes for free at BS>1: each
            # core's unit-u draws from the same tmatmul_dma_b{u}.)
            edges.append(_make_edge(
                f"tmatmul_dma_b{u}", unit_id, tp * 2,
                "TmatmulParallelism * 2 (ternary stream, 1-to-1 "
                "bank-to-unit; broadcast across BS cores)",
            ))

            # Wide activation: IV -> MOA. Per-unit IV is D/N wide.
            edges.append(_make_edge(
                iv_id, moa_id, iv_to_moa_bits,
                f"min(TP, D/N) * FixedPointPrecision = "
                f"{ivr_per_unit} * {fxp} (wide activation, per-unit slice)",
            ))

            # MOA -> EV (reduced output)
            edges.append(_make_edge(
                moa_id, ev_id, moa_out_bits,
                "RowParallelism * FixedPointPrecision "
                "(MOA reduces ImportVectorRowWidth operands)",
            ))

        # Vector registers <-> all FUs (single shared port, N IVs + N EVs
        # OR-muxed in addition to RMS/loadstore/rowwise).
        _add_vr_fu_edges(
            edges, vr_id, rms_id, ls_id, rw_id, iv_ids, ev_ids, vp, fxp,
        )

    # ONE shared loadstore<->DRAM[0] pair per kernel (BS cores share the
    # AXI bus through gbfifo_loadstore).
    if bs > 0:
        ls0_id = f"loadstore_c0"
        edges.append(_make_edge(
            ls0_id, "dram_b0", dw,
            "DdrDataWidth (loadstore W-channel; shared across BS cores)",
        ))
        edges.append(_make_edge(
            "dram_b0", ls0_id, dw,
            "DdrDataWidth (loadstore R-channel; shared across BS cores)",
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
