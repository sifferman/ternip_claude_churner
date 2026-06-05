"""architecture_visualizer — Dash app shell.

Phase 1 Agent D deliverable. The graph is currently populated by a hand-
crafted `mock_topology()` so the GUI is visible before Agent B's
`lib/topology.py` is integrated. Phase 2 integration swaps the import.

Launch locally:
    python app.py

From a laptop forwarded to a remote workstation:
    ssh -L 8050:localhost:8050 <user>@<server>
    # then open http://localhost:8050 in the laptop browser

The server binds to 127.0.0.1:8050 (Dash default) — not exposed externally.
"""
from __future__ import annotations

import math
from typing import Any

import dash
import dash_cytoscape as cyto
from dash import Input, Output, State, dcc, html, no_update

from lib.style import DEFAULT_NODE_COLORS, generate_stylesheet
from lib.topology import build_topology
from lib.throughput import compute_tokens_per_sec

# Enable fcose / dagre / cola / etc. extra layouts (per Agent A's research)
cyto.load_extra_layouts()


# ---------------------------------------------------------------------------
# Defaults sourced from config/xcu250_D=1024_MaxCores.svh
# ---------------------------------------------------------------------------

ARCH_VARIANTS = (
    "NumSeparateAxiInstances",
    "NumDdrBanksPerTmatmul",
    "NumTmatmulBanksPerCore",
)
DEFAULT_VARIANT = "NumTmatmulBanksPerCore"

DEFAULT_PARAMS: dict[str, int] = {
    "TmatmulParallelism": 128,
    "VectorParallelism":  4,
    "BatchSize":          1,
    "NumDdrBanksUsed":    4,
    "D":                  1024,
    "FixedPointPrecision": 16,
    "NumVectorRegisters":  4,
    "DdrDataWidth":        512,
    "InstructionWidth":    128,
    "DramNumBanks":        4,
}

# Power-of-2 marks for the parallelism sliders.
TP_VALUES = [16, 32, 64, 128, 256, 512]
VP_VALUES = [1, 2, 4, 8, 16]

# Layouts the user can switch between (default = Agent A's fcose recommendation).
DEFAULT_LAYOUT_NAME = "fcose"


# ---------------------------------------------------------------------------
# Mock topology — replaced in Phase 2 with lib.topology.build_topology
# ---------------------------------------------------------------------------

