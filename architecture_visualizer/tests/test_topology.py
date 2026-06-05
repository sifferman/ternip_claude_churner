"""Tests for lib/topology.py — Phase 1 Agent B."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make `import lib.foo` work from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.api import ARCH_VARIANTS  # noqa: E402
from lib.topology import build_topology  # noqa: E402


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


# ---------------------------------------------------------------------------
# Structural invariants
# ---------------------------------------------------------------------------

def _validate_graph(nodes, edges):
    """Check shape, uniqueness, and that every edge endpoint resolves."""
    ids = [n["id"] for n in nodes]
    assert len(ids) == len(set(ids)), f"Duplicate node ids: {ids}"

    required_node_keys = {"id", "label", "type", "bank", "core",
                          "cell_count", "slr"}
    for n in nodes:
        missing = required_node_keys - set(n.keys())
        assert not missing, f"node {n.get('id')} missing keys {missing}"
        assert isinstance(n["cell_count"], int)
        assert n["cell_count"] >= 0

    id_set = set(ids)
    for e in edges:
        for key in ("source", "target", "bus_bits", "formula"):
            assert key in e, f"edge missing key {key}: {e}"
        assert e["source"] in id_set, f"dangling source {e['source']}"
        assert e["target"] in id_set, f"dangling target {e['target']}"
        assert e["bus_bits"] >= 0


@pytest.mark.parametrize("variant", ARCH_VARIANTS)
def test_default_params_each_variant(variant):
    nodes, edges = build_topology(variant, DEFAULT_PARAMS)
    assert len(nodes) > 5, f"{variant} should have >5 nodes"
    assert len(edges) > 0, f"{variant} should have edges"
    _validate_graph(nodes, edges)


@pytest.mark.parametrize("variant", ARCH_VARIANTS)
@pytest.mark.parametrize("n", [1, 2, 4])
def test_each_variant_at_each_N(variant, n):
    if variant == "NumTmatmulBanksPerCore" and (1024 % n) != 0:
        pytest.skip("variant doesn't support D % N != 0")
    params = dict(DEFAULT_PARAMS, NumDdrBanksUsed=n)
    nodes, edges = build_topology(variant, params)
    _validate_graph(nodes, edges)
    # Always exactly N DRAM banks regardless of variant.
    dram_count = sum(1 for n_ in nodes if n_["type"] == "DRAM")
    assert dram_count == n


@pytest.mark.parametrize("bs", [1, 4])
def test_column_slice_scales_with_BatchSize(bs):
    params = dict(DEFAULT_PARAMS, BatchSize=bs)
    nodes, _ = build_topology("NumTmatmulBanksPerCore", params)
    n = params["NumDdrBanksUsed"]
    tmatmul_units = [n_ for n_ in nodes if n_["type"] == "tmatmul_unit"]
    assert len(tmatmul_units) == bs * n
    ternip_cores = [n_ for n_ in nodes if n_["type"] == "ternip_core"]
    assert len(ternip_cores) == bs


def test_column_slice_BS20_N4_produces_80_tmatmul_units():
    params = dict(DEFAULT_PARAMS, BatchSize=20, NumDdrBanksUsed=4)
    nodes, edges = build_topology("NumTmatmulBanksPerCore", params)
    tmatmul_units = [n_ for n_ in nodes if n_["type"] == "tmatmul_unit"]
    assert len(tmatmul_units) == 80
    moas = [n_ for n_ in nodes if n_["type"] == "MOA"]
    assert len(moas) == 80
    _validate_graph(nodes, edges)


def test_column_slice_rejects_non_divisible_N():
    # D=1024 is divisible by 1,2,4 but not by 3.
    params = dict(DEFAULT_PARAMS, NumDdrBanksUsed=3)
    with pytest.raises(ValueError):
        build_topology("NumTmatmulBanksPerCore", params)


def test_column_slice_rejects_non_divisible_D():
    params = dict(DEFAULT_PARAMS, D=1023, NumDdrBanksUsed=2)
    with pytest.raises(ValueError):
        build_topology("NumTmatmulBanksPerCore", params)


def test_unknown_variant_raises():
    with pytest.raises(ValueError):
        build_topology("NotAVariant", DEFAULT_PARAMS)


# ---------------------------------------------------------------------------
# Variant-specific structural checks
# ---------------------------------------------------------------------------

def test_separate_axi_has_no_shared_logic():
    params = dict(DEFAULT_PARAMS, NumDdrBanksUsed=4)
    nodes, _ = build_topology("NumSeparateAxiInstances", params)
    # Each AXI instance has its own everything: 4 ternip_cores, 4 MOAs, etc.
    n = params["NumDdrBanksUsed"]
    assert sum(1 for x in nodes if x["type"] == "ternip_core") == n
    assert sum(1 for x in nodes if x["type"] == "MOA") == n
    assert sum(1 for x in nodes if x["type"] == "axi_dma_instr") == n
    assert sum(1 for x in nodes if x["type"] == "instruction_decode") == n
    assert sum(1 for x in nodes if x["type"] == "RMS") == n
    assert sum(1 for x in nodes if x["type"] == "loadstore") == n


def test_banks_per_tmatmul_shares_compute():
    params = dict(DEFAULT_PARAMS, NumDdrBanksUsed=4)
    nodes, _ = build_topology("NumDdrBanksPerTmatmul", params)
    n = params["NumDdrBanksUsed"]
    # N tmatmul_dma but only 1 MOA / IV / EV / RMS / etc.
    assert sum(1 for x in nodes if x["type"] == "tmatmul_dma") == n
    assert sum(1 for x in nodes if x["type"] == "MOA") == 1
    assert sum(1 for x in nodes if x["type"] == "importvector") == 1
    assert sum(1 for x in nodes if x["type"] == "exportvector") == 1
    assert sum(1 for x in nodes if x["type"] == "RMS") == 1
    assert sum(1 for x in nodes if x["type"] == "loadstore") == 1
    assert sum(1 for x in nodes if x["type"] == "ternip_core") == 1


def test_column_slice_broadcasts_dma_to_all_units():
    params = dict(DEFAULT_PARAMS, NumDdrBanksUsed=4, BatchSize=1)
    nodes, edges = build_topology("NumTmatmulBanksPerCore", params)
    n = params["NumDdrBanksUsed"]
    # Per spec: each tmatmul_dma[b] feeds every tmatmul_unit[u] in every
    # core. With BS=1 there are N units => N*N broadcast edges.
    dma_to_unit = [
        e for e in edges
        if e["source"].startswith("tmatmul_dma_b")
        and e["target"].startswith("tmatmul_unit_")
    ]
    assert len(dma_to_unit) == n * n


def test_dram_banks_have_slr_pinning():
    nodes, _ = build_topology("NumSeparateAxiInstances", DEFAULT_PARAMS)
    drams = [n for n in nodes if n["type"] == "DRAM"]
    for b, node in enumerate(sorted(drams, key=lambda n_: n_["bank"])):
        assert node["slr"] == b


def test_edges_carry_formula_strings():
    nodes, edges = build_topology("NumTmatmulBanksPerCore", DEFAULT_PARAMS)
    for e in edges:
        assert isinstance(e["formula"], str) and len(e["formula"]) > 0


def test_bus_bits_reflect_TmatmulParallelism():
    base = dict(DEFAULT_PARAMS, TmatmulParallelism=128)
    nodes_b, edges_b = build_topology("NumTmatmulBanksPerCore", base)
    half = dict(DEFAULT_PARAMS, TmatmulParallelism=64)
    nodes_h, edges_h = build_topology("NumTmatmulBanksPerCore", half)

    def _ternary_edge(edges):
        for e in edges:
            if "ternary stream" in e["formula"]:
                return e["bus_bits"]
        raise AssertionError("no ternary-stream edge found")

    assert _ternary_edge(edges_b) == 2 * _ternary_edge(edges_h)


# ---------------------------------------------------------------------------
# Orchestrator sanity check (verbatim from the agent prompt)
# ---------------------------------------------------------------------------

def test_orchestrator_sanity_check():
    nodes, edges = build_topology("NumTmatmulBanksPerCore", {
        "TmatmulParallelism": 128, "VectorParallelism": 4, "BatchSize": 1,
        "NumDdrBanksUsed": 4, "D": 1024, "FixedPointPrecision": 16,
        "NumVectorRegisters": 4, "DdrDataWidth": 512, "InstructionWidth": 128,
        "DramNumBanks": 4,
    })
    assert len(nodes) > 10
    assert all("id" in n and "type" in n and "cell_count" in n for n in nodes)
    assert all(
        "source" in e and "target" in e and "bus_bits" in e for e in edges
    )
