# AGGRESSIVE_PROPOSAL.md — closing the last 0.751 ns + enabling BatchSize scaling

Build_44 reached WNS=-0.751 ns at MaxCores BS=5 (69% of way to closure from build_31's -2.439). The slice+pblock recipe is saturated. This document proposes **two concrete RTL changes** to ternip that should close the remaining timing gap AND remove the throughput bottleneck that currently caps BatchSize.

Each section below tells you:
- **The path** (file:line of the actual code that's failing)
- **Why it's currently this way**
- **The exact change** (before/after code, line counts)
- **Expected impact** (WNS, throughput, area)
- **Risk** and mitigation

---

## Change 1 (short-term, ~50 lines): Decouple `latched_instr_q` from `all_fus_in_ready`

### The failing path (build_44 CSV, top intra-core cluster)

```
SRC: core[N]/buffered/core/tmatmul/state_q_reg[1]/C       (tmatmul's FSM)
DST: core[N]/buffered/core/latched_instr_q_reg[*]/CE      (instruction latch CE)
slack: -0.751 ns at the worst, 14 paths at this slack
```

Plus 102 paths from `tmatmul_operation_q_reg[1]` with similar topology.

### The exact combinational chain

In `third_party/ternip/rtl/ternip/ternip_core.sv`:

```systemverilog
// line 453-456
wire all_fus_in_ready = loadstore_in_ready
                      & rms_in_ready
                      & rowwise_operation_in_ready
                      & tmatmul_in_ready;

// line 458-459
assign instruction_ready_o = instr_ready_internal & !stall_active_q;

// line 580 (inside always_comb, INSTR_FSM_DECODE state)
instr_ready_internal = all_fus_in_ready;

// line 581-575
if (instruction_valid_i && instruction_ready_o) begin
    latched_instr_d = instruction_i;     // CE of latched_instr_q[*]
    ...
end
```

And in `third_party/ternip/rtl/fus/ternip_tmatmul.sv`:

```systemverilog
// line 667
if (state_q == WAITING_FOR_IN) begin
    in_ready_o = !queued_valid_q;       // tmatmul's in_ready_o
    ...
end
```

So the path is: `tmatmul.state_q[1]` (FF) → 4-input AND (`all_fus_in_ready`) → AND with `!stall_active_q` (`instruction_ready_o`) → AND with `instruction_valid_i` → drives CE of 64-bit `latched_instr_q` register. **Three levels of combinational logic between an FF launch and 64+ FF capture pins.** Vivado replicates `latched_instr_q` for fanout (CSV shows replicas like `_reg[19]`, `_reg[20]`, etc.), and the chain still has -0.751 ns slack.

### Why it's currently this way

`ternip_core.sv:448-456` comment:

> "Gate opcode acceptance on all FUs being ready... The vector_request mux assumes only one FU is doing a register access at a time; if we let a new opcode in before the previous instruction's FU finished, two FUs end up racing the same cycle for the vector_register port."

So the `all_fus_in_ready` AND gate is a SAFETY mechanism that enforces FU mutual-exclusion on the shared `vector_register` port. The shared port is a hard architectural constraint — only one FU can read/write it per cycle. The simplest way to enforce that is to refuse new instructions until ALL FUs are idle. That's what the current chain does.

### Why I can't just register the existing ready

I tried this. Registering `all_fus_in_ready` into a 1-cycle-delayed `all_fus_in_ready_q` and using that in the DECODE state caused:

```
%Error: ternip_core.sv:488: Verilog $stop
```

The `unique case (1)` assertion at line 488 fired because two FUs simultaneously asserted `vector_request_valid`. The registered ready told the FSM "OK to dispatch" while the previous FU was still active. Reverted.

### The proposed change: split the latch from the dispatch

**Insight**: `latched_instr_q` doesn't need to wait for `all_fus_in_ready`. It just needs to HOLD the instruction. The DISPATCH decision is what needs to wait. By decoupling the latch's update CE from the FU-ready check, we break the comb chain.

Add a 1-deep skid stage `stage1_instr_q` between `instruction_i` and `latched_instr_q`. Track separately whether `latched_instr_q` has been dispatched.

#### Before (`ternip_core.sv` ~line 555-575, inside always_comb)

```systemverilog
INSTR_FSM_DECODE: begin
    instr_ready_internal = all_fus_in_ready;     // <-- the chain
    if (instruction_valid_i && instruction_ready_o) begin
        latched_instr_d = instruction_i;          // <-- CE driven by chain
        unique case (instruction_i.fu)
            ternip_pkg::LOADSTORE: instr_fsm_d = INSTR_FSM_FETCH_LOADSTORE_ADDR;
            ternip_pkg::TMATMUL: begin
                if (instruction_i.tmatmul_op == ternip_pkg::GO) begin
                    instr_fsm_d = INSTR_FSM_FETCH_TMATMUL_ADDR;
                    tmatmul_addr_counter_d = 0;
                end else begin
                    instr_fsm_d = INSTR_FSM_DISPATCH;
                end
            end
            default: instr_fsm_d = INSTR_FSM_DISPATCH;
        endcase
    end
end
```

#### After (~30 added lines)

Add new state at the top of `ternip_core.sv` (~line 440 area):

```systemverilog
// build_46: stage1 skid. Holds the upstream instruction one cycle
// before latching into latched_instr_q. Breaks the combinational
// chain from FU state FFs to latched_instr_q[*].CE.
logic         stage1_valid_d, stage1_valid_q;
instruction_t stage1_instr_d, stage1_instr_q;

always_ff @(posedge clk_i) begin
    if (!rst_ni) begin
        stage1_valid_q <= 1'b0;
        stage1_instr_q <= '0;
    end else begin
        stage1_valid_q <= stage1_valid_d;
        stage1_instr_q <= stage1_instr_d;
    end
end

// Track whether latched_instr_q has been DISPATCHED to a FU.
// When 1, latched_instr_q can accept the next instruction from stage1.
logic latched_dispatched_d, latched_dispatched_q;
always_ff @(posedge clk_i) begin
    if (!rst_ni) latched_dispatched_q <= 1'b1;   // start "dispatched", ready for first
    else         latched_dispatched_q <= latched_dispatched_d;
end
```

Replace `instr_ready_internal` and the DECODE logic:

```systemverilog
// instruction_ready_o now reflects stage1's emptiness (NOT all_fus_in_ready)
assign instruction_ready_o = !stage1_valid_q & !stall_active_q;

always_comb begin
    stage1_valid_d = stage1_valid_q;
    stage1_instr_d = stage1_instr_q;
    latched_dispatched_d = latched_dispatched_q;
    // ... (other defaults)

    // Upstream -> stage1
    if (instruction_valid_i && instruction_ready_o) begin
        stage1_valid_d = 1'b1;
        stage1_instr_d = instruction_i;
    end

    // stage1 -> latched_instr_q (when latched is dispatched / empty)
    if (stage1_valid_q && latched_dispatched_q) begin
        latched_instr_d = stage1_instr_q;
        stage1_valid_d = 1'b0;                    // stage1 drained
        latched_dispatched_d = 1'b0;              // latched has new instr to dispatch

        // FSM transition decision uses stage1_instr_q (registered)
        unique case (stage1_instr_q.fu)
            ternip_pkg::LOADSTORE: instr_fsm_d = INSTR_FSM_FETCH_LOADSTORE_ADDR;
            ternip_pkg::TMATMUL: begin
                if (stage1_instr_q.tmatmul_op == ternip_pkg::GO) begin
                    instr_fsm_d = INSTR_FSM_FETCH_TMATMUL_ADDR;
                    tmatmul_addr_counter_d = 0;
                end else begin
                    instr_fsm_d = INSTR_FSM_DISPATCH;
                end
            end
            default: instr_fsm_d = INSTR_FSM_DISPATCH;
        endcase
    end

    // Dispatch state checks all_fus_in_ready as before
    INSTR_FSM_DISPATCH: begin
        if (all_fus_in_ready) begin              // <-- chain still here, but now goes to latched_dispatched_d (1 bit), not latched_instr_q.CE (64 bits with replicas)
            // dispatch logic per latched_instr_q.fu ...
            latched_dispatched_d = 1'b1;          // mark dispatched, ready for next
            instr_fsm_d = INSTR_FSM_DECODE;
        end
    end
end
```

### What this accomplishes

The CE chain decomposes:

**Before:**
- `tmatmul.state_q[1]` → 4-AND → 2-AND → drives 64 FF CEs (with replicas, 14+ critical paths)

**After:**
- `latched_instr_q.CE = (stage1_valid_q && latched_dispatched_q)`. **No `all_fus_in_ready` in this chain.**
- The `all_fus_in_ready` chain still drives `latched_dispatched_d` (1 bit FF, not 64). Fanout collapses from ~14 critical paths to ~1.
- `tmatmul.state_q[1]` → 4-AND → drives 1 FF (and the FSM transition). Much shorter path, much smaller fanout.

### Throughput cost

Each instruction now takes 1 extra cycle from upstream-arrival to dispatch (it sits in stage1 for 1 cycle before moving to latched_instr_q). For a workload averaging ~6 instructions per token at BS=5, that's ~6 extra cycles per token. At 300 MHz and current 1.7M cycles/token, this is **~0.0003% overhead** — invisible.

### Expected WNS impact

The 14 paths at -0.751 ns should collapse entirely. The 102 paths from `tmatmul_operation_q` (similar topology — `tmatmul_operation_q` drives various FU dispatch signals via combinational gates) would also benefit IF we apply the same decoupling pattern to dispatch (which is already part of this change).

**My estimate: +0.5 to +1.0 ns WNS recovery.** Could bring us to closure (≥0 ns) or close enough that the AUTO-FREQ-SCALING-04 flag becomes irrelevant.

### Risk

- **Subtle FSM bug**: the proposed code introduces 2 new state FFs (`stage1_valid_q`, `latched_dispatched_q`). Reset logic and edge cases need careful review.
- **Cocotb gate**: the existing cocotb top-level test covers `ldv → sv` round-trip and `tmatmul_import` exercise. Both will exercise the new pipeline. If cocotb fails, the FSM has a bug.
- **hw_emu first-layer**: must show non-zero, reasonable values in `output.0.x_f/c/h_t_slice_0` per CLAUDE.md's pass criterion.

### Validation plan (before kicking pynqvivado_au250_hw)

1. All 7 gates: lint + 4 sims + cocotb + yosys_check_pipelined.
2. hw_emu on eq1 (OneCore) — must pass first-layer.
3. Only after both pass, kick eq2.

---

## Change 2 (longer-term, ~200 lines): Per-FU vector_register ports

This is what unlocks **BatchSize scaling beyond 5**.

### Why BatchSize is currently capped

Each FU dispatches sequentially because they share one `vector_register` port. With more cores (higher BS), there are more FUs trying to compete for that port. The `all_fus_in_ready` AND gate gets wider (more terms), and the FSM stalls more often.

Looking at build_44's utilization:
- LUT: 14.2% (5x headroom)
- FF: 10.4% (8x headroom)
- BRAM: 0.7% (130x headroom)
- DSP: 2.4% (40x headroom)

Plenty of area for more cores. The blocker is the SHARED `vector_register` port serializing dispatch.

### The current shared port (`ternip_core.sv:464-540`)

`ternip_vector_registers` has ONE port (`vector_request_*`). The FSM in `ternip_core.sv` muxes between FUs based on which FU asserts its `vector_request_valid`. Line 488:

```systemverilog
unique case (1)
    loadstore_vector_request_valid:  ...
    rms_vector_request_valid:        ...
    rowwise_operation_vector_request_valid: ...
    tmatmul_vector_request_valid:    ...
endcase
```

`unique case (1)` asserts that EXACTLY ONE FU is requesting. Mutual exclusion is hardcoded.

### Proposed change: 4-port vector_register

`ternip_vector_registers.sv` becomes a 4-port memory:
- Port 0: loadstore
- Port 1: rms
- Port 2: rowwise_operation
- Port 3: tmatmul

Each FU has its own port. No more mutual exclusion. FUs can run concurrently.

#### Implementation outline

1. **`ternip_vector_registers.sv`**: change from 1 R/W port to 4 R/W ports.
   - Internally: same BRAM/distRAM, but with arbitration/banking to avoid conflicts on the same register.
   - The simplest implementation: 4-port BRAM (Ultra-RAM supports this on UltraScale+). Per-port read+write.
   - Or: per-FU local register file (each FU owns a partition of the register namespace).

2. **`ternip_core.sv`**: remove the `vector_request` mux (lines 461-540). Wire each FU's `vector_request_*` directly to its dedicated port on `ternip_vector_registers`.

3. **`ternip_core.sv`**: remove `all_fus_in_ready` (lines 448-456). The dispatch FSM no longer needs to wait for all FUs to be idle — each FU has its own port, so concurrent dispatch is safe.

4. **`ternip_core.sv`**: simplify the FSM. INSTR_FSM_DECODE just needs to check the SPECIFIC FU's ready (based on `instruction_i.fu`).

### Lines of code

- `ternip_vector_registers.sv`: ~100 lines (mostly port replication + BRAM instantiation)
- `ternip_core.sv`: ~80 lines (FSM simplification, remove all_fus_in_ready chain)
- Each FU's port list: ~5 lines × 4 FUs = 20 lines

**Total: ~200 lines.** Medium refactor.

### Expected impact

- **Concurrent FU dispatch**: instruction throughput improves significantly. The current "wait for all FUs idle" pattern stalls heavily during long-running tmatmul operations. With per-FU ports, loadstore can keep running while tmatmul is busy.
- **BatchSize scaling**: at BS=5, this would mean cores can dispatch their own loadstore/rms/rowwise operations independently. The cumulative dispatch pressure on `all_fus_in_ready` goes away. BS=6, 7, 8+ becomes viable.
- **WNS**: the entire `all_fus_in_ready` combinational chain that's currently failing disappears. Should close at 300 MHz with significant margin.
- **Tokens/sec**: estimated **2-3× improvement** at BS=5 alone (concurrent dispatch), then linear scaling with higher BS.

### Risk

This is a real RTL refactor with handshake semantics changes. Need careful design + thorough verification:
1. Each FU's port must handle write conflicts gracefully (two FUs writing to same register).
2. RAW/WAR hazards across FUs need explicit handling (or the architecture needs to forbid them).
3. The FSM in `ternip_core.sv` becomes simpler but still needs to track each FU's progress.

### Validation plan

Same as Change 1, but with more emphasis on:
- All cocotb tests pass (including a NEW test that issues back-to-back instructions to different FUs to verify concurrent dispatch).
- hw_emu first-layer pass — but ALSO check accumulated drift in later layers. If the math is right, drift should be ~same as build_44.
- Add a SystemVerilog assertion that no two FUs simultaneously read/write the SAME register address (RAW conflict detection).

---

## Suggested sequence

1. **First: implement Change 1 (skid stage)**. Low risk, fast iteration. If it closes WNS, we ship at BS=5 immediately. If it doesn't fully close but recovers ~0.5 ns, the remaining gap is small enough that build_46 with both changes is the right next step.

2. **Then: implement Change 2 (per-FU ports)**. Independently valuable for BS scaling. Even if WNS is already closed at BS=5, this enables BS=6+ which is the user's stated goal.

If you approve, I'll start on Change 1 immediately and report back with the exact diff before kicking eq2.

---

## What I will NOT propose

- **Touching MOA**. Builds 33/34/41/42/45 all tripped MOA verify failures from placement perturbation. MOA is at the placer's tolerance limit. Don't touch.
- **More slicing**. The slice+pblock recipe is saturated (build_45 confirmed). Adding more pipelined buffers risks the same MOA verify failure.
- **MAX_FANOUT experiments on any FF**. Builds 41/42 ruled this out for state_q. Probably applies to other deep-fanout FFs in MOA region.
- **`(* dont_touch *)`**. CLAUDE.md forbids.
