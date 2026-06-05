"""Per-node cell-count formulas. See lib/api.py for the contract.

OWNED BY: Phase 1 Agent B (Topology + Cells Builder).

Formulas here are intentionally coarse — they capture the shape of each
node's area growth with parameters, not absolute LUT count accuracy.
The visualizer uses sqrt(cell_count) for node radius, so a 2x error in
absolute count only moves the radius by ~40%.
"""
from __future__ import annotations

import math

from lib.api import CellEstimate, ParamsDict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clog2(n: int) -> int:
    """Ceiling log2, matching Verilog's $clog2 (clog2(1) = 0)."""
    if n <= 1:
        return 0
    return (n - 1).bit_length()


def _scale(count: int, bs_factor: int) -> int:
    if bs_factor <= 1:
        return count
    return count * bs_factor


# ---------------------------------------------------------------------------
# Per-node-type formulas
# ---------------------------------------------------------------------------

def _est_DRAM(params: ParamsDict) -> tuple[int, str, str]:
    # External to the FPGA — no on-chip cells.
    return 0, "0 (external DDR)", "0"


def _est_axi_dma_instr(params: ParamsDict) -> tuple[int, str, str]:
    # Small AXI DMA — mostly fixed control logic.
    count = 500
    return count, "~500 (fixed AXI DMA control logic)", f"= {count}"


def _est_tmatmul_dma(params: ParamsDict) -> tuple[int, str, str]:
    # axi_dma_rd + gbfifo_tmatmul: width-converter FIFO that depends on the
    # DDR data width on the input side and TmatmulParallelism on the output.
    dw = int(params.get("DdrDataWidth", 512))
    tp = int(params.get("TmatmulParallelism", 128))
    count = dw * 4 + tp * 8
    formula = "DdrDataWidth * 4 + TmatmulParallelism * 8"
    breakdown = f"{dw} * 4 + {tp} * 8 = {count}"
    return count, formula, breakdown


def _est_MOA(params: ParamsDict) -> tuple[int, str, str]:
    # multioperand_accumulator: log2-deep pipelined adder tree.
    # Width of each stage scales with the accumulator precision.
    tp = int(params.get("TmatmulParallelism", 128))
    fxp = int(params.get("FixedPointPrecision", 16))
    depth = max(_clog2(tp), 1)
    count = tp * fxp * depth
    formula = "TmatmulParallelism * FixedPointPrecision * ceil(log2(TmatmulParallelism))"
    breakdown = f"{tp} * {fxp} * {depth} = {count}"
    return count, formula, breakdown