def mock_topology(variant: str, params: dict[str, int]) -> tuple[list[dict], list[dict]]:
    """Return a hand-crafted (nodes, edges) tuple for GUI bring-up.

    Mimics the Node/Edge TypedDicts in lib/api.py, including a `cell_count`
    that scales with the slider parameters so the visualization animates.
    Topology shape is approximate; precise modeling lives in Agent B's
    lib/topology.py.
    """
    TP = params["TmatmulParallelism"]
    VP = params["VectorParallelism"]
    BS = params["BatchSize"]
    N  = params["NumDdrBanksUsed"]
    FxP = params["FixedPointPrecision"]
    D = params["D"]
    DDR = params["DdrDataWidth"]
    IW = params["InstructionWidth"]

    nodes: list[dict] = []
    edges: list[dict] = []

    # DRAM banks pinned across the top row
    dram_y = -400
    bank_xs = [-450, -150, 150, 450]
    for b in range(N):
        nodes.append({
            "id": f"dram_{b}",
            "label": f"DRAM[{b}]",
            "type": "DRAM",
            "bank": b,
            "core": None,
            "cell_count": 50_000,
            "slr": b % 4,
            "pinned": True,
            "x": bank_xs[b % 4],
            "y": dram_y,
        })

    # Shared infrastructure
    nodes.append({
        "id": "axi_dma_instr",
        "label": "axi_dma_instr",
        "type": "axi_dma_instr",
        "bank": None,
        "core": None,
        "cell_count": 6_000,
        "slr": None,
    })
    nodes.append({
        "id": "instruction_decode",
        "label": "instruction_decode",
        "type": "instruction_decode",
        "bank": None,
        "core": None,
        "cell_count": 8_000,
        "slr": None,
    })
    edges.append({
        "source": "axi_dma_instr",
        "target": "instruction_decode",
        "bus_bits": IW,
        "formula": "InstructionWidth",
    })

    # Per-bank tmatmul_dma
    for b in range(N):
        nodes.append({
            "id": f"tmatmul_dma_{b}",
            "label": f"tmatmul_dma[{b}]",
            "type": "tmatmul_dma",
            "bank": b,
            "core": None,
            "cell_count": 5_000 + 400 * TP // 32,
            "slr": None,
        })
        edges.append({
            "source": f"dram_{b}",
            "target": f"tmatmul_dma_{b}",
            "bus_bits": DDR,
            "formula": "DdrDataWidth",
        })

    # Per-core sub-graph (mock — for visibility scale this with BatchSize)
    for c in range(BS):
        # Compound parent so the cytoscape `:parent` selector renders
        core_id = f"core_{c}"
        nodes.append({
            "id": core_id,
            "label": f"core[{c}]",
            "type": "ternip_core",
            "bank": None,
            "core": c,
            "cell_count": 30_000,
            "slr": None,
            "parent": None,
        })

        # Inside-core fixed nodes
        for ntype, base_cells in (
            ("RMS",              4_500),
            ("loadstore",        3_000),
            ("rowwise_op",       2_500),
            ("vector_registers", 6_000),
            ("MOA",              2_000 + TP * 40),
            ("importvector",    1_800 + TP * 20),
            ("exportvector",    1_600 + TP * 18),
            ("tmatmul_unit",    3_500 + TP * 60),
        ):
            nid = f"{ntype}_{c}"
            nodes.append({
                "id": nid,
                "label": f"{ntype}[{c}]" if BS > 1 else ntype,
                "type": ntype,
                "bank": None,
                "core": c,
                "cell_count": base_cells,
                "slr": None,
                "parent": core_id,
            })

        # Per-core internal edges
        for src, dst, bw_formula, bw in (
            (f"importvector_{c}", f"tmatmul_unit_{c}", "VP*FxP*4", VP * FxP * 4),
            (f"tmatmul_unit_{c}", f"MOA_{c}",          "TP*FxP",   TP * FxP),
            (f"MOA_{c}",          f"exportvector_{c}", "FxP",      FxP),
            (f"vector_registers_{c}", f"importvector_{c}", "VP*FxP*4", VP * FxP * 4),
            (f"vector_registers_{c}", f"RMS_{c}",          "VP*FxP",   VP * FxP),
            (f"RMS_{c}",          f"vector_registers_{c}", "VP*FxP",   VP * FxP),
            (f"vector_registers_{c}", f"rowwise_op_{c}",   "VP*FxP",   VP * FxP),
            (f"rowwise_op_{c}",       f"vector_registers_{c}", "VP*FxP", VP * FxP),
            (f"loadstore_{c}",        f"vector_registers_{c}", "VP*FxP*2", VP * FxP * 2),
        ):
            edges.append({
                "source": src, "target": dst, "bus_bits": bw, "formula": bw_formula,
            })

        # Hook each tmatmul_dma into this core's importvector / tmatmul_unit
        for b in range(N):
            edges.append({
                "source": f"tmatmul_dma_{b}",
                "target": f"tmatmul_unit_{c}",
                "bus_bits": TP * 2,
                "formula": "TmatmulParallelism * 2",
            })

        # loadstore <-> any DRAM bank (mock — just bank 0 for visibility)
        edges.append({
            "source": f"loadstore_{c}",
            "target": "dram_0",
            "bus_bits": DDR,
            "formula": "DdrDataWidth",
        })

        # instruction_decode broadcasts to core
        edges.append({
            "source": "instruction_decode",
            "target": f"vector_registers_{c}",
            "bus_bits": IW,
            "formula": "InstructionWidth",
        })

    return nodes, edges


# ---------------------------------------------------------------------------
# Helpers — turn (nodes, edges) into cytoscape element dicts
# ---------------------------------------------------------------------------

