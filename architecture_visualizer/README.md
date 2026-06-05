# architecture_visualizer

Interactive Dash + dash-cytoscape tool for visualizing the Ternip kernel
topology under three architecture variants, with sliders that change node
sizes (cell counts) and edge thicknesses (bus widths), plus an on-demand
tokens/sec estimator.

## Install

```bash
cd architecture_visualizer
pip install -r requirements.txt
```

Requirements (`requirements.txt`):
- `dash >= 2.14`
- `dash-cytoscape >= 0.3.0`
- `pytest >= 7.0` (test-only)

Python >= 3.9 recommended.

## Launch

From the `architecture_visualizer/` directory:

```bash
python app.py
```

The Dash server starts on `http://127.0.0.1:8050`. Open that URL in a
browser on the same machine.

## Remote-server access (SSH port forwarding)

The Dash process binds to `127.0.0.1:8050` and is **not** exposed to the
public network. If `app.py` is running on a remote workstation (eq1/eq2,
etc.) and you want to view it from your laptop:

1. On the remote server, launch the app:
   ```bash
   python app.py
   ```
2. On your **laptop** (not the SSH server), open a tunnel:
   ```bash
   ssh -L 8050:localhost:8050 <user>@<server>
   ```
3. In your laptop's browser, open `http://localhost:8050`.

The tunnel forwards laptop:8050 → server:8050 through SSH; the Dash
process never accepts external connections.

## Usage

- **Top bar** — pick the architecture variant
  (`NumSeparateAxiInstances` / `NumDdrBanksPerTmatmul` /
  `NumTmatmulBanksPerCore` — default `NumTmatmulBanksPerCore`) and the
  cytoscape layout engine (default `fcose`; fallback `dagre` is best for
  dense BS=20 graphs).
- **Left panel** — four sliders:
  - `TmatmulParallelism` (16 / 32 / 64 / 128 / 256 / 512)
  - `VectorParallelism` (1 / 2 / 4 / 8 / 16)
  - `BatchSize` (1..20)
  - `NumDdrBanksUsed` (1 / 2 / 3 / 4)
  - **Compute tokens/sec** button → tokens/sec readout below it.
- **Center pane** — Cytoscape graph. Nodes are color-coded by type and
  sized by `√(cell_count)`. Edges' width is proportional to `bus_bits`.
  DRAM banks are pinned along the top row. Other nodes are draggable.
- **Right sidebar** — hierarchical node checklist (DRAM banks, Shared
  infrastructure, per-core groups). Checked items get a bright magenta
  border to make them stand out.
- **Bottom strip** — `Total wire length` statistic (Σ `bus_bits` × edge
  pixel length). Refreshes whenever a node is tapped or the graph is
  rebuilt.

## Phase status

Phase 1 (this commit): GUI shell with a hand-crafted `mock_topology()`
that yields a representative ~15-25 node graph so the UI is fully
visible. The four parallel agents:

- Agent A — `research/LAYOUT_ENGINE.md` (recommendation: `fcose`)
- Agent B — `lib/topology.py` (real topology)
- Agent C — `lib/throughput.py` (real tokens/sec math)
- **Agent D — `app.py`, `lib/style.py`, `README.md` (this file)**

Phase 2 integration will replace `mock_topology` with
`lib.topology.build_topology` and the mock tokens/sec math with
`lib.throughput.compute_tokens_per_sec`.

## File layout

```
architecture_visualizer/
├── app.py                # Dash app: layout + callbacks (Agent D)
├── lib/
│   ├── api.py            # Frozen API contract (do NOT modify)
│   ├── style.py          # Cytoscape stylesheet generator (Agent D)
│   ├── topology.py       # Agent B
│   ├── cell_estimates.py # Agent B
│   └── throughput.py     # Agent C
├── research/
│   └── LAYOUT_ENGINE.md  # Agent A
├── tests/
├── PLAN.md
├── PARALLEL_AGENT_PLAN.md
└── README.md             # This file
```
