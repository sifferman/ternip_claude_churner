# LAYOUT_ENGINE.md — Phase 1 Agent A research

## 1. Executive Summary

Recommendation: **`fcose`** (fast CoSE) as the primary layout, with the 4
DRAM banks pinned via fcose's native `fixedNodeConstraint` +
`alignmentConstraint`. fcose ships in `dash_cytoscape.load_extra_layouts()`,
runs ~2× faster than `cose` at 100 nodes, and produces clean hub-and-spoke
output. For the dense BS=20 case, fall back to **`dagre`** (deterministic,
hierarchical TB) so the data-flow direction stays readable.

## 2. Comparison Table

| Layout | Pinning | Perf @ 100n | Bundling | Determinism | Notes |
|---|---|---|---|---|---|
| `preset` | Perfect (manual x/y) | Instant | None | Yes | All layout work in Python; useful for pinning only. |
| `grid` | No | Instant | None | Yes | Loses topology — bad for hub-and-spoke. |
| `circle` | No | Instant | None | Yes | OK ≤20 nodes; chaos at 100. |
| `concentric` | No | Instant | None | Yes | Works if there's a clear "center" (e.g. one tmatmul core). |
| `cose` | `locked:true` honored, drifts poorly | ~1-3 s | None | Stochastic | Default; eclipsed by fcose. |
| `cose-bilkent` | `locked:true` honored | Slower than fcose | None | Stochastic | Better than `cose`; fcose supersedes. |
| **`fcose`** | **Native `fixedNodeConstraint` + alignment + relative-placement** | **~0.5-1.5 s, 2× cose** | None | Yes if `randomize:false` + `quality:'proof'` | **Recommended.** Matches DRAM-pin + cluster-cores requirements. |
| `cola` | `locked` + rich constraints, but flaky (issue #1137) | ~1-3 s; smooth incremental | None native | Stochastic | Strong constraints, but locked-node bug. fcose wins. |
| `dagre` | No pin; pre-rank with `rank:'min'` | ~0.2-0.5 s | None | Yes | **Best fallback for BS=20.** TB makes DRAM→core→export legible. |
| `klay` | Limited | Moderate | None | Yes | ELK port; heavier than dagre, no advantage here. |

## 3. Recommended Layout — `fcose`

1. **Native pinning** via `fixedNodeConstraint` + `alignmentConstraint` —
   DRAM banks stay at the top edge without custom JS.
2. **Bundled** with `load_extra_layouts()` — no separate dependency.
3. **Performance** — under 2 s at 100 nodes / 400 edges; re-runs cheaply
   on BatchSize-slider changes.
4. **Determinism** — `randomize:false, quality:'proof'` is reproducible
   enough for screenshot diffs (minor jitter on free nodes acceptable).
5. **Aesthetics** — hub-and-spoke patterns (tmatmul core fanning out to
   vector_registers / loadstore / rms) get sensible angular spread; bus-
   width edges read clearly with no crossings through the pinned region.

## 4. Fallback for BS=20 Dense Case

fcose's force-directed output can blob up at ~100 nodes / 400 edges.
Fallbacks, in order:

- **`dagre`** with `rankDir:'TB'`. Pin DRAM banks to the top rank by
  setting `rank:'min'` on those nodes. Deterministic, fast, and top-
  down flow matches how an FPGA engineer reads a data-path.
- **`fcose` + concentric pre-positioning**: Python computes an initial
  polar layout via `preset` (banks on top semicircle, cores radial),
  then fcose with `randomize:false` only refines.

Agent D should expose this as a dropdown; key the layout config dicts
off the selected name.

## 5. Implementation Notes for Phase 1 Agent D

```python
# at app start, before app = Dash(...)
import dash_cytoscape as cyto
cyto.load_extra_layouts()   # enables fcose, cose-bilkent, cola, dagre, klay, euler, spread
```

**Pinning DRAM banks** — two equivalent options:

1. **Per-node `locked: True` + `position`** (works with any layout):
   ```python
   {'data': {'id': 'dram_0'}, 'position': {'x': -300, 'y': -400},
    'locked': True, 'grabbable': False}
   ```
   `locked:true` makes position immutable through layout re-runs;
   `grabbable:false` blocks user dragging.

2. **`fixedNodeConstraint` in the fcose layout dict** (cleaner):
   ```python
   layout = {
       'name': 'fcose',
       'randomize': False,
       'quality': 'proof',
       'animate': False,
       'fit': True,
       'padding': 30,
       'nodeRepulsion': 8000,
       'idealEdgeLength': 80,
       'fixedNodeConstraint': [
           {'nodeId': 'dram_0', 'position': {'x': -300, 'y': -400}},
           {'nodeId': 'dram_1', 'position': {'x': -100, 'y': -400}},
           {'nodeId': 'dram_2', 'position': {'x':  100, 'y': -400}},
           {'nodeId': 'dram_3', 'position': {'x':  300, 'y': -400}},
       ],
       'alignmentConstraint': {'horizontal': [['dram_0','dram_1','dram_2','dram_3']]},
   }
   ```

Prefer (2): constraint lives in the layout config, so swapping to
`dagre` is just replacing the dict — no node-data churn.

**Don't enable `autolock`** at the Cytoscape component level — it
freezes every node, including free ones the user should drag.

**Edge bundling** — none of these layouts bundle natively. For BS=20
clutter, style with `'curve-style':'bezier'` + `'control-point-step-size'`,
or `'haystack'` which groups parallel edges. True (Holten-style)
bundling needs a separate JS lib — out of scope for Phase 1.

**Non-cytoscape alternatives — skip.** `pyvis` renders to a standalone
iframe, breaking Dash callbacks. `networkx + Plotly` loses node-drag
interactivity. `sigma.js` would need a custom Dash component. Stick
with dash-cytoscape.

Sources:
- [Layouts | Dash for Python Documentation](https://dash.plotly.com/cytoscape/layout)
- [cytoscape.js-fcose README](https://github.com/iVis-at-Bilkent/cytoscape.js-fcose/blob/master/README.md)
- [fcose demo and constraint docs](https://ivis-at-bilkent.github.io/cytoscape.js-fcose/)
- [Using layouts (cytoscape.js blog)](https://blog.js.cytoscape.org/2020/05/11/layouts/)
- [cytoscape.js-cola](https://github.com/cytoscape/cytoscape.js-cola)
- [cytoscape.js-dagre](https://github.com/cytoscape/cytoscape.js-dagre)
- [dash-cytoscape #119 — fcose added to extras](https://github.com/plotly/dash-cytoscape/issues/119)
- [cytoscape.js issue #1137 — locked-node behavior](https://github.com/cytoscape/cytoscape.js/issues/1137)