def _radius_px(cell_count: int) -> float:
    """sqrt-scale a cell_count into a pixel radius. Clamp to [20, 200]."""
    if cell_count <= 0:
        return 20.0
    return max(20.0, min(200.0, math.sqrt(cell_count) * 0.9))


def _edge_width_px(bus_bits: int) -> float:
    """Linear-scale bus_bits to pixel width with a 1px floor and 14px ceiling."""
    if bus_bits <= 0:
        return 1.0
    return max(1.0, min(14.0, bus_bits / 64.0))


# ---------------------------------------------------------------------------
# U250 layout: 4 stacked SLR bands + DRAM banks pinned per SLR + XRT below
# ---------------------------------------------------------------------------

# Vertical SLR positions (cytoscape Y axis: smaller Y = higher on canvas).
# AU250 physical: SLR0 bottom, SLR3 top. Render SLR3 at top of canvas.
SLR_Y = {3: -600, 2: -200, 1: 200, 0: 600}
# DRAM bank X (left edge) — gives the U250 a "tall rectangle" shape.
DRAM_X = -400
# XRT shell at the very bottom — represents PCIe / host edge.
XRT_X, XRT_Y = 0, 1000


def _slr_parent_id(slr_idx: int) -> str:
    return f"slr_{slr_idx}"


def _assign_node_to_slr(n: dict, num_banks: int) -> int:
    """Heuristic SLR assignment for layout purposes:
       - DRAM[b] / xrt_shell: explicit slr field
       - tmatmul_dma[b] / tmatmul_buffers[b]: SLR b (mirrors our pblock work)
       - Anything else with bank set: SLR(bank)
       - Anything else: SLR1 (kernel-center default)
    """
    if n.get("type") == "xrt_shell":
        return 0  # placed BELOW SLR0; uses no parent in fact
    if n.get("slr") is not None:
        return int(n["slr"])
    b = n.get("bank")
    if b is not None and n["type"] in ("tmatmul_dma", "tmatmul_buffers"):
        return int(b) % max(num_banks, 1)
    return 1  # kernel center


def decorate_topology_with_u250_layout(
    nodes: list[dict],
    num_banks: int,
) -> list[dict]:
    """Enrich nodes with `parent` (SLR compound) and pinned positions for
    DRAM banks + xrt_shell. Returns the augmented node list (originals +
    4 SLR parent dummies). The SLR parents render as compound bands.
    """
    out: list[dict] = []

    # 4 SLR compound parent dummies (no cell_count, no bank/core).
    for slr_idx in (3, 2, 1, 0):
        out.append({
            "id": _slr_parent_id(slr_idx),
            "label": f"SLR{slr_idx}",
            "type": "slr_parent",
            "bank": None,
            "core": None,
            "cell_count": 0,
            "slr": slr_idx,
        })

    for n in nodes:
        nn = dict(n)
        nt = n.get("type")
        if nt == "xrt_shell":
            # Pinned BELOW SLR0, no compound parent.
            nn["pinned"] = True
            nn["x"] = XRT_X
            nn["y"] = XRT_Y
        elif nt == "DRAM":
            b = int(n.get("bank", 0))
            slr = b  # AU250: DDR[b] -> SLR[b]
            nn["parent"] = _slr_parent_id(slr)
            nn["pinned"] = True
            nn["x"] = DRAM_X
            nn["y"] = SLR_Y.get(slr, 0)
        else:
            slr = _assign_node_to_slr(n, num_banks)
            nn["parent"] = _slr_parent_id(slr)
        out.append(nn)

    return out


