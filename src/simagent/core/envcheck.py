"""gmx_check_environment (design 4.6): identity, version, data files, probe run, execution mode."""

from __future__ import annotations

import os
import re
import shutil
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from simagent.config import Config
from simagent.core.gmx import GmxRunner, RunResult, build_gmx_env
from simagent.core.paths import sha256_file
from simagent.errors import ErrorCode, SimAgentError

_VERSION_RE = re.compile(r"GROMACS version:\s*(\S+)")
_GPU_RE = re.compile(r"GPU support:\s*(\S+)", re.IGNORECASE)  # [verify TASK-034]


class _Runner(Protocol):
    def run(self, args: Sequence[str], *, cwd: Path, stage: str = ..., tool: str = ...,
            stdin: str | None = ..., inputs: Sequence[Path] = ..., outputs: Sequence[Path] = ...,
            base: Path | None = ..., timeout: float | None = ...) -> RunResult: ...


def _parse_version(text: str) -> str | None:
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def check_environment(
    cfg: Config, *,
    version_fn: Callable[[], str] | None = None,
    probe_fn: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    binary = Path(cfg.gmx.bin)
    hint = "set GMX_BIN (or gmx.bin in config.yaml) to the GROMACS executable"
    if not binary.is_file():
        raise SimAgentError(ErrorCode.E_GMX_NOT_FOUND, f"GROMACS binary not found: {binary}",
                            hint=hint)
    if not os.access(binary, os.X_OK):
        raise SimAgentError(ErrorCode.E_GMX_NOT_FOUND,
                            f"GROMACS binary is not executable: {binary}", hint=hint)

    with open(binary, "rb") as handle:
        head = handle.read(4)
    is_elf = head == b"\x7fELF"
    is_script = head[:2] == b"#!"
    if not (is_elf or is_script):
        raise SimAgentError(ErrorCode.E_GMX_NOT_ELF,
                            "GMX_BIN is neither an ELF binary nor a script", hint=hint)
    if is_script and not cfg.allow_fake:
        raise SimAgentError(
            ErrorCode.E_FAKE_BINARY, "GMX_BIN is a script, not a GROMACS binary",
            hint="point GMX_BIN at the real gmx, or set SIMAGENT_ALLOW_FAKE=1 for the test fake")

    digest = sha256_file(binary)
    if cfg.gmx.expected_sha256 and cfg.gmx.expected_sha256 != digest:
        raise SimAgentError(ErrorCode.E_GMX_VERSION,
                            "binary SHA-256 does not match gmx.expected_sha256",
                            hint="update the pin after verifying the new build")

    version_text = (version_fn or (lambda: _default_version(cfg)))()
    version = _parse_version(version_text)
    warnings: list[str] = []
    result: dict[str, Any] = {
        "bin_path": str(binary), "sha256": digest, "is_elf": is_elf, "version": version,
        "ncpu": os.cpu_count(), "env_allow": build_gmx_env(cfg.gmx, os.environ),
        "warnings": warnings,
    }

    if is_script:
        if "FAKE" not in version_text:
            raise SimAgentError(ErrorCode.E_FAKE_BINARY,
                                "script does not identify itself as the test fake",
                                hint="point GMX_BIN at the real gmx")
        result.update(has_oplsaa=None, has_tip4p_itp=None, has_tip4p_gro=None,
                      gpu_detected=False, probe="skipped (simulated)",
                      execution_mode="simulated")
        return result

    if cfg.allow_fake:
        warnings.append("SIMAGENT_ALLOW_FAKE is set but the binary is a real ELF; flag ignored")
    if version is None or not version.startswith(cfg.gmx.expected_version_prefix):
        raise SimAgentError(
            ErrorCode.E_GMX_VERSION,
            f"expected GROMACS {cfg.gmx.expected_version_prefix}, found {version or 'unknown'}",
            hint="use the 2025.3 build or change gmx.expected_version_prefix")

    top = Path(build_gmx_env(cfg.gmx, {})["GMXDATA"]) / "top"
    found = {
        "has_oplsaa": (top / "oplsaa.ff" / "forcefield.itp").is_file(),
        "has_tip4p_itp": (top / "oplsaa.ff" / "tip4p.itp").is_file(),
        "has_tip4p_gro": (top / "tip4p.gro").is_file(),
    }
    missing = [name for name, present in found.items() if not present]
    if missing:
        raise SimAgentError(
            ErrorCode.E_DATA_MISSING, f"GROMACS data files missing under {top}: {missing}",
            hint="check the install prefix of GMX_BIN; GMXLIB may point at extra force fields")

    gpu = _GPU_RE.search(version_text)
    result.update(found)
    result["gpu_detected"] = bool(gpu and gpu.group(1).lower() != "disabled")
    result["probe"] = (probe_fn or (lambda: run_probe(cfg, GmxRunner(cfg))))()
    result["execution_mode"] = "real"
    return result


def _default_version(cfg: Config) -> str:
    runner = GmxRunner(cfg)
    return runner.run(["--version"], cwd=Path(cfg.gmx.bin).parent).stdout


_TOPOL = """#include "oplsaa.ff/forcefield.itp"
#include "oplsaa.ff/tip4p.itp"

[ system ]
probe water

[ molecules ]
SOL  {n}
"""

_MDP = """integrator = md
dt = 0.002
nsteps = 20
cutoff-scheme = Verlet
coulombtype = PME
rcoulomb = 0.6
rvdw = 0.6
tcoupl = no
nstenergy = 10
nstlog = 10
"""


def run_probe(cfg: Config, runner: _Runner) -> dict[str, Any]:
    """Run a tiny real simulation in the server's own environment (design 4.6 step 5)."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    workdir = Path(cfg.paths.runs_dir) / "_probe" / stamp
    workdir.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()

    def step(args: list[str]) -> RunResult:
        result = runner.run(args, cwd=workdir, tool="gmx_check_environment")
        if result.exit_code != 0:
            tail = (result.stderr or result.stdout).strip().splitlines()[-5:]
            raise SimAgentError(
                ErrorCode.E_PROBE_FAILED,
                f"probe step 'gmx {args[0]}' failed: {' | '.join(tail)}",
                hint=f"probe files kept in {workdir}")
        return result

    step(["solvate", "-cs", "tip4p.gro", "-box", "1.5", "1.5", "1.5", "-o", "water.gro"])
    atoms = int((workdir / "water.gro").read_text(encoding="utf-8").splitlines()[1])
    (workdir / "topol.top").write_text(_TOPOL.format(n=atoms // 4), encoding="utf-8")
    (workdir / "md.mdp").write_text(_MDP, encoding="utf-8")
    step(["grompp", "-f", "md.mdp", "-c", "water.gro", "-p", "topol.top", "-o", "probe.tpr"])
    step(["mdrun", "-deffnm", "probe"])
    step(["check", "-e", "probe.edr"])
    log = (workdir / "probe.log").read_text(encoding="utf-8", errors="replace") \
        if (workdir / "probe.log").exists() else ""
    if "Finished mdrun" not in log:  # [verify TASK-013]
        raise SimAgentError(ErrorCode.E_PROBE_FAILED,
                            "probe mdrun log lacks 'Finished mdrun'",
                            hint=f"probe files kept in {workdir}")
    shutil.rmtree(workdir, ignore_errors=True)
    return {"ok": True, "wall_s": round(time.perf_counter() - start, 3)}
