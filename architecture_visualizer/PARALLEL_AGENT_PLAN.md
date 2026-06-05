# PARALLEL_AGENT_PLAN.md — multi-agent work breakdown

Strategy for splitting the architecture_visualizer build across multiple
Claude Code agents in parallel. Designed so **no two agents touch the same
file** and the only coordination needed is the API contract in §1.

## §1 Shared contract (read by every agent, written by NOBODY during Phase 1)

These shapes/signatures live at the top of `PLAN.md` and **must not change**
once Phase 1 starts. If a contract needs revision, integration agent (§3)
makes the call.

```python
# Node
{
    "id":         "tmatmul_dma_b0_c0",  # unique
    "label":      "tmatmul_dma[0]",     # display
    "type":       "tmatmul_dma",        # for coloring/grouping
    "bank":       0,                    # int | None
    "core":       0,                    # int | None
    "cell_count": 1234,
    "slr":        0,                    # int | None (None = movable)
}

# Edge
{
    "source":   "tmatmul_dma_b0_c0",
    "target":   "tmatmul_unit_b0_c0",
    "bus_bits": 256,
    "formula":  "TmatmulParallelism * 2",  # for hover tooltip
}

# Topology API
build_topology(
    variant: Literal["NumSeparateAxiInstances",
                     "NumDdrBanksPerTmatmul",
                     "NumTmatmulBanksPerCore"],
    params:  dict,   # keys: TmatmulParallelism, VectorParallelism,
                     #       BatchSize, NumDdrBanksUsed, plus all
                     #       parameters from a Config dict
) -> tuple[list[Node], list[Edge]]

# Throughput API
compute_tokens_per_sec(
    config: dict,    # same parameter keys as Config.from_dict
    model:  str,     # e.g. "MMfreeLM-370M"
) -> dict            # {"singlecore": float, "multicore": float, "clk_freq_mhz": float}

# Stylesheet API
generate_stylesheet(
    nodes_by_type:  dict[str, str],   # type -> color hex
    highlight_set:  set[str],         # node ids to override
) -> list[dict]      # cytoscape stylesheet entries
```

## §2 Phase 0 — Setup (single agent, ~10 min)

**One agent**, before any parallel work begins.

| Task | Files created |
|---|---|
| Create `requirements.txt`, `lib/__init__.py`, `tests/__init__.py`, empty stub modules with docstrings only | `requirements.txt`, `lib/{topology,cell_estimates,throughput,style}.py` (empty), `tests/` |
| Pin API contract above into `PLAN.md` and `lib/api.py` (Python TypedDict / Protocol classes) | `lib/api.py` |
| Initial `README.md` skeleton (how to launch, will be filled by §3 Agent D) | `README.md` |

After this completes, the agents in §3 can start in parallel. **Nothing in §3
modifies any of these files** except `app.py` (still empty after §2).

## §3 Phase 1 — Parallel work (4 agents simultaneously)

Each agent's exclusive file list is shown. No file appears in two agents'
lists.

### Agent A — Layout Engine Research (research only, ZERO code)

**Files**:
- `research/LAYOUT_ENGINE.md` (NEW)

**Task**: Investigate Python-driven interactive graph layout engines for the
cytoscape graph. Compare (at minimum): `cose`, `cose-bilkent`, `cola`, `dagre`,
`klay`, `preset` (hand-positioned). Recommend one for the default layout, plus
one for "complex BS=20 high-density" mode if different. Document trade-offs:
- DRAM banks must be pinnable (fixed top row)
- Cores spread across the rest of the canvas
- Performance at ~100 nodes / ~400 edges (BS=20 N=4 case)
- Edge-bundling support (for the dense BS=20 case)

Output is a single Markdown doc with a Recommendation section. **No code
written.** This unblocks Agent D's stylesheet/layout choice.

**Duration**: ~30 min of WebFetch + writing.

### Agent B — Topology + Cells Builder (data layer)

**Files**:
- `lib/topology.py`
- `lib/cell_estimates.py`
- `tests/test_topology.py`
- `tests/test_cell_estimates.py`

**Task**: Implement node/edge generation for ALL THREE architecture variants.
For each (variant, params) input, produce the node + edge lists matching the
contract in §1.

Sub-tasks:
- Write `cell_estimates.py` with one formula per node-type (e.g. `MOA = TmatmulParallelism × FixedPointPrecision × log2(TmatmulParallelism)`)
- Write `topology.py` with three `_build_<variant>()` functions and one
  dispatcher
- Tests covering: N=1/2/4 for each variant, BS=1/4/20, D % N edge cases
  (NumTmatmulBanksPerCore variant should reject N=3 when D=1024 since
  1024 % 3 ≠ 0)

**Dependencies**: §1 contract (read-only). Does NOT depend on Agent A.

**Duration**: ~1-2 hours.

### Agent C — Throughput Math + Config Refactor (math layer)

**Files**:
- `lib/throughput.py`
- `tests/test_throughput.py`
- `ternary_matmul/sw_utils/lib/config.py` (UPSTREAM patch — see notes)