def topology_to_cyto_elements(nodes: list[dict], edges: list[dict]) -> list[dict]:
    """Convert the (Node, Edge) lists into a cytoscape elements list."""
    elements: list[dict] = []

    for n in nodes:
        cyto_node: dict[str, Any] = {
            "data": {
                "id": n["id"],
                "label": n.get("label", n["id"]),
                "type": n["type"],
                "cell_count": int(n.get("cell_count", 0)),
                "radius_px": _radius_px(int(n.get("cell_count", 0))),
                "bank": n.get("bank"),
                "core": n.get("core"),
                "slr": n.get("slr"),
                "formula": n.get("formula", ""),
            },
        }
        # Compound parent support — Cytoscape uses `data.parent`
        parent = n.get("parent")
        if parent:
            cyto_node["data"]["parent"] = parent

        # Pin DRAM banks (or any node with explicit x/y + pinned=True)
        if n.get("pinned"):
            cyto_node["position"] = {
                "x": float(n.get("x", 0)),
                "y": float(n.get("y", 0)),
            }
            cyto_node["locked"] = True
            cyto_node["grabbable"] = False
        elif "x" in n and "y" in n:
            cyto_node["position"] = {"x": float(n["x"]), "y": float(n["y"])}

        elements.append(cyto_node)

    for e in edges:
        elements.append({
            "data": {
                "source": e["source"],
                "target": e["target"],
                "bus_bits": int(e.get("bus_bits", 1)),
                "edge_width": _edge_width_px(int(e.get("bus_bits", 1))),
                "formula": e.get("formula", ""),
            },
        })

    return elements


def make_layout_config(layout_name: str, nodes: list[dict]) -> dict:
    """Return a cytoscape layout config dict.

    For fcose, supply a fixedNodeConstraint listing all pinned (DRAM) nodes
    so they snap to the top row across re-layouts.
    """
    if layout_name == "fcose":
        pinned = [
            {"nodeId": n["id"], "position": {"x": float(n.get("x", 0)),
                                              "y": float(n.get("y", -400))}}
            for n in nodes if n.get("pinned")
        ]
        cfg: dict[str, Any] = {
            "name": "fcose",
            "randomize": False,
            "quality": "proof",
            "animate": False,
            "fit": True,
            "padding": 30,
            "nodeRepulsion": 8000,
            "idealEdgeLength": 80,
        }
        if pinned:
            cfg["fixedNodeConstraint"] = pinned
            ids = [p["nodeId"] for p in pinned]
            cfg["alignmentConstraint"] = {"horizontal": [ids]}
        return cfg
    if layout_name == "dagre":
        return {"name": "dagre", "rankDir": "TB", "padding": 30, "animate": False}
    if layout_name == "cose":
        return {"name": "cose", "animate": False, "padding": 30}
    return {"name": layout_name, "animate": False, "padding": 30}


# ---------------------------------------------------------------------------
# Build the right-sidebar checklist from a nodes list
# ---------------------------------------------------------------------------

def build_sidebar_groups(nodes: list[dict]) -> list[Any]:
    """Return the right-sidebar's hierarchical checklist as a Dash component
    tree. Returns a list of html.Div sections; each holds a header + checklist.
    """
    dram = [n for n in nodes if n["type"] == "DRAM"]
    shared = [n for n in nodes
              if n["type"] in ("axi_dma_instr", "instruction_decode")]
    # Group remaining by core
    cores: dict[int, list[dict]] = {}
    for n in nodes:
        c = n.get("core")
        if c is None or n["type"] in ("DRAM", "axi_dma_instr",
                                      "instruction_decode", "tmatmul_dma"):
            continue
        cores.setdefault(int(c), []).append(n)

    tmatmul_dmas = [n for n in nodes if n["type"] == "tmatmul_dma"]

    sections: list[Any] = []

    def _section(title: str, items: list[dict]):
        if not items:
            return None
        opts = [{"label": n.get("label", n["id"]), "value": n["id"]}
                for n in items]
        return html.Div([
            html.Div(title, style={
                "fontWeight": "bold", "marginTop": "8px",
                "borderBottom": "1px solid #ccc", "paddingBottom": "2px",
            }),
            dcc.Checklist(
                id={"role": "node-highlight", "group": title},
                options=opts, value=[],
                style={"marginLeft": "8px", "fontSize": "12px"},
            ),
        ])

    s = _section("DRAM banks", dram)
    if s is not None:
        sections.append(s)
    s = _section("Shared infrastructure", shared)
    if s is not None:
        sections.append(s)
    s = _section("tmatmul DMA (per bank)", tmatmul_dmas)
    if s is not None:
        sections.append(s)
    for cid in sorted(cores):
        s = _section(f"Core {cid}", cores[cid])
        if s is not None:
            sections.append(s)

    return sections


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

