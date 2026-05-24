#!/usr/bin/env python3
"""
Parse a Vitis report_accelerator_utilization report (kernel_util_*.rpt)
and present a clean breakdown of:

  - Total chip resources (Platform + User Budget)
  - Platform (XRT overhead): cells the shell uses regardless of kernel
  - Kernel (ternip_ip): cells used by the user's RTL
  - Unused (free): what's still available inside the User Budget for
    growth

Usage:
  parse_kernel_util.py <path-to-kernel_util_routed.rpt>
  parse_kernel_util.py <build-root-dir>      # auto-finds the rpt

The .rpt is a fixed-width text table written by Vivado's
report_accelerator_utilization at the end of impl_1. Format:

  +-----+-------+--------+...
  | Name| LUT   | LUTAsMem|...
  +-----+-------+--------+...
  | Platform        |  184531 [ 10.69%] |  ...
  | User Budget     | 1541677 [100.00%] |  ...
  |    Used Resources |  96775 [  6.28%] |  ...
  |    Unused Resources | 1444902 [ 93.72%] |  ...
  | ternip_ip       |   96775 [  6.28%] |  ...
  |    ternip_ip_1  |   96775 [  6.28%] |  ...
  +-----+-------+--------+...

Each cell value has the form "NNNNN [ NN.NN%]". The % is relative to
the User Budget for that resource (so Platform's % is XRT's share of
total chip area, while Used Resources' % is the kernel's share of the
budget).
"""
import os
import re
import sys
from typing import Optional


REPORT_GLOB = "kernel_util_routed.rpt"
FALLBACK_GLOB = "kernel_util_placed.rpt"


def find_report(path: str) -> str:
    """If path is a file, return it. Otherwise walk down looking for one."""
    if os.path.isfile(path):
        return path
    candidates = []
    for root, _, files in os.walk(path):
        for name in files:
            if name == REPORT_GLOB or name == FALLBACK_GLOB:
                candidates.append(os.path.join(root, name))
    if not candidates:
        raise FileNotFoundError(
            f"no kernel_util_*.rpt under {path}. Has the build reached impl_1?"
        )
    # Prefer routed over placed; among multiples, prefer the most recently
    # modified one (most likely the latest build).
    candidates.sort(
        key=lambda p: (REPORT_GLOB not in p, -os.path.getmtime(p))
    )
    return candidates[0]


# Match a value like "  96775 [  6.28%]"
CELL_RE = re.compile(r"^\s*([0-9]+)\s*\[\s*([0-9.]+)%\]\s*$")


def parse_cell(s: str) -> Optional[tuple[int, float]]:
    m = CELL_RE.match(s)
    if not m:
        return None
    return int(m.group(1)), float(m.group(2))


def parse_report(path: str) -> dict:
    """Return a dict keyed by row name with per-column (value, percent) tuples."""
    with open(path) as f:
        lines = f.read().splitlines()

    # Find the System Utilization table -- starts after a line that
    # contains "Name" and "LUT" etc.
    header_idx = None
    for i, line in enumerate(lines):
        if "Name" in line and "LUT" in line and "REG" in line:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"could not find Name/LUT/REG header in {path}")

    # Header line gives column names. Split by | and strip.
    columns = [c.strip() for c in lines[header_idx].split("|")[1:-1]]
    # First column is "Name"; the rest are resource types.
    resource_cols = columns[1:]

    rows = {}
    for line in lines[header_idx + 1:]:
        if line.startswith("+"):
            # +---+---+ separator -- table end if we've already collected
            # data; otherwise just the separator under the header.
            if rows:
                break
            else:
                continue
        if "|" not in line:
            continue
        parts = [c.strip() for c in line.split("|")[1:-1]]
        if not parts:
            continue
        name = parts[0]
        if not name:
            continue
        vals = {}
        for col_name, raw in zip(resource_cols, parts[1:]):
            cell = parse_cell(raw)
            if cell:
                vals[col_name] = cell
        if vals:
            rows[name] = vals

    return rows


# Human-readable thousands separators.
def fmt_count(n: int) -> str:
    return f"{n:>9,}"


def fmt_pct(p: float) -> str:
    return f"{p:>6.2f}%"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2

    path = find_report(sys.argv[1])
    rows = parse_report(path)
    print(f"Report: {path}")
    print()

    # We expect these rows. "ternip_ip_1" sometimes appears as the kernel
    # instance row -- fold it into the kernel total if both exist.
    required = ["Platform", "User Budget", "Used Resources", "Unused Resources"]
    for r in required:
        if r not in rows:
            print(f"ERROR: row '{r}' not found in {path}", file=sys.stderr)
            return 1

    # Resource columns in order presented.
    cols = list(rows["Platform"].keys())

    print(f"{'Resource':<10} {'Platform (XRT)':>20} {'Kernel (ternip_ip)':>22} {'Free (Unused)':>20} {'Total Chip':>14}")
    print("-" * 95)
    for col in cols:
        plat_n, plat_p = rows["Platform"][col]
        used_n, used_p = rows["Used Resources"][col]
        unused_n, unused_p = rows["Unused Resources"][col]
        budget_n, _ = rows["User Budget"][col]
        total = plat_n + budget_n
        plat_pct_total = (plat_n / total * 100) if total else 0.0
        used_pct_total = (used_n / total * 100) if total else 0.0
        unused_pct_total = (unused_n / total * 100) if total else 0.0
        print(
            f"{col:<10} "
            f"{fmt_count(plat_n)} {fmt_pct(plat_pct_total)}  "
            f"{fmt_count(used_n)} {fmt_pct(used_pct_total)}   "
            f"{fmt_count(unused_n)} {fmt_pct(unused_pct_total)}   "
            f"{fmt_count(total)}"
        )

    # Per-budget view (the % numbers Vivado itself prints).
    print()
    print("Kernel-budget view (matches Vivado's report_accelerator_utilization %):")
    print(f"  ternip_ip uses {rows['Used Resources']['LUT'][1]:.2f}% of LUT budget, "
          f"{rows['Used Resources']['REG'][1]:.2f}% of FF budget, "
          f"{rows['Used Resources']['DSP'][1]:.2f}% of DSP budget, "
          f"{rows['Used Resources']['BRAM'][1]:.2f}% of BRAM budget.")
    print(f"  Headroom in user budget: "
          f"LUT {rows['Unused Resources']['LUT'][1]:.2f}%, "
          f"FF {rows['Unused Resources']['REG'][1]:.2f}%, "
          f"DSP {rows['Unused Resources']['DSP'][1]:.2f}%, "
          f"BRAM {rows['Unused Resources']['BRAM'][1]:.2f}%.")

    # Scaling hint: if the user is targeting BatchSize=N, how many cores
    # fit at current per-core cost?
    print()
    print("Per-resource scaling headroom (assuming current usage is the per-core cost):")
    for col in ("LUT", "REG", "DSP", "BRAM"):
        if col not in rows["Used Resources"]:
            continue
        used = rows["Used Resources"][col][0]
        unused = rows["Unused Resources"][col][0]
        if used == 0:
            print(f"  {col}: kernel uses 0, headroom is unlimited")
        else:
            ratio = unused / used
            print(
                f"  {col}: kernel uses {used:,}, {unused:,} free "
                f"({ratio:.2f}x more would fit)"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
