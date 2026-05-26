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