app = dash.Dash(__name__, suppress_callback_exceptions=True)
app.title = "Ternip architecture visualizer"

# Initial graph — try the real topology first, fall back to mock on failure.
try:
    _initial_nodes, _initial_edges = build_topology(DEFAULT_VARIANT, DEFAULT_PARAMS)
except Exception as _e:
    print(f"[init] build_topology failed ({_e}); falling back to mock")
    _initial_nodes, _initial_edges = mock_topology(DEFAULT_VARIANT, DEFAULT_PARAMS)
_initial_nodes_decorated = decorate_topology_with_u250_layout(
    _initial_nodes, int(DEFAULT_PARAMS["NumDdrBanksUsed"])
)
_initial_elements = topology_to_cyto_elements(_initial_nodes_decorated, _initial_edges)

# Top bar
top_bar = html.Div([
    html.H3("Ternip architecture visualizer",
            style={"display": "inline-block", "marginRight": "24px"}),
    html.Label("Architecture variant:", style={"marginRight": "8px"}),
    dcc.Dropdown(
        id="variant-dropdown",
        options=[{"label": v, "value": v} for v in ARCH_VARIANTS],
        value=DEFAULT_VARIANT,
        clearable=False,
        style={"display": "inline-block", "width": "320px",
               "verticalAlign": "middle"},
    ),
    html.Label("Layout:", style={"marginLeft": "16px", "marginRight": "8px"}),
    dcc.Dropdown(
        id="layout-dropdown",
        options=[{"label": n, "value": n}
                 for n in ("fcose", "dagre", "cose", "cose-bilkent", "cola",
                           "concentric", "circle", "grid")],
        value=DEFAULT_LAYOUT_NAME,
        clearable=False,
        style={"display": "inline-block", "width": "180px",
               "verticalAlign": "middle"},
    ),
], style={
    "padding": "10px 16px",
    "borderBottom": "1px solid #ccc",
    "background": "#f7f7f7",
})


# Left panel sliders
def _slider(label: str, slider_id: str, values: list[int], default: int):
    return html.Div([
        html.Label(label, style={"fontWeight": "bold"}),
        dcc.Slider(
            id=slider_id,
            min=0,
            max=len(values) - 1,
            step=None,
            marks={i: str(v) for i, v in enumerate(values)},
            value=values.index(default),
        ),
    ], style={"marginBottom": "18px"})


left_panel = html.Div([
    html.Div("Parameters", style={"fontWeight": "bold", "marginBottom": "8px"}),
    _slider("TmatmulParallelism", "slider-tp", TP_VALUES,
            DEFAULT_PARAMS["TmatmulParallelism"]),
    _slider("VectorParallelism", "slider-vp", VP_VALUES,
            DEFAULT_PARAMS["VectorParallelism"]),
    html.Div([
        html.Label("BatchSize", style={"fontWeight": "bold"}),
        dcc.Slider(
            id="slider-bs", min=1, max=20, step=1,
            marks={i: str(i) for i in (1, 5, 10, 15, 20)},
            value=DEFAULT_PARAMS["BatchSize"],
            tooltip={"placement": "bottom", "always_visible": False},
        ),
    ], style={"marginBottom": "18px"}),
    html.Div([
        html.Label("NumDdrBanksUsed", style={"fontWeight": "bold"}),
        dcc.Slider(
            id="slider-banks", min=1, max=4, step=1,
            marks={1: "1", 2: "2", 3: "3", 4: "4"},
            value=DEFAULT_PARAMS["NumDdrBanksUsed"],
        ),
    ], style={"marginBottom": "18px"}),
    html.Hr(),
    html.Button("Compute tokens/sec", id="btn-tokens",
                n_clicks=0,
                style={"width": "100%", "padding": "8px",
                       "background": "#1f6f1f", "color": "white",
                       "border": "none", "cursor": "pointer",
                       "borderRadius": "4px"}),
    html.Div(id="tokens-readout", children="Click button to estimate",
             style={"marginTop": "12px", "padding": "8px",
                    "background": "#f0f0f0", "borderRadius": "4px",
                    "fontFamily": "monospace", "fontSize": "12px",
                    "whiteSpace": "pre-wrap"}),
], style={
    "width": "260px",
    "padding": "12px",
    "borderRight": "1px solid #ccc",
    "background": "#fafafa",
    "overflowY": "auto",
})


