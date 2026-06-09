"""Throughput math — variant-dispatched subprocess of report_instruction_timing.py.

Each architecture variant has its own ``sw_utils/target/report_instruction_timing.py``
under ``architectures/<variant>/``. The script's Config class differs across
variants (NumSeparateAxiInstances vs NumDdrBanksPerTmatmul vs
NumTmatmulBanksPerCore branches), and only the newest branch has
``Config.from_dict``. To keep the visualizer variant-correct without
patching the older submodules, we generate a temp ``.svh`` from the slider
values and subprocess each variant's own script.

Subprocess cost is ~1-2s per "Compute tokens/sec" click — acceptable for
an on-demand button.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from .api import ArchVariant, ThroughputResult


# architecture_visualizer/av_lib/throughput.py -> architecture_visualizer/
_PKG_ROOT = Path(__file__).resolve().parent.parent
_ARCHITECTURES_DIR = _PKG_ROOT / "architectures"


# Variant -> .svh field name that holds the N-replication factor.
_VARIANT_N_PARAM = {
    "NumSeparateAxiInstances":   "NumSeparateAxiInstances",
    "NumDdrBanksPerTmatmul":     "NumDdrBanksPerTmatmul",
    "NumTmatmulBanksPerCore":    "NumTmatmulBanksPerCore",
}


def _render_svh(config: dict, variant: ArchVariant) -> str:
    """Render a config dict as a minimal .svh file the report script can parse."""
    n = int(config["NumDdrBanksUsed"])
    variant_n_field = _VARIANT_N_PARAM[variant]
    lines = [
        f'localparam string Part = "{config.get("Part", "xcu250-figd2104-2L-e")}";',
        "",
        f'localparam int D = {int(config["D"])};',
        f'localparam int TmatmulParallelism = {int(config["TmatmulParallelism"])};',
        f'localparam int VectorParallelism = {int(config["VectorParallelism"])};',
        f'localparam int LutParallelism = {int(config.get("LutParallelism", 1))};',
        "",
        f'localparam int FixedPointPrecision = {int(config["FixedPointPrecision"])};',
        f'localparam int FixedPointExponent = {int(config.get("FixedPointExponent", -5))};',
        "",
        "parameter mul_impl_e MultiplicationImplementation = MUL_STAR;",
        "parameter div_impl_e DivisionImplementation = DIV_BSG;",
        "",
        f'localparam bit UseHardSigmoid = 1;',
        "",
        f'localparam int BatchSize = {int(config["BatchSize"])};',
        "",
        f'localparam int NumVectorRegisters = {int(config.get("NumVectorRegisters", 4))};',
        f'localparam int ImmediateWidth = {int(config.get("ImmediateWidth", 16))};',
        f'localparam int DdrAddressWidth = {int(config.get("DdrAddressWidth", 64))};',
        f'localparam int InstructionWidth = {int(config["InstructionWidth"])};',
        "",
        f'localparam int DdrDataWidth = {int(config["DdrDataWidth"])};',
        f'localparam int InstrFetchWidth = {int(config.get("InstrFetchWidth", 32))};',
        f'localparam int {variant_n_field} = {n};',
        f'localparam int CoreInterconnectNumStages = '
        f'{int(config.get("CoreInterconnectNumStages", 8))};',
        "",
        "localparam real ClockPeriod = 3.333 * 10.0**-9; // 300MHz",
        "",
        "localparam real DramMaxBytesPerSecond = 8 * 2400.0 * 10**6;",
        f'localparam int DramNumBanks = {int(config.get("DramNumBanks", 4))};',
        "",
    ]
    return "\n".join(lines)


_SINGLECORE_RE = re.compile(
    r"^\s*singlecore\s+tokens_per_second\s+at\s+([\d.]+)\s*MHz\s*=\s*([\d.eE+-]+)"
)
_MULTICORE_RE = re.compile(
    r"^\s*multicore\s+tokens_per_second\s+at\s+([\d.]+)\s*MHz\s*=\s*([\d.eE+-]+)"
)


def compute_tokens_per_sec(
    config: dict,
    model: str = "MMfreeLM-370M",
    variant: ArchVariant = "NumTmatmulBanksPerCore",
) -> ThroughputResult:
    """Estimate tokens/sec for the given config + model + architecture variant.

    Writes a temp .svh from ``config``, then subprocesses
    ``architectures/<variant>/sw_utils/target/report_instruction_timing.py``
    against that file and parses its stdout for the singlecore/multicore
    tokens-per-second lines.

    The variant's own throughput math is used — each branch has its own
    AlgorithmTree, so the result is variant-correct.
    """
    if variant not in _VARIANT_N_PARAM:
        raise ValueError(f"unknown variant: {variant!r}")

    variant_dir = _ARCHITECTURES_DIR / variant
    sw_utils_dir = variant_dir / "sw_utils"
    script = sw_utils_dir / "target" / "report_instruction_timing.py"
    if not script.exists():
        raise FileNotFoundError(f"report_instruction_timing.py not found: {script}")

    svh_text = _render_svh(config, variant)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".svh", delete=False, encoding="utf-8"
    ) as fp:
        fp.write(svh_text)
        svh_path = fp.name

    try:
        proc = subprocess.run(
            ["python3", str(script), svh_path, model],
            cwd=str(sw_utils_dir / "target"),
            env={"PYTHONPATH": str(sw_utils_dir), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        Path(svh_path).unlink(missing_ok=True)

    if proc.returncode != 0:
        raise RuntimeError(
            f"report_instruction_timing.py exited {proc.returncode}\n"
            f"--- stderr ---\n{proc.stderr[-2000:]}\n"
            f"--- stdout (tail) ---\n{proc.stdout[-2000:]}"
        )

    singlecore = multicore = clk_freq_mhz = None
    for line in proc.stdout.splitlines():
        m = _SINGLECORE_RE.match(line)
        if m:
            clk_freq_mhz = float(m.group(1))
            singlecore = float(m.group(2))
            continue
        m = _MULTICORE_RE.match(line)
        if m:
            clk_freq_mhz = float(m.group(1))
            multicore = float(m.group(2))
            continue

    if singlecore is None or multicore is None or clk_freq_mhz is None:
        raise RuntimeError(
            "could not parse tokens_per_second from script output:\n"
            + proc.stdout[-2000:]
        )

    return {
        "singlecore": singlecore,
        "multicore": multicore,
        "clk_freq_mhz": clk_freq_mhz,
    }
