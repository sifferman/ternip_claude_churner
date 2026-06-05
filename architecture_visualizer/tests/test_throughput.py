"""Parity tests for ``architecture_visualizer.lib.throughput``.

For each example ``.svh`` config, run the original
``report_instruction_timing.py`` as a subprocess and compare its
reported ``singlecore`` / ``multicore`` tokens_per_second against the
values returned by our in-process ``compute_tokens_per_sec``.

These tests are marked ``slow`` because the subprocess call takes
~20-30 seconds (model load + scheduler).

Note: the project-level ``pytest.ini`` enables ``--import-mode=importlib``
so that ``architecture_visualizer/lib`` does NOT become the top-level
``lib`` package, which would otherwise shadow ``ternary_matmul/sw_utils/lib``
and break the upstream ``Config`` import below.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_TERNARY_MATMUL = _REPO_ROOT / "ternary_matmul"
_SW_UTILS = _TERNARY_MATMUL / "sw_utils"

# Insert sw_utils onto sys.path so ``from lib.config import Config`` works.
# (The throughput module also does this at import time.)
if str(_SW_UTILS) not in sys.path:
    sys.path.insert(0, str(_SW_UTILS))


CONFIG_FILES = [
    "xcu250_D=1024_OneCore.svh",
    "xcu250_D=1024_MaxCores.svh",
    "xcu250_D=1024_BS2_N4.svh",
]

MODEL = "MMfreeLM-370M"

# Fraction tolerance for float comparison (0.01%).
REL_TOL = 1e-4


def _config_path(name: str) -> Path:
    return _TERNARY_MATMUL / "config" / name


def _build_config_dict_from_svh(svh_path: Path) -> dict:
    """Use the existing Config parser to read a .svh, then convert into
    the dict shape ``Config.from_dict`` accepts."""
    from lib.config import Config  # type: ignore[import-not-found]

    cfg = Config([str(svh_path)])
    d: dict = {}
    for name in Config._INT_FIELDS_WITH_DEFAULTS:
        d[name] = getattr(cfg, name)
    for name in Config._STRING_FIELDS:
        d[name] = getattr(cfg, name)
    for name in Config._FLOAT_FIELDS:
        d[name] = getattr(cfg, name)
    return d


_SINGLECORE_RE = re.compile(
    r"singlecore tokens_per_second at\s+([\d.]+)\s*MHz\s*=\s*([0-9.eE+\-]+)"
)
_MULTICORE_RE = re.compile(
    r"multicore tokens_per_second at\s+([\d.]+)\s*MHz\s*=\s*([0-9.eE+\-]+)"
)


def _run_original_script(svh_path: Path) -> tuple[float, float, float]:
    """Subprocess the original ``report_instruction_timing.py`` and parse
    ``(singlecore, multicore, clk_freq_mhz)`` out of stdout."""
    env = {**os.environ, "PYTHONPATH": str(_SW_UTILS)}
    cmd = [
        sys.executable,
        str(_SW_UTILS / "target" / "report_instruction_timing.py"),
        str(svh_path),
        MODEL,
    ]
    # Run with cwd at ternary_matmul so the script's relative
    # huggingface_cache lookup behaves the same way it does for normal
    # repo-root invocations.
    result = subprocess.run(
        cmd,
        cwd=str(_TERNARY_MATMUL),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"report_instruction_timing.py failed for {svh_path.name}:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    sc_match = _SINGLECORE_RE.search(result.stdout)
    mc_match = _MULTICORE_RE.search(result.stdout)
    if not sc_match or not mc_match:
        raise AssertionError(
            f"Could not find tokens_per_second lines in stdout for "
            f"{svh_path.name}:\n{result.stdout[-2000:]}"
        )
    clk_freq_mhz = float(sc_match.group(1))
    singlecore = float(sc_match.group(2))
    multicore = float(mc_match.group(2))
    return singlecore, multicore, clk_freq_mhz


@pytest.mark.slow
@pytest.mark.parametrize("config_name", CONFIG_FILES)
def test_parity_against_original_script(config_name: str) -> None:
    """``compute_tokens_per_sec`` must match the original script's output
    for the same config file."""
    from architecture_visualizer.lib.throughput import compute_tokens_per_sec

    svh_path = _config_path(config_name)
    assert svh_path.exists(), f"Missing test config: {svh_path}"

    # Ground truth from subprocess
    expected_sc, expected_mc, expected_freq = _run_original_script(svh_path)

    # Our re-implementation
    config_dict = _build_config_dict_from_svh(svh_path)
    result = compute_tokens_per_sec(config_dict, MODEL)

    got_sc = result["singlecore"]
    got_mc = result["multicore"]
    got_freq = result["clk_freq_mhz"]

    assert got_sc == pytest.approx(expected_sc, rel=REL_TOL), (
        f"singlecore mismatch on {config_name}: "
        f"expected {expected_sc} (from subprocess) "
        f"got {got_sc} (from compute_tokens_per_sec)"
    )
    assert got_mc == pytest.approx(expected_mc, rel=REL_TOL), (
        f"multicore mismatch on {config_name}: "
        f"expected {expected_mc} (from subprocess) "
        f"got {got_mc} (from compute_tokens_per_sec)"
    )
    assert got_freq == pytest.approx(expected_freq, rel=REL_TOL), (
        f"clk_freq_mhz mismatch on {config_name}: "
        f"expected {expected_freq} (from subprocess) "
        f"got {got_freq} (from compute_tokens_per_sec)"
    )


def test_returns_dict_with_expected_keys() -> None:
    """Quick smoke test: returned dict has the contract's keys and values
    are positive floats."""
    from architecture_visualizer.lib.throughput import compute_tokens_per_sec

    svh_path = _config_path("xcu250_D=1024_OneCore.svh")
    config_dict = _build_config_dict_from_svh(svh_path)
    result = compute_tokens_per_sec(config_dict, MODEL)

    assert set(result.keys()) == {"singlecore", "multicore", "clk_freq_mhz"}
    assert result["singlecore"] > 0
    assert result["multicore"] > 0
    assert result["clk_freq_mhz"] > 0
