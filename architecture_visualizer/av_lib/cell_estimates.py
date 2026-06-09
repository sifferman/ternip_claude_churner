"""Per-node cell-count formulas. See lib/api.py for the contract.

OWNED BY: Phase 1 Agent B (Topology + Cells Builder).

Formulas here are intentionally coarse — they capture the shape of each
node's area growth with parameters, not absolute LUT count accuracy.
The visualizer uses sqrt(cell_count) for node radius, so a 2x error in
absolute count only moves the radius by ~40%.
"""
from __future__ import annotations

import math

from av_lib.api import CellEstimate, ParamsDict


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
    # multioperand_accumulator: log2-deep pipelined adder tree, NEXT_STAGE_FANIN=2.
    # Audit-grounded: at TP=128 FxP=16 real LUT ~7k. The naive
    # TP*FxP*log2(TP) = 14k overstates by 2x because the tree's leaf stages
    # are narrow ternary mults, not full FxP adders.
    tp = int(params.get("TmatmulParallelism", 128))
    fxp = int(params.get("FixedPointPrecision", 16))
    depth = max(_clog2(tp), 1)
    count = tp * fxp * depth // 2
    formula = "TP * FxP * ceil(log2(TP)) / 2 (tree leaves are narrow)"
    breakdown = f"{tp} * {fxp} * {depth} / 2 = {count}"
    return count, formula, breakdown


def _est_importvector(params: ParamsDict) -> tuple[int, str, str]:
    # ternip_pipelined_mem double-buffered: storage = ImportVectorRowWidth *
    # FxP * NumEntries (NumEntries typically 2 for double-buffer). The caller
    # (topology builder) is responsible for passing an effective `D` matching
    # this node's ImportVectorLength - full D for NSAI/NDB, D/N for NTB per
    # tmatmul_unit. The /9 reflects the BRAM-as-LUT-equivalent factor (one
    # RAMB18 ~= 9 LUT-equivalents per stored bit for visualization scaling).
    d = int(params.get("D", 1024))
    fxp = int(params.get("FixedPointPrecision", 16))
    count = (d * fxp * 2) // 9
    formula = "D * FxP * 2 / 9 (LUT-equiv for BRAM double-buffered storage)"
    breakdown = f"{d} * {fxp} * 2 / 9 = {count}"
    return count, formula, breakdown


def _est_exportvector(params: ParamsDict) -> tuple[int, str, str]:
    # ternip_pipelined_mem; NumChunksPerVector = D / VP entries of FxP wide.
    # Independent of N - per-unit EV in NTB stores RowParallelism * FxP per
    # cycle * (D/N / RowParallelism) entries which simplifies to D/N * FxP.
    # Caller passes effective D matching the node (full for NSAI/NDB,
    # D/N for NTB per-unit).
    d = int(params.get("D", 1024))
    fxp = int(params.get("FixedPointPrecision", 16))
    count = (d * fxp * 2) // 9
    formula = "D * FxP * 2 / 9 (LUT-equiv for BRAM)"
    breakdown = f"{d} * {fxp} * 2 / 9 = {count}"
    return count, formula, breakdown


def _est_tmatmul_unit(params: ParamsDict) -> tuple[int, str, str]:
    # The tmatmul_unit wrapper in NumTmatmulBanksPerCore. Its MOA / IV / EV
    # are rendered as separate child nodes; this node holds only the unit's
    # state machine + tmatmul_dma-side handshake. Same convention as
    # ternip_core: glue-only, no children-sum (would double-count).
    count = 500
    formula = "~500 (tmatmul_unit FSM + handshake; children counted separately)"
    breakdown = f"= {count}"
    return count, formula, breakdown


def _est_RMS(params: ParamsDict) -> tuple[int, str, str]:
    # Audit-grounded: real RMS ~10-15k LUT. Contains a small MOA that sums
    # the D operands (D-wide accumulator tree), a sqrt LUT, a fixed-point
    # multiplier and a divider. The D-wide accumulator dominates; the rest
    # scales with VP for the per-lane normalize.
    d = int(params.get("D", 1024))
    vp = int(params.get("VectorParallelism", 4))
    fxp = int(params.get("FixedPointPrecision", 16))
    # D-wide accumulator tree (compressed) + VP-wide per-lane normalize +
    # fixed-cost sqrt LUT + divider.
    count = (d * fxp) // 2 + vp * fxp * 16 + 2000
    formula = "D*FxP/2 + VP*FxP*16 + 2000 (acc tree + per-lane + sqrt/div)"
    breakdown = (
        f"{d}*{fxp}/2 + {vp}*{fxp}*16 + 2000 = {count}"
    )
    return count, formula, breakdown


def _est_loadstore(params: ParamsDict) -> tuple[int, str, str]:
    # Audit-grounded: real loadstore ~3k LUT. It's a small FSM moving
    # vector_chunk_t = VP*FxP chunks between vector_registers and the AXI
    # bus; the D-wide path is on the DRAM side (handled by gbfifo_loadstore
    # which is not modeled separately). The visualizer's loadstore node
    # represents the kernel-side FSM + per-lane control.
    vp = int(params.get("VectorParallelism", 4))
    fxp = int(params.get("FixedPointPrecision", 16))
    count = vp * fxp * 16 + 1500
    formula = "VP*FxP*16 + 1500 (per-lane control + DMA-side FSM)"
    breakdown = f"{vp}*{fxp}*16 + 1500 = {count}"
    return count, formula, breakdown


def _est_rowwise_op(params: ParamsDict) -> tuple[int, str, str]:
    # Audit-grounded: real rowwise_op ~5k LUT. Has elementwise mul,
    # sigmoid/silu LUT (when not UseHardSigmoid), divider. Scales with VP
    # for per-lane parallel arithmetic.
    vp = int(params.get("VectorParallelism", 4))
    fxp = int(params.get("FixedPointPrecision", 16))
    count = vp * fxp * 64
    formula = "VP*FxP*64 (per-lane mul + sigmoid LUT + divider)"
    breakdown = f"{vp}*{fxp}*64 = {count}"
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


def _est_xrt_shell(params: ParamsDict) -> tuple[int, str, str]:
    # XRT static region: PCIe, AXI interconnect, memory subsystem, H2C/C2H
    # async FIFOs, etc. Per real builds, ~174k LUTs (10% of AU250). Reported
    # at coarse magnitude here.
    count = 174_000
    formula = "~174k (XRT shell static region, fixed)"
    breakdown = f"= {count}"
    return count, formula, breakdown


def _est_ternip_core(params: ParamsDict) -> tuple[int, str, str]:
    # Arbitration glue only — the per-core FSM, instruction-dispatch mux, and
    # vector_register port arbitration. Children (MOA, IV, EV, RMS, etc.) are
    # rendered as separate nodes with their own cell counts; summing them
    # here would double-count. Order-of-magnitude estimate: ~3k LUT for the
    # FSM + arbiter logic.
    count = 3000
    formula = "~3k LUT (FSM + arbitration glue; children counted separately)"
    breakdown = f"= {count}"
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
    "xrt_shell": _est_xrt_shell,
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
