#!/usr/bin/env python3
"""
Parse a yosys JSON netlist and report the highest-fanout nets.

Usage:
  fanout_report.py <netlist.json> [top_n]

"Fanout" here is the LOGICAL fanout of each bit of each net: how many
distinct cell input pins are connected to that bit. A 1-bit control
signal driving N registers has fanout N. A 32-bit data bus where each
bit goes to one register has per-bit fanout 1 (so it doesn't show up as
"high fanout"; the wide-bus aspect is unrelated to fanout).

Output:
  FO    Net Name [bit]    Sample Sinks
  --- ----------------- ------------------
  4147  rms/state_q [1]   rms/cell_a/D, rms/cell_b/D, ...
  ...

Limitations:
- yosys-xc7 is not the same device family as Vivado-xcu250. Cell counts
  and exact net names may differ. But LOGICAL fanout (number of sinks
  driven by a net) is RTL-determined, so it transfers cleanly.
- After synth_xilinx -family xc7, the design is flattened. Hierarchical
  names appear as dotted paths (e.g. "core.tmatmul.state_q").
"""
import json
import sys
from collections import defaultdict


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    json_path = sys.argv[1]
    top_n = int(sys.argv[2]) if len(sys.argv) >= 3 else 50

    with open(json_path) as f:
        data = json.load(f)

    modules = data.get("modules", {})
    if not modules:
        print(f"ERROR: no modules in {json_path}", file=sys.stderr)
        return 1

    # Combined report across all modules. After synth_xilinx -family xc7
    # the design is usually flattened to a single top module, but we
    # handle the multi-module case defensively.
    all_results = []
    for mod_name, mod in modules.items():
        results = analyze_module(mod_name, mod)
        all_results.extend(results)

    # Sort by fanout descending.
    all_results.sort(key=lambda r: -r["fanout"])

    print(f"Top {top_n} highest-fanout net bits in {json_path}")
    print()
    print(f"{'FO':>5}  {'Net Name [bit]':<50}  {'Source':<35}  Sample sinks")
    print("-" * 130)
    for r in all_results[:top_n]:
        sinks_sample = ", ".join(r["sinks"][:2])
        if len(r["sinks"]) > 2:
            sinks_sample += f", ... (+{len(r['sinks']) - 2} more)"
        net_label = f"{r['net']} [{r['bit_idx']}]"
        src = r.get("source") or "(unknown)"
        print(f"{r['fanout']:>5}  {net_label[:50]:<50}  {src[:35]:<35}  {sinks_sample}")

    return 0


def analyze_module(mod_name: str, mod: dict) -> list:
    """For one module, compute per-bit fanout and return a list of records
    of the form {"net": str, "bit_idx": int, "fanout": int, "sinks":
    [str, ...]}."""

    # Step 1: walk every cell. For each input port connection, record
    # the bit as a sink. For each OUTPUT port connection, record the
    # bit's source cell -- this lets us tell what's driving the high-FO
    # net (a flip-flop bank, a LUT, etc.).
    bit_to_sinks = defaultdict(list)   # bit_id -> [pin_path, ...]
    bit_to_source = {}                 # bit_id -> "cell_name.port[idx]"
    for cell_name, cell in mod.get("cells", {}).items():
        port_dirs = cell.get("port_directions", {})
        for port, bits in cell.get("connections", {}).items():
            direction = port_dirs.get(port, "input")  # default to input
            for idx, bit in enumerate(bits):
                # Bits in yosys JSON are either ints (signal ids) or
                # the strings "0", "1", "x", "z" for constants.
                if not isinstance(bit, int):
                    continue
                if bit < 2:  # 0 and 1 are reserved for constants
                    continue
                if direction == "output":
                    if bit not in bit_to_source:
                        bit_to_source[bit] = f"{cell_name}.{port}[{idx}]"
                elif direction in ("input", "inout"):
                    bit_to_sinks[bit].append(f"{cell_name}.{port}[{idx}]")

    # Step 2: build bit_id -> (net_name, bit_index_in_net) so we can
    # report human-readable net names. A single bit_id can appear in
    # multiple netnames (aliasing); pick the first non-hidden-name one.
    bit_to_net = {}  # bit_id -> (net_name, bit_index_in_net)
    for net_name, net_info in mod.get("netnames", {}).items():
        hide = net_info.get("hide_name", 0)
        for bit_idx, bit in enumerate(net_info["bits"]):
            if not isinstance(bit, int) or bit < 2:
                continue
            existing = bit_to_net.get(bit)
            # Prefer named (non-hidden) nets; if both hidden, take first.
            if existing is None:
                bit_to_net[bit] = (net_name, bit_idx, hide)
            elif existing[2] == 1 and hide == 0:
                bit_to_net[bit] = (net_name, bit_idx, hide)

    # Step 3: produce per-(net, bit_idx) records.
    results = []
    seen = set()
    for bit, sinks in bit_to_sinks.items():
        net_info = bit_to_net.get(bit)
        if net_info is None:
            net_name = f"__anon_bit_{bit}"
            bit_idx = 0
        else:
            net_name, bit_idx, _ = net_info
        key = (net_name, bit_idx)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "net": net_name,
            "bit_idx": bit_idx,
            "fanout": len(sinks),
            "sinks": sinks,
            "source": bit_to_source.get(bit),
        })

    return results


if __name__ == "__main__":
    sys.exit(main())