# Right sidebar (built from current nodes)
right_sidebar = html.Div([
    html.Div("Nodes", style={"fontWeight": "bold", "marginBottom": "8px"}),
    html.Div(id="sidebar-checklist",
             children=build_sidebar_groups(_initial_nodes_decorated)),
], style={
    "width": "240px",
    "padding": "12px",
    "borderLeft": "1px solid #ccc",
    "background": "#fafafa",
    "overflowY": "auto",
})


# Center pane
cyto_pane = html.Div([
    cyto.Cytoscape(
        id="cyto-graph",
        elements=_initial_elements,
        layout=make_layout_config(DEFAULT_LAYOUT_NAME, _initial_nodes_decorated),
        stylesheet=generate_stylesheet(DEFAULT_NODE_COLORS, set()),
        style={"width": "100%", "height": "100%"},
        minZoom=0.1,
        maxZoom=4.0,
        boxSelectionEnabled=False,
        responsive=True,
    ),
], style={"flex": "1 1 auto", "position": "relative",
          "background": "#ffffff", "minWidth": "0"})


# Bottom strip — wire-length readout
bottom_strip = html.Div([
    html.Span(id="wire-length-readout",
              children="Total wire length: (drag a node to compute)"),
], style={
    "padding": "8px 16px",
    "borderTop": "1px solid #ccc",
    "background": "#f7f7f7",
    "fontFamily": "monospace",
    "fontSize": "13px",
})


# Hidden stores
stores = html.Div([
    dcc.Store(id="store-nodes", data=_initial_nodes_decorated),
    dcc.Store(id="store-edges", data=_initial_edges),
    dcc.Store(id="store-highlight", data=[]),
])


app.layout = html.Div([
    top_bar,
    html.Div([
        left_panel,
        cyto_pane,
        right_sidebar,
    ], style={"display": "flex",
              "flex": "1 1 auto",
              "height": "calc(100vh - 110px)",
              "minHeight": 0}),
    bottom_strip,
    stores,
], style={"display": "flex", "flexDirection": "column",
          "height": "100vh", "margin": 0,
          "fontFamily": "Helvetica, Arial, sans-serif"})


# ---------------------------------------------------------------------------
# Callbacks
# ---------------------------------------------------------------------------

@app.callback(
    Output("cyto-graph", "elements"),
    Output("cyto-graph", "layout"),
    Output("store-nodes", "data"),
    Output("store-edges", "data"),
    Output("sidebar-checklist", "children"),
    Input("variant-dropdown", "value"),
    Input("slider-tp", "value"),
    Input("slider-vp", "value"),
    Input("slider-bs", "value"),
    Input("slider-banks", "value"),
    Input("layout-dropdown", "value"),
)
def rebuild_graph(variant, tp_idx, vp_idx, bs, banks, layout_name):
    params = dict(DEFAULT_PARAMS)
    params.update({
        "TmatmulParallelism": TP_VALUES[tp_idx],
        "VectorParallelism":  VP_VALUES[vp_idx],
        "BatchSize":          int(bs),
        "NumDdrBanksUsed":    int(banks),
    })
    try:
        nodes, edges = build_topology(variant, params)
    except ValueError as e:
        # e.g. column-slice with D % NumDdrBanksUsed != 0
        # Fall back to mock so the GUI doesn't crash; surface the error in
        # the wire-length strip via an empty graph + a labeled placeholder.
        print(f"[topology] {e} — falling back to mock graph")
        nodes, edges = mock_topology(variant, params)
    nodes = decorate_topology_with_u250_layout(nodes, int(params["NumDdrBanksUsed"]))
    elements = topology_to_cyto_elements(nodes, edges)
    layout = make_layout_config(layout_name, nodes)
    sidebar = build_sidebar_groups(nodes)
    return elements, layout, nodes, edges, sidebar


