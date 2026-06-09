"""Tests for lib/cell_estimates.py — Phase 1 Agent B."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make `import lib.foo` work from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from av_lib.cell_estimates import estimate_cells  # noqa: E402


DEFAULT_PARAMS = {
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


ALL_NODE_TYPES = (
    "DRAM",
    "axi_dma_instr",
    "tmatmul_dma",
    "MOA",
    "importvector",
    "exportvector",
    "tmatmul_unit",
    "RMS",
    "loadstore",
    "rowwise_op",
    "vector_registers",
    "instruction_decode",
    "ternip_core",
)


@pytest.mark.parametrize("node_type", ALL_NODE_TYPES)
def test_all_node_types_return_sane_estimate(node_type):
    est = estimate_cells(node_type, DEFAULT_PARAMS)
    assert "count" in est and "formula" in est and "breakdown" in est
    assert isinstance(est["count"], int)
    if node_type == "DRAM":
        assert est["count"] == 0
    else:
        assert est["count"] > 0, f"{node_type} should report a positive count"


def test_unknown_node_type_raises():
    with pytest.raises(ValueError):
        estimate_cells("not_a_real_block", DEFAULT_PARAMS)


def test_halving_TP_halves_MOA():
    full = estimate_cells("MOA", DEFAULT_PARAMS)["count"]
    half_params = dict(DEFAULT_PARAMS, TmatmulParallelism=64)
    half = estimate_cells("MOA", half_params)["count"]
    # MOA = TP * FxP * clog2(TP). 128 -> 64 halves TP and reduces depth by 1.
    # The ratio should be (128*7) / (64*6) = 896 / 384 ≈ 2.33.
    # So full / half should be > 2.
    assert full > 2 * half - 1


def test_bs_factor_scales_linearly():
    one = estimate_cells("RMS", DEFAULT_PARAMS, bs_factor=1)["count"]
    four = estimate_cells("RMS", DEFAULT_PARAMS, bs_factor=4)["count"]
    assert four == 4 * one


def test_bs_factor_does_not_scale_dram():
    one = estimate_cells("DRAM", DEFAULT_PARAMS, bs_factor=1)["count"]
    four = estimate_cells("DRAM", DEFAULT_PARAMS, bs_factor=4)["count"]
    assert one == 0
    assert four == 0


def test_breakdown_includes_concrete_numbers():
    est = estimate_cells("MOA", DEFAULT_PARAMS)
    assert "128" in est["breakdown"]
    assert "16" in est["breakdown"]
    assert str(est["count"]) in est["breakdown"]


def test_vp_affects_rowwise_op():
    base = estimate_cells("rowwise_op", DEFAULT_PARAMS)["count"]
    double_vp = estimate_cells(
        "rowwise_op",
        dict(DEFAULT_PARAMS, VectorParallelism=8),
    )["count"]
    assert double_vp == 2 * base


def test_tmatmul_unit_glue_only_does_not_double_count():
    # Per the visualizer's convention (same as ternip_core): tmatmul_unit's
    # MOA/IV/EV children are rendered as separate nodes with their own
    # cell counts. The tmatmul_unit node itself counts only its FSM/handshake
    # glue, NOT the sum of children, to avoid double-counting.
    moa = estimate_cells("MOA", DEFAULT_PARAMS)["count"]
    iv = estimate_cells("importvector", DEFAULT_PARAMS)["count"]
    ev = estimate_cells("exportvector", DEFAULT_PARAMS)["count"]
    unit = estimate_cells("tmatmul_unit", DEFAULT_PARAMS)["count"]
    assert unit < moa  # MOA alone dwarfs the FSM-only count
    assert unit > 0    # but it's non-zero (FSM + handshake exist)


def test_importvector_scales_with_effective_D():
    # cell_estimates uses params["D"] verbatim - the topology builder is
    # responsible for passing an effective D matching each node's actual
    # ImportVectorLength (D for NSAI/NDB shared IV, D/N for NTB per-unit IV).
    # Doubling D doubles the importvector's storage.
    small = estimate_cells(
        "importvector", dict(DEFAULT_PARAMS, D=256),
    )["count"]
    big = estimate_cells(
        "importvector", dict(DEFAULT_PARAMS, D=1024),
    )["count"]
    assert big == 4 * small  # 1024 / 256 = 4
