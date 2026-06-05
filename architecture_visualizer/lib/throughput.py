"""Throughput math — re-implementation of report_instruction_timing.py.
See lib/api.py for the contract.

OWNED BY: Phase 1 Agent C (Throughput + Config Refactor).
"""
from lib.api import ThroughputResult


def compute_tokens_per_sec(
    config: dict,
    model: str = "MMfreeLM-370M",
) -> ThroughputResult:
    raise NotImplementedError("see PARALLEL_AGENT_PLAN.md §3-C")
