# ternip_claude_churner

Self-driving Claude harness for FPGA timing optimization on the ternary-
matmul project. A `claude --dangerously-skip-permissions` instance reads
[CLAUDE.md](CLAUDE.md), picks one RTL improvement, builds it on the AU250
through Vitis, records the result as a tagged GitHub release, and loops —
for days, unattended.

## Run

```bash
claude --dangerously-skip-permissions
```

That's it. Walk away and check back later. Each iteration creates a new
release with full timing data, a build tarball, and a short summary.

## Prerequisites

- `gh` (GitHub CLI) installed and `gh auth login` done. Needed for
  `gh release create/edit/upload`. Install: `apt install gh`,
  `dnf install gh`, `conda install gh`, or the GitHub-released binary
  from <https://github.com/cli/cli/releases>.
- `git`, `ssh eq2` working, and the usual ternary-matmul toolchain
  (sv2v, verilator, vcs, vivado) on PATH.
- The two submodule forks already pushed (we did this during repo seeding;
  see the git history if you need to redo).

## Layout

```
ternip_claude/
├─ ternary_matmul/        # private fork submodule; has third_party/ternip nested
├─ references/            # docs + example projects (submodules)
│  ├─ vivado-docs-2023.1/
│  ├─ lowRISC-style-guide/
│  ├─ firesim/  RapidStream/  tapa/  corundum/
│  └─ philipabbey-blog-projects/  SAR-ATR-on-FPGA/
├─ .claude/skills/        # yosys-fanout, vivado-read-reports, vivado-utilization
├─ scripts/               # build/poll/collect/release helpers
├─ CLAUDE.md              # autonomous-loop instructions (read on every claude session)
├─ STYLE.md               # SV coding rules
└─ README.md              # this file
```

## Documents

- **[CLAUDE.md](CLAUDE.md)** — the operational manual. Loop algorithm,
  verification, error handling, what's been tried, what to try next.
- **[STYLE.md](STYLE.md)** — SV coding rules: ready/valid everywhere,
  `_d`/`_q` suffixes, prefer existing modules, etc.