def _est_importvector(params: ParamsDict) -> tuple[int, str, str]:
    # Per-slice FIFO storage (one slice of D / N elements).
    d = int(params.get("D", 1024))
    n = max(int(params.get("NumDdrBanksUsed", 4)), 1)
    fxp = int(params.get("FixedPointPrecision", 16))
    count = (d // n) * fxp * 2
    formula = "(D / NumDdrBanksUsed) * FixedPointPrecision * 2"
    breakdown = f"({d} / {n}) * {fxp} * 2 = {count}"
    return count, formula, breakdown


def _est_exportvector(params: ParamsDict) -> tuple[int, str, str]:
    # Symmetric with importvector.
    d = int(params.get("D", 1024))
    n = max(int(params.get("NumDdrBanksUsed", 4)), 1)
    fxp = int(params.get("FixedPointPrecision", 16))
    count = (d // n) * fxp * 2
    formula = "(D / NumDdrBanksUsed) * FixedPointPrecision * 2"
    breakdown = f"({d} / {n}) * {fxp} * 2 = {count}"
    return count, formula, breakdown


def _est_tmatmul_unit(params: ParamsDict) -> tuple[int, str, str]:
    # Combines MOA + importvector + exportvector + state machine glue.
    moa_count, _, _ = _est_MOA(params)
    iv_count, _, _ = _est_importvector(params)
    ev_count, _, _ = _est_exportvector(params)
    control = 200
    count = moa_count + iv_count + ev_count + control
    formula = "MOA + importvector + exportvector + 200 (control)"
    breakdown = f"{moa_count} + {iv_count} + {ev_count} + {control} = {count}"
    return count, formula, breakdown


def _est_RMS(params: ParamsDict) -> tuple[int, str, str]:
    # Accumulator + norm multipliers + sqrt LUT.
    d = int(params.get("D", 1024))
    fxp = int(params.get("FixedPointPrecision", 16))
    count = d * fxp * 4
    formula = "D * FixedPointPrecision * 4"
    breakdown = f"{d} * {fxp} * 4 = {count}"
    return count, formula, breakdown


def _est_loadstore(params: ParamsDict) -> tuple[int, str, str]:
    # Read path + write path between vector registers and DRAM.
    d = int(params.get("D", 1024))
    fxp = int(params.get("FixedPointPrecision", 16))
    count = d * fxp * 2
    formula = "D * FixedPointPrecision * 2"
    breakdown = f"{d} * {fxp} * 2 = {count}"
    return count, formula, breakdown


def _est_rowwise_op(params: ParamsDict) -> tuple[int, str, str]:
    # Per-lane elementwise arithmetic.
    vp = int(params.get("VectorParallelism", 4))
    fxp = int(params.get("FixedPointPrecision", 16))
    count = vp * fxp * 8
    formula = "VectorParallelism * FixedPointPrecision * 8"
    breakdown = f"{vp} * {fxp} * 8 = {count}"
    return count, formula, breakdown


def _est_vector_registers(params: ParamsDict) -> tuple[int, str, str]:
    # BRAM-backed; reported as LUT-equivalent for visualization purposes.
    nvr = int(params.get("NumVectorRegisters", 4))
    d = int(params.get("D", 1024))
    fxp = int(params.get("FixedPointPrecision", 16))
    count = (nvr * d * fxp) // 9
    formula = "NumVectorRegisters * D * FixedPointPrecision / 9 (LUT-equiv for BRAM)"
    breakdown = f"{nvr} * {d} * {fxp} / 9 = {count}"
    return count, formula, breakdown


def _est_instruction_decode(params: ParamsDict) -> tuple[int, str, str]:
    iw = int(params.get("InstructionWidth", 128))
    count = iw * 4
    formula = "InstructionWidth * 4 (FIFO + decoder)"
    breakdown = f"{iw} * 4 = {count}"
    return count, formula, breakdown


def _est_ternip_core(params: ParamsDict) -> tuple[int, str, str]:
    # Compound — sum of typical children (MOA + IV + EV + RMS + loadstore +
    # rowwise + vector_registers + instruction_decode). Used for the
    # NumSeparateAxiInstances variant where each AXI instance is one ternip_core.
    moa, _, _ = _est_MOA(params)
    iv, _, _ = _est_importvector(params)
    ev, _, _ = _est_exportvector(params)
    rms, _, _ = _est_RMS(params)
    ls, _, _ = _est_loadstore(params)
    rw, _, _ = _est_rowwise_op(params)
    vr, _, _ = _est_vector_registers(params)
    idc, _, _ = _est_instruction_decode(params)
    count = moa + iv + ev + rms + ls + rw + vr + idc
    formula = "MOA + IV + EV + RMS + loadstore + rowwise + vector_registers + instruction_decode"
    breakdown = (
        f"{moa} + {iv} + {ev} + {rms} + {ls} + {rw} + {vr} + {idc} = {count}"
    )
    return count, formula, breakdown


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_DISPATCH = {
    "DRAM": _est_DRAM,
    "axi_dma_instr": _est_axi_dma_instr,
    "tmatmul_dma": _est_tmatmul_dma,
    "MOA": _est_MOA,
    "importvector": _est_importvector,
    "exportvector": _est_exportvector,
    "tmatmul_unit": _est_tmatmul_unit,
    "RMS": _est_RMS,
    "loadstore": _est_loadstore,
    "rowwise_op": _est_rowwise_op,
    "vector_registers": _est_vector_registers,
    "instruction_decode": _est_instruction_decode,
    "ternip_core": _est_ternip_core,
}


def estimate_cells(
    node_type: str,
    params: ParamsDict,
    bs_factor: int = 1,
) -> CellEstimate:
    """Per-node-type cell-count estimate.

    `bs_factor` scales the result linearly when the node represents
    per-core hardware replicated across BatchSize cores.
    """
    if node_type not in _DISPATCH:
        raise ValueError(f"unknown node_type {node_type!r}")
    count, formula, breakdown = _DISPATCH[node_type](params)
    scaled = _scale(count, bs_factor)
    if bs_factor > 1 and count != 0:
        formula = f"({formula}) * bs_factor"
        breakdown = f"({breakdown}) * {bs_factor} = {scaled}"
    return {"count": scaled, "formula": formula, "breakdown": breakdown}
