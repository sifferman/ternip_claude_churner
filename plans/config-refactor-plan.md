# Plan: config as a threaded struct parameter (CVA6-style), driven by per-target descriptors

---
## EXECUTION STATUS (live)

### CLASS REFACTOR — COMPLETE (2026-08-16), committed on `tps_improvement`

The config type is now a single-source **`class ternip_types #(parameter ternip_user_cfg_t Cfg)`**
(rtl/ternip_types.sv). It derives every config-dependent integer AND every config typedef
from the base `ternip_user_cfg_t` struct. `ternip_cfg_t` + `build_config()` are gone;
`ternip_pkg` no longer defines any config-derived value or type. This replaces the earlier
`ternip_cfg_t`/`build_config` struct-derive model documented lower in this file.

**Why class, not the struct-derive function:** the struct approach worked, but the class is
the single-source form the user chose ("full in on the classes"). It compiles cleanly in
VCS + yosys-slang + Vivado (deliverable). It does NOT compile in Verilator (a known
class-in-parameter-list frontend bug — repro + write-up + upstream comment filed) or via
sv2v-for-the-class (sv2v mis-lowers nested-struct-param slice access) — **both are accepted-broken**.

**Phases (each VCS-gated + deliverable-gated, committed):**
- **A — retire `ternip_cfg_t`+`build_config`; class derives all.** ternip `64c3758`, tm `3b3dacc`.
- **B — strip `ternip_pkg` config types (class is sole source); migrate wrappers+TBs+DPI.** ternip `731a2b1`, tm `f58f8e9`.
- **C — rewire secondary flows off sv2v.** tm `0b43fbb` (flows) + `870433e` (CI). ternip untouched.

**Flow status after C:**
| Flow | State | Gate result |
|---|---|---|
| VCS `tmatmul_tb`/`rms_tb` | GREEN | 10× Matrix Multiplication Passed / End simulation |
| pynqvivado deliverable (`create_bd_xpr` → v++/IP) | GREEN | project.xpr exit 0 (reads class SV + `.v` wrapper) |
| yosys_generic / yosys_xc7 | GREEN | read_slang → synth.v (10.8 MB) produced from the class |
| Verilator lint/sim | accepted-broken | class-in-param-list frontend bug (upstream comment filed) |
| sv2v-for-class | accepted-broken | mis-lowers nested-struct-param slice → invalid Verilog |
| vivado_generic / vivado_acorn | **frontend rewired + config-include proven; in-session synth BLOCKED (documented)** | project+BD+`create_bd_cell -reference ternip_kernel_wrap`+make_wrapper succeed; `launch_runs synth_1` fails elaborating top-level `ternip_types#(Cfg)` cross-hierarchy — a Vivado in-session-synth limitation (reproduces even single-file), independent of the plumbing. The deliverable sidesteps it via v++/packaged-IP. Not run to bitstream. |
| yosys GLS (`yosys_gls`) | documented-unavailable | runs under accepted-broken verilator + class packages; specialized module names / TB hierarchical probes don't survive synth |

**CI (`.github/workflows`):** `sim.yml`+`yosys_gls.yml` now hard-gate on `make yosys` (slang);
verilator lint/sim + yosys_gls steps are `continue-on-error` (accepted-broken, commented);
`--zachjs-sv2v` install removed; `liblz4-dev`/`python3-dev` kept. `SELFHOSTED_*.yml` untouched
(manual dispatch; they hit the same vivado-synth wall / class-TB GLS incompatibility).

**Left documented-but-not-fully-validated (NOT broken):** vivado_generic/vivado_acorn full
in-session synth + bitstream (Vivado class-elaboration wall — would need either
Vivado-in-session-friendly `ternip_types#(Cfg)` usage or a v++/IP packaging path like the
deliverable; both beyond this refactor's scope and NOT attempted overnight to avoid
destabilizing the working deliverable). The orphaned `rtl.sv2v.v` rule + `SV2V_ARGS` were
left in the Makefile (no flow depends on them; harmless).

---

**Build-org refactor — DONE, committed & verified** (on `tps_improvement`):
- `9fc8e00` content-addressed descriptor-driven build (Makefile hard-cut `CONFIG=`→`TARGET=`, `resolve_target.py` two-hash core, flow-scoped descriptors, N-kernel link generalizing the old `HETERO_*`).
- ternip `8d46d8c` + `ef38dbc` — `ternip_config_pkg` foundation (`ternip_user_cfg_t`/`ternip_cfg_t`/`build_config()`, 16 equivalence asserts).
- `187a56a` — host tooling driven from target descriptors (`configs_from_target`).
- Gates: VCS `tmatmul_tb`/`rms_tb` pass via the new interface; `ternip_core` lint clean; `gen-config-check` 11/11; asym `kernel.cfg` pinning correct.

