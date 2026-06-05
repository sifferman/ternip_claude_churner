"""Topology builder — converts (architecture variant, parameters) into
a list of Node + Edge dicts. See lib/api.py for the contract.

OWNED BY: Phase 1 Agent B (Topology + Cells Builder).
"""
from lib.api import ArchVariant, Edge, Node, ParamsDict


def build_topology(
    variant: ArchVariant,
    params: ParamsDict,
) -> tuple[list[Node], list[Edge]]:
    raise NotImplementedError("see PARALLEL_AGENT_PLAN.md §3-B")
