"""Cytoscape stylesheet generator. See lib/api.py for the contract.

OWNED BY: Phase 1 Agent D (GUI Shell).
"""
from __future__ import annotations

from typing import Optional


# ---------------------------------------------------------------------------
# Default per-node-type color palette
# ---------------------------------------------------------------------------

DEFAULT_NODE_COLORS: dict[str, str] = {
    "DRAM":               "#1f77b4",  # blue
    "tmatmul_dma":        "#ff7f0e",  # orange
    "MOA":                "#d62728",  # red
    "importvector":       "#f7e836",  # yellow
    "exportvector":       "#f7d300",  # yellow (slightly different)
    "tmatmul_unit":       "#cc5500",  # dark orange
    "ternip_core":        "#2ca02c",  # green
    "RMS":                "#9467bd",  # purple
    "loadstore":          "#17becf",  # cyan
    "rowwise_op":         "#e377c2",  # pink
    "vector_registers":   "#8c564b",  # brown
    "instruction_decode": "#7f7f7f",  # gray
    "axi_dma_instr":      "#c7c7c7",  # light gray
    "xrt_shell":          "#999999",  # medium gray (platform/static region)
}

# Highlight override
HIGHLIGHT_BORDER_COLOR = "#ff00ff"  # bright magenta
HIGHLIGHT_BORDER_WIDTH = 4


def generate_stylesheet(
    nodes_by_type_colors: Optional[dict[str, str]] = None,
    highlight_set: Optional[set[str]] = None,
) -> list[dict]:
    """Return cytoscape stylesheet list per the api.py contract.

    - node radius = sqrt-scaled by cell_count
    - node fill from nodes_by_type_colors[node['type']]
    - highlighted nodes get a bright magenta border
    - edge width = linear in bus_bits (with a 1px floor)
    """
    colors = dict(DEFAULT_NODE_COLORS)
    if nodes_by_type_colors:
        colors.update(nodes_by_type_colors)
    highlight = highlight_set or set()

    stylesheet: list[dict] = [
        # ------------------------------------------------------------------
        # Base node
        # ------------------------------------------------------------------
        {
            "selector": "node",
            "style": {
                "label": "data(label)",
                "font-size": "10px",
                "color": "#222",
                "text-valign": "center",
                "text-halign": "center",
                "text-wrap": "wrap",
                "text-max-width": "80px",
                "text-outline-color": "#fff",
                "text-outline-width": 2,
                # sqrt scaling via mapData: domain [0, 100000], range [20, 200]
                # cytoscape mapData is linear; we approximate sqrt by clamping
                # the upper bound and using the sqrt of cell_count as input
                # (encoded into node data as `radius_sqrt`).
                "width": "data(radius_px)",
                "height": "data(radius_px)",
                "background-color": "#bbbbbb",
                "border-width": 1,
                "border-color": "#444",
                "shape": "ellipse",
            },
        },
    ]

    # Per-type color rules
    for ntype, color in colors.items():
        stylesheet.append({
            "selector": f'node[type = "{ntype}"]',
            "style": {"background-color": color},
        })

    # DRAM nodes are square so they read as "external memory" at a glance
    stylesheet.append({
        "selector": 'node[type = "DRAM"]',
        "style": {
            "shape": "round-rectangle",
            "border-width": 2,
            "border-color": "#0a3a66",
        },
    })

    # ternip_core compound rendering (if anyone uses parent grouping)
    stylesheet.append({
        "selector": ":parent",
        "style": {
            "background-opacity": 0.10,
            "background-color": colors.get("ternip_core", "#2ca02c"),
            "border-width": 2,
            "border-color": "#1f6f1f",
            "label": "data(label)",
            "text-valign": "top",
            "text-halign": "center",
            "font-size": "12px",
            "padding": "12px",
        },
    })
    # SLR background bands — fixed-size rectangles drawn BEHIND kernel
    # nodes via z-compound-depth. Per-SLR pastel tint so SLR boundaries
    # are easy to see.
    SLR_COLORS = ["#fde0dc", "#ddebf7", "#e2efda", "#fff2cc"]  # 0..3
    stylesheet.append({
        "selector": 'node[type = "slr_band"]',
        "style": {
            "shape": "round-rectangle",
            "width": "data(band_w)",
            "height": "data(band_h)",
            "background-opacity": 0.45,
            "border-width": 2,
            "border-color": "#666",
            "border-style": "dashed",
            "label": "data(label)",
            "text-valign": "top",
            "text-halign": "left",
            "text-margin-y": 16,
            "text-margin-x": 24,
            "font-size": "20px",
            "font-weight": "bold",
            "color": "#444",
            "z-compound-depth": "bottom",
            "z-index": -1,
        },
    })
    for slr_idx, slr_color in enumerate(SLR_COLORS):
        stylesheet.append({
            "selector": f'node[id = "slr_band_{slr_idx}"]',
            "style": {
                "background-color": slr_color,
            },
        })

    # ------------------------------------------------------------------
    # Highlighted nodes — magenta border override
    # ------------------------------------------------------------------
    # We emit one selector per highlighted id rather than using a data
    # attribute, so the caller doesn't need to mutate node['data'].
    for nid in highlight:
        # escape any double-quotes in node id (unlikely but defensive)
        safe = nid.replace('"', r'\"')
        stylesheet.append({
            "selector": f'node[id = "{safe}"]',
            "style": {
                "border-color": HIGHLIGHT_BORDER_COLOR,
                "border-width": HIGHLIGHT_BORDER_WIDTH,
                "border-style": "solid",
            },
        })

    # Also support an in-data flag if a future caller prefers that path:
    stylesheet.append({
        "selector": "node[?highlighted]",
        "style": {
            "border-color": HIGHLIGHT_BORDER_COLOR,
            "border-width": HIGHLIGHT_BORDER_WIDTH,
            "border-style": "solid",
        },
    })

    # ------------------------------------------------------------------
    # Edges
    # ------------------------------------------------------------------
    stylesheet.append({
        "selector": "edge",
        "style": {
            "curve-style": "bezier",
            "line-color": "#888",
            "target-arrow-color": "#888",
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.7,
            # width = bus_bits / 32, with 1px floor and 12px ceiling
            "width": "data(edge_width)",
            "opacity": 0.7,
            "label": "",
        },
    })
    stylesheet.append({
        "selector": "edge:selected",
        "style": {
            "line-color": HIGHLIGHT_BORDER_COLOR,
            "target-arrow-color": HIGHLIGHT_BORDER_COLOR,
            "width": "data(edge_width)",
        },
    })

    return stylesheet
