"""Smoke tests for ``av_lib.throughput.compute_tokens_per_sec``.

Each architecture variant has its own subprocess'd
``report_instruction_timing.py`` under
``architecture_visualizer/architectures/<variant>/sw_utils/``. The tests
exercise all three variants end-to-end and assert sane outputs.

These tests are slow (each subprocess loads the MMfreeLM model + runs
the scheduler), so they're marked ``slow``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Make ``import av_lib.X`` work from the repo root.
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


MODEL = "MMfreeLM-370M"

VARIANTS = (
    "NumSeparateAxiInstances",
    "NumDdrBanksPerTmatmul",
    "NumTmatmulBanksPerCore",
)

BASE_CONFIG = {
    "TmatmulParallelism": 128,
    "VectorParallelism":  4,
    "LutParallelism":     1,
    "BatchSize":          1,
    "NumDdrBanksUsed":    4,
    "D":                  1024,
    "FixedPointPrecision": 16,
    "FixedPointExponent": -5,
    "NumVectorRegisters":  4,
    "DdrDataWidth":        512,
    "DdrAddressWidth":     64,
    "ImmediateWidth":      16,
    "InstructionWidth":    128,
    "InstrFetchWidth":     32,
    "DramNumBanks":        4,
    "DramMaxBytesPerSecond": 8 * 2400.0 * 10**6,
    "ClockPeriod":         3.333e-9,
}


@pytest.mark.slow
@pytest.mark.parametrize("variant", VARIANTS)
def test_variant_returns_sane_tokens_per_sec(variant: str) -> None:
    from av_lib.throughput import compute_tokens_per_sec  # noqa: E402

    result = compute_tokens_per_sec(BASE_CONFIG, MODEL, variant=variant)
    assert set(result.keys()) == {"singlecore", "multicore", "clk_freq_mhz"}
    assert result["singlecore"] > 0
    assert result["multicore"] >= result["singlecore"], (
        f"multicore must be >= singlecore for {variant}; "
        f"got {result}"
    )
    # 300 MHz with the 3.333e-9 clock period above.
    assert 250 < result["clk_freq_mhz"] < 350


def test_unknown_variant_raises() -> None:
    from av_lib.throughput import compute_tokens_per_sec  # noqa: E402

    with pytest.raises(ValueError, match="unknown variant"):
        compute_tokens_per_sec(BASE_CONFIG, MODEL, variant="NotARealVariant")
