"""Water box builder (design 4.7): exact molecule count, topology, receipts, dry grompp check."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from simagent.core.gmx import RunnerLike
from simagent.core.jobs import PROCESS_STAGES, JobHandle
from simagent.core.mdp import render_mdp
from simagent.core.paths import sha256_file, sha256_text
from simagent.core.provenance import record_internal
from simagent.errors import ErrorCode, SimAgentError

AVOGADRO_FACTOR = 602.2140857  # converts g/cm^3 and g/mol to molecules/nm^3
MOLAR_MASS_WATER = 18.01528
MAX_BISECTION_STEPS = 12

_FORCEFIELD_INCLUDES = {
    "tip4p": ("oplsaa.ff/forcefield.itp", "oplsaa.ff/tip4p.itp"),
    "tip4pew": ("amber99sb-ildn.ff/forcefield.itp", "amber99sb-ildn.ff/tip4pew.itp"),
}


def number_density(density_g_cm3: float) -> float:
    return density_g_cm3 * AVOGADRO_FACTOR / MOLAR_MASS_WATER


def initial_edge(n: int, density_g_cm3: float) -> float:
    return float((n / number_density(density_g_cm3)) ** (1 / 3))


def count_sol(gro_text: str) -> int:
    lines = gro_text.splitlines()
    atoms = lines[2:2 + int(lines[1])]
    return sum(1 for ln in atoms if ln[5:10].strip() == "SOL" and ln[10:15].strip() == "OW")


def find_edge(count_fn: Callable[[float], int], n: int, density_g_cm3: float) -> float:
    """Bisect the box edge; return the smallest tested edge whose molecule count is at least n."""
    edge = initial_edge(n, density_g_cm3)
    lo, hi = 0.9 * edge, 1.1 * edge
    best: float | None = None
    for _ in range(MAX_BISECTION_STEPS):
        count = count_fn(edge)
        if count >= n:
            if best is None or edge < best:
                best = edge
            if count == n:
                break
            hi = edge
        else:
            lo = edge
        edge = (lo + hi) / 2
    if best is None:
        raise SimAgentError(
            ErrorCode.E_BUILD_COUNT, f"no tested box edge held {n} molecules",
            hint="check that gmx solvate works with tip4p.gro; the builder never pads molecules")
    return best


def trim_gro(gro_text: str, n: int) -> str:
    """Keep the first n whole molecules (4 atoms each) and rewrite the atom-count line."""
    lines = gro_text.splitlines()
    natoms = int(lines[1])
    if natoms < 4 * n:
        raise SimAgentError(ErrorCode.E_BUILD_COUNT, f"only {natoms // 4} molecules, need {n}",
                            hint="the builder never pads molecules")
    kept = lines[2:2 + 4 * n]
    box = lines[2 + natoms]
    return "\n".join([lines[0], str(4 * n), *kept, box]) + "\n"


def topology_text(n: int, edge: float, water_model: str = "tip4p") -> str:
    forcefield, water = _FORCEFIELD_INCLUDES[water_model]
    return (f'#include "{forcefield}"\n#include "{water}"\n\n[ system ]\n'
            f"{n} {water_model.upper()} water, box {edge:.4f} nm\n\n[ molecules ]\nSOL  {n}\n")


def _tail(text: str, lines: int = 3) -> str:
    return " | ".join(text.strip().splitlines()[-lines:])


def build_water_box(job: JobHandle, runner: RunnerLike, *, n_molecules: int, water_model: str,
                    target_density_g_cm3: float = 0.997) -> dict[str, Any]:
    data = job.read()
    if any(data["stage_status"][s] == "RUNNING" for s in PROCESS_STAGES):
        raise SimAgentError(ErrorCode.E_MDRUN_RUNNING, "a stage process is running",
                            hint="cancel the run before rebuilding the system")
    if water_model not in _FORCEFIELD_INCLUDES:
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE,
                            f"unsupported water_model: {water_model}",
                            hint="use tip4p (reference) or tip4pew")
    if data["plan"] is not None or data["stage_status"]["build"] != "PENDING":
        job.invalidate("build")
        if data["plan"] is not None:
            job.update(lambda d: d.__setitem__("approval", None))
    job.set_status("build", "RUNNING")
    try:
        result = _build(job, runner, n_molecules, water_model, target_density_g_cm3)
    except SimAgentError as exc:
        cause = exc.code.value
        job.update(lambda d: d["failures"].append({
            "stage": "build", "cause": cause, "time": datetime.now(UTC).isoformat()}))
        job.set_status("build", "FAILED")
        raise
    job.set_status("build", "GATING")
    return result


def _build(job: JobHandle, runner: RunnerLike, n: int, water_model: str,
           density: float) -> dict[str, Any]:
    build_dir = job.stage_dir("build")
    kwargs: dict[str, Any] = {"cwd": build_dir, "stage": "build", "tool": "build_water_box",
                              "base": job.dir}

    def solvate(edge: float) -> int:
        box = f"{edge:.5f}"
        result = runner.run(["solvate", "-cs", "tip4p.gro", "-box", box, box, box,
                             "-o", "water_raw.gro"], **kwargs)
        if result.exit_code != 0:
            raise SimAgentError(ErrorCode.E_BUILD_COUNT,
                                f"gmx solvate failed: {_tail(result.stderr)}",
                                hint="check the GROMACS data directory and tip4p.gro")
        return count_sol((build_dir / "water_raw.gro").read_text(encoding="utf-8"))

    edge = find_edge(solvate, n, density)
    solvate(edge)  # regenerate water_raw.gro at the chosen edge
    raw = build_dir / "water_raw.gro"
    raw.write_text(trim_gro(raw.read_text(encoding="utf-8"), n), encoding="utf-8")
    box = f"{edge:.5f}"
    result = runner.run(["editconf", "-f", "water_raw.gro", "-o", "water.gro",
                         "-box", box, box, box, "-c"], **kwargs)
    if result.exit_code != 0:
        raise SimAgentError(ErrorCode.E_BUILD_COUNT, f"gmx editconf failed: {_tail(result.stderr)}",
                            hint="see the build directory")
    gro = (build_dir / "water.gro").read_text(encoding="utf-8")
    atoms = int(gro.splitlines()[1])
    if atoms != 4 * n or count_sol(gro) != n:
        raise SimAgentError(ErrorCode.E_BUILD_COUNT,
                            f"built {count_sol(gro)} molecules and {atoms} atoms, expected {n}",
                            hint="atoms must equal 4 per SOL (O, H1, H2, virtual site M)")
    (build_dir / "topol.top").write_text(topology_text(n, edge, water_model), encoding="utf-8")
    build_sha = sha256_text(sha256_file(build_dir / "water.gro")
                            + sha256_file(build_dir / "topol.top"))
    info = {
        "box_nm": edge, "n_molecules": n, "n_atoms": atoms, "water_model": water_model,
        "target_density_g_cm3": density, "build_sha256": build_sha,
        "density_g_cm3": n * MOLAR_MASS_WATER / (AVOGADRO_FACTOR * edge ** 3),
    }
    (build_dir / "build.json").write_text(json.dumps(info, indent=2, sort_keys=True),
                                          encoding="utf-8")
    scratch = build_dir / "_dry"
    scratch.mkdir()
    try:
        (scratch / "dry.mdp").write_text(render_mdp("dry", {}, {}), encoding="utf-8")
        dry = runner.run(["grompp", "-f", "dry.mdp", "-c", "../water.gro", "-p", "../topol.top",
                          "-o", "dry.tpr"], **{**kwargs, "cwd": scratch})
        if dry.exit_code != 0:
            raise SimAgentError(ErrorCode.E_GROMPP_FAILED,
                                f"dry grompp check failed: {_tail(dry.stderr)}",
                                hint="the topology and coordinates are inconsistent")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    files = ["01_build/water_raw.gro", "01_build/water.gro", "01_build/topol.top",
             "01_build/build.json"]
    record_internal(job.ledger, stage="build", tool="build_water_box", base=job.dir,
                    outputs=files)
    return {**info, "files": files}
