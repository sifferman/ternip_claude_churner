# QUESTIONS.md

Open questions / judgement calls Claude wants the user to review out of
band. Newest at the top. Each entry: what was decided, the alternative
considered, and how to redirect if the user wants something different.

---

## 2026-05-24 — build_2: per-lane stall1 vs MAX_FANOUT vs full lane-localization

**Decision:** Expose per-lane `lane_in_ready_o` from
`ternip_pipelined_interconnect` and compute a per-lane stall1 in
`ternip_pipelined_mem` so each `data_lanes[i].lane.ce1_i` is driven
by `!stall1_per_lane[i]` rather than the shared `!stall1`. Each lane's
`axis_tready_reg` then fans out to ~512 wdata bits instead of ~4166.

**Alternatives considered:**
- *MAX_FANOUT* on read_valid_q1/q2 (already in the code) — CLAUDE.md
  warns against trusting it; clearly hasn't held against this fanout.
- *Latching stall1* (register it per-lane) — would add a cycle of
  latency on the stall feedback, breaking the addr_q1/wdata_q1
  handshake. Rejected.
- *Full lane-localization* (each data_lane has private replicas of
  read_valid_q1/write_valid_q1/addr_q1) — works but invasive. Holding
  in reserve in case the per-lane stall1 approach doesn't fully close
  the cluster.

**Why this choice:** all NumLanes axis_pipeline_fifos inside the
interconnect already run in lockstep (CLAUDE.md & the
ternip_pipelined_interconnect comment both say so — `&lane_in_ready`
is just a safety AND-reduce). So `lane_in_ready[i]` and
`lane_in_ready[j]` are bit-identical every cycle. Driving
`data_lanes[i].lane.ce1_i` from `lane_in_ready[i]` alone preserves
correctness while turning a single FF with 4166 fanout into 8 FFs
with ~512 fanout each, placeable next to their respective data lane.

**Redirect if needed:** if the per-lane stall1 doesn't shrink the
cluster enough, the next step is the full lane-localization (private
addr_q1 / valid_q1 per lane).
