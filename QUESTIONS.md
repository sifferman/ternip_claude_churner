# QUESTIONS.md

Open questions / judgement calls Claude wants the user to review out of
band. Newest at the top. Each entry: what was decided, the alternative
considered, and how to redirect if the user wants something different.

---

## 2026-05-24 — 2026.05.24-0827 was a regression; reverted; what's next?

**Outcome of 2026.05.24-0827:** Net-negative. The per-lane `stall1` change in
`ternip_pipelined_mem` did eliminate the 4166-fanout
`axis_tready → wdata_q1.CE` cluster, but the placer's re-layout exposed
worse clusters in `tmatmul_operation_q[1]` → its replicas
(slack -0.308 ns, 7 LUT levels) and
`tmatmul_operation_q[1] → latched_tmatmul_addrs_q.CE` (slack -0.19 ns,
5 LUT levels). Net: WNS -0.259 → -0.308, TNS 4× worse, frequency
242.1 → 216.4 MHz. See 2026.05.24-0827 release for full data.

**Decision (autonomous):** Reverted the per-lane stall1 RTL change in
ternip + ternary_matmul (kept the hard_→build_ rename in place). Pushed
the reverts. **Did NOT kick a 2026.05.25-1846 build.** We already have 2026.05.24-0501
numbers from the same post-revert RTL state — a pure-revert build would
just re-spend 3 hours producing data we already have.

**Why not kick a new build:** The next move needs a fresh idea, not a
re-run of 2026.05.24-0501. Choosing what that idea is benefits from your
direction — and the cost of waiting (eq2 idle) is much less than the
cost of running an iteration in the wrong direction.

**Candidate next moves (priority-ordered, my best guess):**

1. **Attack `tmatmul_operation_q[1]`'s 7-LUT-level self-loop.** This
   path (Q → 7 LUT → replica.D) is what the 2026.05.24-0827 layout exposed and
   what's gating WNS now if the placer happens to settle the same way
   without 2026.05.24-0827's RTL change. Likely structural fix: register the
   FSM transition decode (insert an intermediate FF between `state_q ==
   X && tmatmul_operation_q == Y` decode and the next-state mux), or
   restructure the FSM to flatten the cone.
2. **Address-staging FSM (`latched_tmatmul_addrs_q`, ternip_core).** CLAUDE.md
   item #3. Wide demux with FO~410 surfaced as the WNS path in 2026.05.24-0827.
   Restructure `latched_tmatmul_addrs_d` write logic so the bank-index
   mux is registered before the wide-write FF, or split the
   `latched_tmatmul_addrs_q` array into per-bank named regs.
3. **Disable `--trace_memory` (CLAUDE.md item #5).** Easy to revert,
   easy to measure. Doesn't touch RTL. Removes the AIM/trace_buffer
   infrastructure that consumes routing for runtime profiling we don't
   use during timing closure.
4. **Move to MaxCores anyway and start scaling `BatchSize`.** WNS=-0.259
   isn't quite "within 0.1 ns of 0" per CLAUDE.md, but it's close. If
   you're more interested in throughput than the last ns of OneCore
   closure, this is the productive path.

**Redirect if you want a different option:** just point me at it. If you
want me to start option (3) on a new session, that's the lowest-risk
forward motion.

---

## 2026-05-24 — 2026.05.24-0827: per-lane stall1 vs MAX_FANOUT vs full lane-localization (resolved)

**Decision:** Expose per-lane `lane_in_ready_o` from
`ternip_pipelined_interconnect` and compute a per-lane stall1 in
`ternip_pipelined_mem` so each `data_lanes[i].lane.ce1_i` is driven
by `!stall1_per_lane[i]` rather than the shared `!stall1`. Each lane's
`axis_tready_reg` then fans out to ~512 wdata bits instead of ~4166.

**Outcome:** Reverted. See entry above; CLAUDE.md "Things that were
net-negative" list updated.

**Alternatives considered (still on the table for a future attempt):**
- *MAX_FANOUT* on read_valid_q1/q2 (already in the code) — CLAUDE.md
  warns against trusting it; clearly hasn't held against this fanout.
- *Latching stall1* (register it per-lane) — would add a cycle of
  latency on the stall feedback, breaking the addr_q1/wdata_q1
  handshake. Rejected.
- *Full lane-localization* (each data_lane has private replicas of
  read_valid_q1/write_valid_q1/addr_q1) — works but invasive. Still in
  reserve if someone wants to retry the wide-CE attack later. The
  per-lane stall1 attempt failed not because the structural idea was
  wrong, but because removing one cluster freed the placer to make
  worse decisions elsewhere — full lane-localization probably has the
  same risk.

---

## 2026-05-29 ~3:30 PM PDT: hw_emu numerical FAIL — pre-existing or build_31?

The hw_emu (`make pynqvivado_au250_hw_emu`) finished and reported
`FAILED!`. Per-output-slice fail counts (out of 5120 elements):

- output.0.x_f_slice_0:  ~0 failures (clean)
- output.0.x_c_slice_0:  11
- output.0.x_g_slice_0:  20
- output.0.h_t_slice_0:  1024–2865 (h_t = hidden state — first slice
  is where the wheels fall off; everything downstream tracks h_t)
- output.0.d_t_slice_0:  3329
- output.0.x_o_slice_0:  2930
- output.1+:             2900–3200 each (accumulated drift after h_t)

**Why this is ambiguous wrt the build_31 change:**

- **cocotb test (committed in 2e7bd80) PASSES all 4 tests**, including
  a tmatmul_import + tmatmul_go that exercises the new
  `ternip_pipelined_interconnect` register slice on
  `m_axi_tmatmul_<b>_r`. Read bursts went through cleanly on all 4
  banks. No protocol hang, no missing handshakes.
- The build_31 RTL is bit-preserving: the pack/unpack order matches
  `{rid, rdata, rresp, rlast}` on both sides, and
  `ternip_pipelined_interconnect` is already used elsewhere in
  `ternip_buffered.sv` for similar channels.
- The first slice (x_f) is clean — the AXI handshake clearly works
  for the first non-recurrent slice.
- It breaks at **h_t** specifically, which is recurrent. Recurrent
  ops use `tmatmul_import` + `tmatmul_go`, but so does the matmul
  in x_f/x_c (which passes for x_f and barely fails for x_c).

**What I would ask if I could:**

1. Is the `h_t` divergence pre-existing? Was hw_emu validated on a
   recent commit before 1aff984 (build_31)? Your note "first two
   layers visually look pretty close" suggests the test has been
   loose, but the user-criterion was visual — not the
   `[FAIL] N element(s)` counter that the script prints.
2. If pre-existing: cocotb passes, ship build_31 RTL.
3. If new: the most suspect change is the R-channel pack/unpack
   order. I'd re-check the `ternip_pkg::tmatmul_stream_data_t`
   width vs `DdrDataWidth` — if the buffer's `TmatmulRChannelWidth`
   = 8 + DdrDataWidth + 2 + 1 doesn't match what the kernel expects,
   the test might pass cocotb (which uses AxiRamRead = passthrough)
   but fail on the real DMA's beat boundaries.

**Action:** kept eq2's `make vivado MaxCores BS=5` running with
build_31 RTL. cocotb shows the AXI handshake is functionally
preserved; the make vivado will measure whether the register slice
helps timing. The numerical mismatch is its own investigation,
separate from build_31's timing goal.