**RTL `Cfg`-threading — COMPLETE, committed & verified.** Every config-consumer gained `parameter ternip_config_pkg::ternip_cfg_t Cfg = ternip_pkg::Cfg` with its scalar param defaults rebased onto same-named `Cfg` fields (behavior-identical; every swap audited same-named; VCS `tmatmul_tb`+`rms_tb` green each step):
- `ternip_tmatmul` `c7d2f98` · `ternip_rms` `4638c87` · `ternip_mul`+`ternip_div` `224f401`
- activations (`sig`/`csig`/`silu` +3 `_parallelized`) `28d41ce` · helpers (`vector_registers`+`accumulator`+`loadstore`) `59d4957`
- `ternip_rowwise_operation` `74e1a0d` · `ternip_core` `d9c1a24` · `ternip_add`+`ternip_sub` `5e557f0`
(ternary_matmul submodule bumps `ef38dbc`→`1cab866`.)

**Verified end state:** a full-tree grep for config-VALUE refs (`D`, `FixedPointPrecision`, `Rms*`, … all 18 base + 16 derived) shows **ZERO** remaining `ternip_pkg::` config-value references outside `ternip_pkg.sv` itself — every config value now flows from `Cfg`. `ternip_core` lint 0 errors; `gen-config-check` 11/11.

**Remaining (next phase, not blocking this refactor):**
- **Wire `Cfg` down from a single top-level.** Today each module independently defaults `Cfg = ternip_pkg::Cfg`; a module's own scalar params (`.D(D)`, …) DO propagate its `Cfg` to children through the existing per-param port connections, but the config-typed *port types* (`ternip_pkg::fixed_point_t`, …) still come from the global package. True per-instance / hetero-in-one-compilation needs `axi_ternip_batched` to thread `Cfg` into `ternip_core` and the port types inlined from `Cfg`. `axi_ternip_batched` is the AXI top → gated on the cocotb functional test.
- **cocotb blocker:** `dv/verilator.f` forces `--trace-fst` and this host lacks `lz4.h`, so verilator `--binary` won't link. Fix (or use a host with the FST/lz4 toolchain) before functionally validating the top AXI boundary.
- **Delete `config/*.svh`** + the `` `include `CONFIG_FILENAME `` shim once a real hardware build validates the descriptor path end-to-end.

### Revised threading model (correction to the "249 sites, every module" framing below)
Reading the leaves showed two distinct kinds of module — they thread differently:
- **Width-parameterized PRIMITIVES** (`ternip_add`/`sub`/`mul`/`div`/`sqrt`/`convert`/`sig`/`csig`/`silu`, accumulator): already take `parameter int <Width> = ternip_pkg::…` because they're instantiated at *varying* widths (e.g. rms accumulator at `RmsSqaSumPrecision`, not `FixedPointPrecision`). **These KEEP per-param parameterization — they do NOT take `Cfg`.** The config-consumer above them passes the right width down.
- **Config-CONSUMER modules** (`ternip_tmatmul`/`rms`/`rowwise_operation`/`loadstore`/`vector_registers`/`ternip_core`): take `parameter ternip_cfg_t Cfg = ternip_pkg::Cfg` and replace their `ternip_pkg::<config-param>` refs with `Cfg.user.X` (base) / `Cfg.X` (derived).

**Classification rule** (what becomes `Cfg`): the 18 base params → `Cfg.user.X`; the 16 derived ints → `Cfg.X`. **Everything else stays `ternip_pkg::`** — operation enums + literals (`MUL`/`SILU`/`tmatmul_op_e`/…), config-typed TYPE names (`fixed_point_t`/`vector_chunk_t`/`ddr_address_t`/…), and helper functions (`fixed_point_min/max/one`).

**Why this is safe to do incrementally:** `Cfg` defaults to `ternip_pkg::Cfg` and the equivalence asserts prove `Cfg.<field> == ternip_pkg::<param>` — so every swap is behavior-identical. Any misclassification (swapping an enum/type) is a **compile error** VCS catches, never a silent functional bug.

