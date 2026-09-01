# Scaled Matmul-Free Language Model (fake 50B / 100B)

Goal: run **fake** 50B and 100B models — weights come from a host-side RNG, token
output is not checked. We care about **tokens/second**, **J/token**, and where time
is wasted (swap instructions, inter-kernel activation transfer).

Base branch: **`larger-model-fixes`** (has the working 370M/1.3B/2.7B configs, the
`ternip.xdc` fanout fix, and the CI/cocotb repairs). Work in a new
`scalable_matmulfree` branch off it.

## Model specs

| | Layers | Vocab | Hidden (D) | Small Intermediate | Large Intermediate | Params/Layer | LM head + Embedding | Total Params |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2.7B (real, shipping) | 32 | 32000 | 2560 | 6912 | 13824 | 79,298,560 | 81,920,000 | 2.70e9 |
| "50B" | 64 | 128000 | 5120 | 13824 | 27648 | 317,194,240 | 655,360,000 | **20.96e9** |
| "100B" | 80 | 128000 | 10080 | 27216 | 54432 | 1,229,437,440 | 1,290,240,000 | **99.65e9** |

Note the names are aspirational: the "50B" is ~21e9 params and the "100B" is
~99.7e9. Weight volume at 2 bits/param (ternary):

| | ternary weight volume | banks needed (16 GB each) |
|---|---:|---:|
| "50B" | 5.2 GB | **1** |
| "100B" | 24.9 GB | **2** |

## Approach

### D substitution
D=10080 is not buildable. Use **D=2048** and tile: a `[10080x10080][10080x1]`
matmul compiles to 25 instructions. This is already supported —
`AlgorithmTree.new_abstract_operation()` maps one abstract vector to several
`instruction_vector_ids`. The tiling is **exact**, not an approximation.

### Pipelining: make it a parameter
Add a parameter for the number of pipeline stages:

- **50B → 1 stage.** The whole model fits in one 16 GB bank, so all kernels run
  as independent data-parallel replicas (more lanes, no transfers).
- **100B → 2 stages.** Needs 2 banks, so split across 2 kernels: kernel 0 holds
  the first 40 layers, kernel 1 the rest. Only the **host code** changes — the
  hardware and bitstream are identical. Between stages the host moves
  *activations*, not weights, so the transfer is small.

**Heterogeneous kernels must share every config value except `BatchSize`, and the
smaller `BatchSize` is used for both.**

Also add a flag that **repeats one stage's layers 4x** to reach the full depth
without doing the host transfers — that isolates transfer cost from compute.

### What to measure
- **tokens/second**, both steady-state pipeline throughput and per-token latency
  including the PCIe round-trips.
- **J/token** (see tooling below).
- **Time wasted in swap and inter-kernel transfer.** For swap,
  `report_instruction_timing`'s existing breakdown is sufficient — it already
  reports a "no swap instructions" speedup (1.10x on 370M). No new hardware
  counters needed.

### Verification
No numerical verification of the fake models. Do confirm the **emulator and
hardware agree on a single layer** so a hang or truncated run can't masquerade as
a valid benchmark.

### Vocab
Not modeled. `embed_token` and `lm_head` run on the CPU and are excluded from the
benchmark, so vocab size does not affect the reported number.

## Verified facts that shape the work

**Layer count and vocab are NOT in the bitstream.** `matmulfree_algorithm_tree.py`
reads `num_layers` from the HuggingFace model config. What *is* baked into
`TERNIP_CFG` is `D`, `VectorParallelism`, `TmatmulParallelism`, `BatchSize`,
`FixedPointPrecision`/`Exponent`, and `NumVectorRegisters`. So a fake 64- or
80-layer model at D=2048 can run on the **existing 1.3B bitstream** — no FPGA
build needed to get started.

**`NumVectorRegisters = 4` in all shipped configs, and it is baked in.** A 10080
vector at D=2048 is 5 instruction vectors, so it cannot be resident — it will be
swapped through 4 registers. Expect the 100B to be **swap-dominated**. NVR is
likely the single biggest lever: NVR 4->8 measured **+5.3%** on the 370M from swap
elimination alone, and with a 5-tile vector the effect should be much larger.
Budget one rebuild for NVR early rather than discovering this late.

**Instruction caching is worth building, but will not speed up today's runs.**
Measured compile time is only **77 s**; setup is dominated by staging 727 MB per
CU (~2.9 GB total) into the DDR banks, which caching cannot avoid. It matters for
the 80-layer x 25-tile case, where compile will grow a lot.

## Practical setup

### Hardware
The U250 is on **godbolt** (`gpu01`), BDF `0000:64:00.1`. Host XRT is 2.23 and has
dropped the legacy `xcl*` API, so everything runs in the `/au250_xrt` container
via `au250-run` (XRT 2.15 + pynq 3.0.1), which mounts CWD at `/work`.

- **Delete `loaded.xclbin` before every run.** A stale one silently programs the
  wrong bitstream and fakes a hardware failure.
- `TARGET` paths must be **repo-relative** (the Makefile cd's to the project root).
- Helper scripts already on the box: `~/ternip_run/runbasic.sh`, `runbench.sh`,
  `powerbench.sh`.

### Start from the built 1.3B xclbin
`~/ternip_run/kernel_1_3B_bs12.xclbin`, md5 `b732b1ce29bf1e5704d5e645a36d92db`,
D=2048, BatchSize 12x3+5 (41 lanes). Validated: 4/4 CUs pass, **969.88 tok/s**
measured. This is the right starting point for the fake models.

### Builds
**eq2 only — never eq1** (reserved for other users). One build at a time, ~6.5 h
each. Post-place WNS has predicted the final outcome on every build so far, so
check it ~3 h in rather than waiting.

### Measuring power / J/token
`xbutil examine -d 0000:64:00.1 --report electrical` gives board power.
`powerbench.sh` samples it alongside a benchmark. Measured baselines:

| model | J/token | load power | idle power |
|---|---:|---:|---:|
| 370M | 0.01437 | 53.35 W | 40.35 W |
| 1.3B | 0.05417 | 52.66 W | 39.13 W |
| 2.7B | 0.12670 | 52.05 W | 37.20 W |

Two gotchas: each sample spawns a container, so the real sampling interval is
~3 s not 1 s; and `NUM_TOKENS` scales the instruction stream linearly (2000 tokens
-> 145 MB per CU, which swamps the run). Use `NUM_TOKENS=300`.

### Modeling before building
`report_instruction_timing.py <target.json> <model>` projects tok/s and cycle
counts with no hardware. Use it to cost a configuration before spending 6.5 h.

### Test the tool, not the flow
Do **not** use full builds to answer Vivado questions. A ~15-line module
synthesized out-of-context answers most of them in ~3 minutes. This is how we
established that `MAX_FANOUT` only applies during `synth_design`, that `foreach`
is rejected in XDC files (`Designutils 20-1307`), and that no post-elaboration
TCL hook exists. Two 6-hour builds were wasted before adopting it.

### Gate gotchas
- `CCACHE_DISABLE=1` for cocotb — ccache + precompiled headers triggers a gcc ICE.
- `liblz4.so` dev symlink needed for the patched verilator
  (`ln -sf /usr/lib/x86_64-linux-gnu/liblz4.so.1 ~/.local/lz4shim/liblz4.so`
  plus `LIBRARY_PATH`).
- `sim_build` is now keyed by config hash. Before that fix, switching `TARGET`
  silently reused the previous elaborated design and produced convincing but
  false failures.