**Task**: Re-implement the math from
`ternary_matmul/sw_utils/target/report_instruction_timing.py` as a pure
in-process Python function (no subprocess). Add unit tests that verify
parity by:
1. Reading several real `.svh` configs (OneCore, MaxCores, BS2_N4)
2. Subprocessing the original `report_instruction_timing.py`
3. Calling `compute_tokens_per_sec()` with the same parameters
4. Asserting equal singlecore + multicore tokens/sec

Sub-tasks:
- Add `Config.from_dict(d)` classmethod to upstream `config.py`
  (backward compatible — does not break existing `Config(configpkg_files=...)`
  callers)
- Port the throughput-relevant math from `report_instruction_timing.py`
- Write parity tests against the original script as ground truth

**Dependencies**: §1 contract (read-only). Does NOT depend on Agents A, B, D.

**Coordination note**: This agent touches a file outside
`architecture_visualizer/`. Coordinate the commit message and timing so it
lands as a single coherent patch.

**Duration**: ~2 hours.

### Agent D — GUI Shell + Cytoscape Integration (UI layer)

**Files**:
- `app.py`
- `lib/style.py`
- `README.md` (fill in from §2 skeleton)

**Task**: Build the Dash UI:
- Top bar: variant dropdown
- Left panel: sliders (TP, VP, BS, NumDdrBanksUsed) + "Compute tokens/sec"
  button + tokens/sec readout
- Center: Cytoscape pane (initial layout per Agent A's recommendation)
- Right sidebar: hierarchical node list with per-node highlight checkboxes
- Bottom strip: live wire-length statistic

Use **MOCK data** from a stub `mock_topology()` function in `app.py` while
Agent B's `lib/topology.py` is in flight. The contract guarantees the data
shape, so swapping mock for real is a single import change at integration time.

`lib/style.py` implements `generate_stylesheet()` from the contract: node
colors by type, sqrt-scaled radii, edge thickness linear in `bus_bits`,
highlight-override colors.

**Dependencies**: Agent A's `LAYOUT_ENGINE.md` (for default layout choice).
Otherwise read-only on §1 contract.

**Duration**: ~2-3 hours.

## §4 Phase 2 — Integration (single agent, after §3 completes)

**One agent**. Wires the modules together.

**Files** (may modify ANY existing file):
- `app.py` (swap mock_topology for `lib.topology.build_topology`)
- `lib/style.py` (final polish on color mapping)
- Add `tests/test_app_callbacks.py` (smoke tests for Dash callbacks)

Tasks:
- Replace mock data with real topology calls
- Wire `Compute tokens/sec` button to `lib.throughput.compute_tokens_per_sec`
- Verify hover tooltips display the formula strings from edges
- Verify D % N edge case is presented to user gracefully (not silently broken)
- Live-on-drag wire-length: cytoscape node-position event → recompute
  `Σ bus_bits × pixel_distance`

**Duration**: ~1-2 hours.

## §5 Coordination summary

| Agent | Reads from | Writes to | Conflicts with |
|---|---|---|---|
| §2 Setup | (nothing) | scaffold files | (nobody — runs first) |
| §3-A Layout research | (nothing) | `research/LAYOUT_ENGINE.md` | nobody |
| §3-B Topology+cells | `lib/api.py` | `lib/topology.py`, `lib/cell_estimates.py`, `tests/test_topology.py`, `tests/test_cell_estimates.py` | nobody |
| §3-C Throughput | `lib/api.py` | `lib/throughput.py`, `tests/test_throughput.py`, `sw_utils/lib/config.py` | nobody |
| §3-D GUI shell | `lib/api.py`, A's research doc | `app.py`, `lib/style.py`, `README.md` | nobody |
| §4 Integration | everything | `app.py`, `lib/style.py`, new tests | each prior agent's files (but they're done) |

**No file appears in two parallel agents' write lists.** The §1 contract is
the only shared dependency; once it's frozen at the end of §2, all four §3
agents are fully independent.

## §6 Recommended dispatch

The four §3 agents can be spawned **in a single message with multiple
parallel Agent tool calls** (per Claude Code's parallelism guidance). Each
gets a prompt with:
1. Pointer to this file (`PARALLEL_AGENT_PLAN.md`) for context
2. Pointer to `PLAN.md` for project decisions
3. Their specific §3 subsection as the task description
4. The §1 contract verbatim
5. Explicit "you may NOT modify any file not listed in your Files section"

§4 integration runs sequentially AFTER all §3 agents report complete.

## §7 What can NOT be parallelized

- **The §1 contract definition** — must be settled before §3 forks. Any
  contract change mid-flight invalidates work.
- **`app.py` editing** — Agent D writes it in §3; Agent §4 modifies it in §4.
  Two writers (with the dependency ordering between phases) is fine, but two
  simultaneous writers is not.
- **End-to-end testing** — meaningful only after integration.

## §8 Estimated time-to-running-demo

- §2 setup: 10 min sequential
- §3 phase: ~3 hours (longest agent's duration, all parallel)
- §4 integration: ~1-2 hours sequential

**Wall clock total: ~4-5 hours**, vs ~8-10 hours single-threaded.