**Verification scope limit (overnight):** VCS `tmatmul_tb`/`rms_tb` cover the math + tmatmul + rms subtrees only. The top-level/AXI consumers (`ternip_core`, `loadstore`, `vector_registers`, top AXI) need the **cocotb** gate, which is currently **blocked by a missing `lz4.h`/FST toolchain issue** on this host (verilator `--binary` can't link). Thread those only after that's fixed (or on a host with cocotb working). Order tonight: `tmatmul` → `rms` → the parallelized sig/csig/silu wrappers (all VCS-covered).

---


**Decision: do it right.** Retire `config/*.svh` and the global-package-of-localparams
model entirely. Adopt CVA6's architecture — config is a **struct parameter threaded
through the module hierarchy**, values come from a **per-target descriptor**, and
`ternip_pkg` keeps only the *type + a derive function*, never the values. This is the only
model that natively expresses heterogeneous per-kernel config.

---

## Why (the problems, restated)

- **Config is build-global.** `ternip_pkg.sv` does `` `include `CONFIG_FILENAME `` and
  every module reads `ternip_pkg::`. One config per compilation → hetero must hack around
  it with `HETERO_*` Makefile vars and separate `.xo` builds.
- **`.svh` is SV-only** → no single source of truth for RTL, build TCL, and host.
- **Build dirs named after config files** (`build/xcu250_D=1024_Big/`) → `=` in paths, one
  dir per *config* not per *kernel/target*, and it has caused config-mismatch artifact bugs.
- Blast radius today: `ternip_pkg` exports **20 typedefs** (13 of them config-dependent:
  `fixed_point_t`, `vector_chunk_t`, `rms_*_t`, `ddr_address_t`, `immediate_t`,
  `tmatmul_stream_data_t`, …) + **25 derived localparams**, used at **249 sites across ~22
  modules**.

## What CVA6 does (the model we're adopting)

- `config_pkg.sv` holds a **type + helpers only**: `cva6_user_cfg_t` (raw knobs),
  `cva6_cfg_t` (full derived), `build_config(user_cfg) -> cfg` (a pure function),
  `check_cfg`, region helpers. **No config values.**
- A **target package** is just a struct literal:
  `localparam cva6_user_cfg_t cva6_cfg = '{ XLEN: 64, NrCommitPorts: 2, … }`.
- The top `cva6.sv` takes `parameter config_pkg::cva6_cfg_t CVA6Cfg = build_config(...)`,
  and **42 modules** each carry that `CVA6Cfg` parameter, referencing `CVA6Cfg.XLEN`, etc.
- Config-dependent widths are **inlined** at use (`logic [CVA6Cfg.XLEN-1:0]`) rather than
  hidden behind global config-typed typedefs — that's what makes the parameter self-contained.
- Part of the config is **generated from YAML** (`config/gen_from_riscv_config/`).

## Target architecture for ternip

### 1. `ternip_config_pkg` — types + derive function, no values
```systemverilog
package ternip_config_pkg;
  typedef struct packed {           // the ~21 raw knobs from today's .svh
    int unsigned D; int unsigned BatchSize; int unsigned VectorParallelism;
    int unsigned TmatmulParallelism; int unsigned FixedPointPrecision;
    int signed   FixedPointExponent; bit UseHardSigmoid; int unsigned NumVectorRegisters;
    int unsigned AxiAuxDataWidth; /* … */
  } ternip_user_cfg_t;

  typedef struct packed {           // full derived config (adds the 25 derived params)
    ternip_user_cfg_t user;
    int unsigned VectorSizeInBytes; int unsigned RmsSqaSumPrecision;
    int unsigned RmsAccumulatorWidth; /* … everything ternip_pkg derives today … */
  } ternip_cfg_t;

  function automatic ternip_cfg_t build_config(ternip_user_cfg_t u);  // pure, was the pkg's derived localparams
    ternip_cfg_t c; c.user = u;
    c.VectorSizeInBytes   = u.D * ... ;
    c.RmsSqaSumPrecision  = 2*u.FixedPointPrecision;
    /* … */ return c;
  endfunction

  function automatic void check_config(ternip_cfg_t c); /* asserts */ endfunction
endpackage
```

### 2. `Cfg` threaded through every module
Each module gains `parameter ternip_config_pkg::ternip_cfg_t Cfg` and references
`Cfg.user.D`, `Cfg.RmsSqaSumPrecision`, etc. The current 13 config-dependent typedefs are
handled the CVA6 way, per site:
- **Widths inline:** `logic signed [Cfg.user.FixedPointPrecision-1:0]` replaces `fixed_point_t`.
- **Where a named type must cross a port,** pass it as `parameter type` derived once at the
  top (e.g. `parameter type fixed_point_t = logic signed [Cfg.user.FixedPointPrecision-1:0]`),
  threaded alongside `Cfg`. Keeps port declarations readable without a global typedef.
- Non-config enums (`tmatmul_op_e`, `mul_impl_e`, …) stay as plain types in the package —
  they don't depend on config.

This is the bulk of the work: ~22 modules, 249 reference sites. It is mechanical but wide,
exactly the CVA6 shape.

### 3. Per-target descriptor → generated `user_cfg` literal
`targets/*.json` is the single source of truth:
```json
{ "name": "au250_asym", "platform": "xilinx_u250_gen3x16_xdma", "part": "xcu250",
  "clock_mhz": 300, "dram_banks": 4, "slr_order": [0,3,2,1],
  "defaults": { "D": 1024, "FixedPointPrecision": 8, "...": "..." },
  "kernels": [
    { "name": "ternip_big",   "count": 3, "slrs": [0,3,2], "params": { "BatchSize": 10, "TmatmulParallelism": 128 } },
    { "name": "ternip_small", "count": 1, "slrs": [1],     "params": { "BatchSize": 6 } }
  ] }
```
`scripts/gen_config.py` emits, per kernel, a tiny `build/kernels/<k>/ternip_target_pkg.sv`
holding `localparam ternip_user_cfg_t ternip_user_cfg = '{ D: 1024, BatchSize: 10, … };`
(CVA6's target-package pattern, but generated). The kernel's top defaults
`parameter Cfg = build_config(ternip_user_cfg)`.
- Because `Cfg` is a parameter, **single-compilation multi-config becomes possible** (a
  wrapper could instantiate `ternip_core #(.Cfg(big_cfg))` and `#(.Cfg(small_cfg))`
  together). We don't have to use that — the CU-per-SLR deliverable can stay separate-`.xo`
  — but the door is open and the global-package limitation is gone.

### 4. Build dirs by kernel/target (not config filename)
```
build/kernels/<kernel_name>/   # generated ternip_target_pkg.sv, mems, kernel.xo
build/targets/<target_name>/   # kernel.cfg, kernel.xclbin, reports
```
No special chars; hetero is "many kernel dirs → one target dir"; the config-mismatch bug
class disappears. `make sim`/`make lint` build one module against one kernel via
`--kernel <name>` (default `targets/dev.json`, replacing `config/generic.svh`).

### 5. One descriptor downstream
- `generate_kernel_cfg.tcl` reads `kernels[]` for `nk=`/`sp=`/`slr=` — retires
  `HETERO_KERNELS_SPEC`/`HETERO_*_CONFIG`.
- `sw_utils/lib/pynqvivado_common.py` reads the **same** descriptor for the per-CU config
  list, `bank_for_instance`/`slr_order`, and U250 pinning — it's already list-of-configs
  shaped after the NSK refactor.

## Migration (staged, each shippable; no `.svh`-bridge dead-end)

- **Stage 1 — `ternip_config_pkg` + `build_config()`, dual-run.** Introduce the struct
  type and the derive function; assert the derived `ternip_cfg_t` fields equal today's
  `ternip_pkg` localparams for every existing config (a compile-time equivalence check).
  Nothing switches yet.
- **Stage 2 — thread `Cfg` module by module.** Convert modules leaf-up to take
  `parameter ternip_cfg_t Cfg`, inlining the config-dependent widths / adding `parameter
  type` where a type crosses a port. Keep `ternip_pkg` alive as a thin shim
  (`Cfg = build_config(ternip_user_cfg_from_include)`) so the tree still builds after each
  module. Gate every step on lint + `tmatmul_tb`/`rms_tb` (VCS).
- **Stage 3 — descriptors + codegen.** Add `targets/*.json`, `scripts/gen_config.py`
  (JSON → `ternip_user_cfg` literal), and `targets/dev.json` for sim/lint. Prove the
  generated literal reproduces each current `.svh` (diff the derived `Cfg`).
- **Stage 4 — build/host on descriptors.** Re-key build dirs (`kernels/`,`targets/`); drive
  `kernel.cfg` + host from `kernels[]`; retire `CONFIG=`/`HETERO_*`.
- **Stage 5 — delete.** Remove `config/*.svh`, the `` `include `CONFIG_FILENAME `` in
  `ternip_pkg`, and the shim. `ternip_pkg` retains only non-config types; config lives in
  `ternip_config_pkg` + descriptors.

## Decisions / notes
- **Format:** JSON (Python stdlib, host already uses it). A `$schema` gives validation; a
  `defaults` block avoids per-kernel copy-paste.
- **Type threading is the cost center.** Budget the effort on Stage 2 — it's the 249 sites.
  Prefer inlining widths (CVA6 style); use `parameter type` only for types that appear in
  port lists (`vector_chunk_t`, `fixed_point_t`) to keep signatures legible.
- **Equivalence gates are non-negotiable:** Stage 1 (derived == old localparams) and Stage 3
  (generated literal == old `.svh`) each prove no value changed before anything switches.
- **Sequence:** after the current merge lands and the timing build confirms the RTL, so the
  wide refactor happens on a known-good design; land Stage 2 in small per-module PRs, each
  sim-verified.

## Recommendation
Commit to the CVA6 model — it is the proven, no-compromise answer and the only one that
makes per-kernel config first-class. The work is wide but mechanical (~22 modules), fully
gated by equivalence checks and the two sim TBs, and it collapses config, build-dir naming,
and hetero orchestration into one descriptor-driven flow.