@app.callback(
    Output("cyto-graph", "stylesheet"),
    Output("store-highlight", "data"),
    Input({"role": "node-highlight", "group": dash.ALL}, "value"),
)
def update_highlight(group_values):
    """Aggregate every group's checklist values into one highlight set."""
    highlight: set[str] = set()
    if group_values:
        for vals in group_values:
            if vals:
                highlight.update(vals)
    sheet = generate_stylesheet(DEFAULT_NODE_COLORS, highlight)
    return sheet, sorted(highlight)


@app.callback(
    Output("tokens-readout", "children"),
    Input("btn-tokens", "n_clicks"),
    State("variant-dropdown", "value"),
    State("slider-tp", "value"),
    State("slider-vp", "value"),
    State("slider-bs", "value"),
    State("slider-banks", "value"),
    prevent_initial_call=True,
)
def compute_tokens(n_clicks, variant, tp_idx, vp_idx, bs, banks):
    if not n_clicks:
        return no_update
    TP = TP_VALUES[tp_idx]
    VP = VP_VALUES[vp_idx]
    BS = int(bs)
    N  = int(banks)

    # Build the full config dict expected by lib.throughput.
    cfg = dict(DEFAULT_PARAMS)
    cfg.update({
        "TmatmulParallelism": TP,
        "VectorParallelism":  VP,
        "BatchSize":          BS,
        "NumDdrBanksUsed":    N,
        # Map variant-specific N alias for the throughput math:
        "NumTmatmulBanksPerCore": N if variant == "NumTmatmulBanksPerCore" else 1,
        # Defaults the throughput math expects:
        "DramMaxBytesPerSecond": 8 * 2400.0 * 10**6,
        "ClockPeriod": 3.333e-9,
    })

    try:
        result = compute_tokens_per_sec(cfg, "MMfreeLM-370M")
    except Exception as e:
        return f"compute_tokens_per_sec failed: {type(e).__name__}: {e}"

    sc = result["singlecore"]
    mc = result["multicore"]
    mhz = result.get("clk_freq_mhz", 300.03)
    return (
        f"variant         : {variant}\n"
        f"TP / VP / BS / N: {TP} / {VP} / {BS} / {N}\n"
        f"singlecore      : {sc:>12,.2f} tok/s @ {mhz:.1f} MHz\n"
        f"multicore       : {mc:>12,.2f} tok/s @ {mhz:.1f} MHz"
    )


@app.callback(
    Output("wire-length-readout", "children"),
    Input("cyto-graph", "tapNodeData"),
    Input("cyto-graph", "elements"),
)
def update_wirelength(_tap, elements):
    """Recompute Σ bus_bits × edge_pixel_length whenever a node is tapped or
    the graph rebuilds. Reads `position` from each cytoscape element; the
    user dragging a node triggers a tapNodeData event with up-to-date
    positions.
    """
    if not elements:
        return "Total wire length: (no graph)"

    positions: dict[str, tuple[float, float]] = {}
    for e in elements:
        if "position" in e and "data" in e:
            pos = e["position"]
            positions[e["data"]["id"]] = (float(pos.get("x", 0.0)),
                                          float(pos.get("y", 0.0)))

    total = 0.0
    n_edges = 0
    for e in elements:
        d = e.get("data", {})
        if "source" not in d or "target" not in d:
            continue
        n_edges += 1
        s = positions.get(d["source"])
        t = positions.get(d["target"])
        if s is None or t is None:
            continue
        dx = s[0] - t[0]
        dy = s[1] - t[1]
        dist = math.hypot(dx, dy)
        total += float(d.get("bus_bits", 0)) * dist

    if total <= 0:
        return (f"Total wire length: 0 bit-pixels ({n_edges} edges; "
                "node positions still resolving — drag any node to refresh)")
    return f"Total wire length: {total:,.0f} bit-pixels ({n_edges} edges)"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Bind to localhost only (matches PLAN.md's remote-access guidance).
    app.run(host="127.0.0.1", port=8050, debug=False)
