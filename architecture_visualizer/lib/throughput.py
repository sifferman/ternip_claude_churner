"""Throughput math — re-implementation of report_instruction_timing.py.

See ``lib/api.py`` for the contract.

Pure in-process Python — no subprocess. The function is driven from a
dict of parameter values (as produced by the visualizer's sliders) so
the caller does not need a ``.svh`` file on disk.

Implementation notes
--------------------
* The ``report_instruction_timing.py`` script builds an
  ``AlgorithmTree`` for the model, schedules it via
  ``create_instruction_ordering`` /  ``create_register_mapping`` /
  ``prune_unnecessary_swaps`` / ``create_assembly_tokens``, and then
  runs the per-operation timing accumulator inside
  ``AlgorithmTree.report_timing``.  We re-use the
  ``ternary_matmul/sw_utils`` library for the algorithm build + the
  schedule — re-porting those thousands of lines would be brittle —
  but we re-implement the final ``report_timing`` accumulator inline
  (without ``print`` statements) so we can return numerical values.
* The original ``report_timing`` prints ``singlecore`` /
  ``multicore`` lines; the math in those two lines is
  ``clk_freq / cycle_counter`` and
  ``NumTmatmulBanksPerCore * BatchSize * singlecore`` respectively.

OWNED BY: Phase 1 Agent C (Throughput + Config Refactor).
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path

from .api import ThroughputResult


# ---------------------------------------------------------------------------
# sw_utils path setup
# ---------------------------------------------------------------------------
# The sw_utils library modules import each other as ``from lib.X import Y``
# (no ``sw_utils`` package prefix), so we have to insert
# ``ternary_matmul/sw_utils`` onto ``sys.path`` before importing them.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SW_UTILS_DIR = _REPO_ROOT / "ternary_matmul" / "sw_utils"
if str(_SW_UTILS_DIR) not in sys.path:
    sys.path.insert(0, str(_SW_UTILS_DIR))


def _operation_duration(tree, operation, parallel=False):
    """Delegate to AlgorithmTree.operation_duration; kept as a thin wrapper
    so the report-timing port below reads close to the original."""
    return tree.operation_duration(operation, parallel=parallel)


def _report_timing_numerical(tree, assembly_tokens):
    """Port of ``AlgorithmTree.report_timing`` without the ``print`` calls.

    Returns ``(singlecore, multicore, clk_freq_mhz)`` floats. Mirrors the
    cycle-accumulator logic in
    ``ternary_matmul/sw_utils/lib/algorithm_tree.py:report_timing``.
    """
    cycle_counter = 0
    tmatmul_go_cycle_counter = 0
    tmatmul_export_stalled_cycle_counter = 0
    time_since_last_tmatmul_go = float("inf")

    for instruction_tokens in assembly_tokens:
        operation_name = instruction_tokens[0]
        operation_delay = _operation_duration(tree, operation_name, parallel=True)

        if operation_name == "tmatmul_export":
            time_remaining_on_tmatmul_go = (
                _operation_duration(tree, "tmatmul_go", parallel=False)
                - time_since_last_tmatmul_go
            )
            if time_remaining_on_tmatmul_go > 0:
                tmatmul_export_stalled_cycle_counter += time_remaining_on_tmatmul_go
                operation_delay += time_remaining_on_tmatmul_go
        elif operation_name == "tmatmul_go":
            tmatmul_go_cycle_counter += _operation_duration(
                tree, "tmatmul_go", parallel=False
            )
            time_since_last_tmatmul_go = 0

        cycle_counter += operation_delay
        time_since_last_tmatmul_go += operation_delay

    clk_freq = 1.0 / tree.config.ClockPeriod
    singlecore = clk_freq / cycle_counter
    multicore = (
        tree.config.NumTmatmulBanksPerCore * tree.config.BatchSize * singlecore
    )
    clk_freq_mhz = clk_freq / 10**6
    return float(singlecore), float(multicore), float(clk_freq_mhz)


def compute_tokens_per_sec(
    config: dict,
    model: str = "MMfreeLM-370M",
) -> ThroughputResult:
    """Estimate tokens/sec for the given config + model.

    ``config`` is the dict form accepted by ``Config.from_dict`` —
    a superset of the visualizer's ``ParamsDict``. Required keys are
    documented on ``Config.from_dict``.

    Returns a dict with keys ``singlecore``, ``multicore``, and
    ``clk_freq_mhz`` (per the API contract).
    """
    # Imports are deferred so the sys.path manipulation at module top
    # has had a chance to take effect.
    from lib.config import Config  # type: ignore[import-not-found]
    from lib.huggingface import HuggingFace  # type: ignore[import-not-found]
    from lib.matmulfree_algorithm_tree import (  # type: ignore[import-not-found]
        matmulfree_algorithm_tree,
    )

    cfg = Config.from_dict(config)

    # Use the same on-disk huggingface_cache dir the original script uses
    # (relative to ternary_matmul/sw_utils/target). The HuggingFace class
    # downloads on demand if cache is missing.
    cache_dir = str(_SW_UTILS_DIR / "target" / "huggingface_cache")
    # Run cwd-independent: HuggingFace resolves the cache relative to cwd
    # unless given an absolute path — pass absolute.
    hf = HuggingFace(model, huggingface_dir=cache_dir)

    # Silence the unconditional "Scheduled N / M" prints inside
    # AlgorithmTree.create_instruction_ordering — they're useful for the
    # CLI script but noise from a Dash callback.
    with contextlib.redirect_stdout(io.StringIO()):
        tree = matmulfree_algorithm_tree(cfg, hf, debug=True)
        ordering = tree.create_instruction_ordering()
        register_mapping = tree.create_register_mapping(ordering)
        ordering, register_mapping = tree.prune_unnecessary_swaps(
            ordering, register_mapping
        )
        assembly_tokens = tree.create_assembly_tokens(ordering, register_mapping)

    singlecore, multicore, clk_freq_mhz = _report_timing_numerical(
        tree, assembly_tokens
    )
    return {
        "singlecore": singlecore,
        "multicore": multicore,
        "clk_freq_mhz": clk_freq_mhz,
    }
