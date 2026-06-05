"""Per-node cell-count formulas. See lib/api.py for the contract.

OWNED BY: Phase 1 Agent B (Topology + Cells Builder).
"""
from lib.api import CellEstimate, ParamsDict


def estimate_cells(
    node_type: str,
    params: ParamsDict,
    bs_factor: int = 1,
) -> CellEstimate:
    raise NotImplementedError("see PARALLEL_AGENT_PLAN.md §3-B")
