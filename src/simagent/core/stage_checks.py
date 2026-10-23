"""Assemble the gate checks for a finished stage from its log, energies, and receipts (design 6).

The pure gate functions live in ``gates``; this module reads the files and runs the short
``gmx energy`` and ``gmx check`` commands they need.
"""

from __future__ import annotations

import re
from typing import Any

from simagent.analysis.stats import linear_drift
from simagent.analysis.xvg import parse_xvg
from simagent.config import Config
from simagent.core.gates import (
    GateCheck,
    common_checks,
    gate_em,
    gate_npt,
    gate_nvt,
    gate_prod,
)
from simagent.core.gmx import RunnerLike
from simagent.core.jobs import JobHandle
from simagent.core.mdp import effective_params
from simagent.core.paths import sha256_file
from simagent.errors import ErrorCode, SimAgentError

# [verify TASK-034] the layout of the final EM report and of `gmx check` output
_FMAX = re.compile(r"Maximum force\s*=\s*(\S+)")
_EPOT = re.compile(r"Potential Energy\s*=\s*(\S+)")
_FRAMES = re.compile(r"^Coords\s+(\d+)", re.MULTILINE)
_LAST_FRAME = re.compile(r"Last frame\s+\d+\s+time\s+(\S+)")


def read_atom_count(job: JobHandle) -> int:
    """Atoms in the built box: the second line of ``water.gro``."""
    lines = (job.stage_dir("build") / "water.gro").read_text(encoding="utf-8").splitlines()
    return int(lines[1])


def _number(pattern: re.Pattern[str], text: str) -> float | None:
    match = pattern.search(text)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def receipts_ok(job: JobHandle, stage: str) -> bool:
    """The wrapper's end record exists and every file it lists still has the recorded hash."""
    stage_end = None
    for record in reversed(job.ledger.records()):
        if record.get("event") == "end" and record.get("stage") == stage \
                and record.get("tool") == "run_mdrun":
            stage_end = record
            break
    if stage_end is None or not stage_end.get("outputs"):
        return False
    for entry in stage_end["outputs"]:
        path = job.dir / entry["path"]
        if not path.is_file() or sha256_file(path) != entry["sha256"]:
            return False
    return True


def _series(job: JobHandle, runner: RunnerLike, stage: str,
            term: str) -> tuple[list[float], list[float]]:
    stage_dir = job.stage_dir(stage)
    name = f"{stage}_gate_{re.sub(r'[^A-Za-z0-9]+', '_', term).strip('_')}.xvg"
    result = runner.run(["energy", "-f", f"{stage}.edr", "-o", name], cwd=stage_dir, stage=stage,
                        tool="validate_stage", stdin=f"{term}\n\n", base=job.dir,
                        outputs=[stage_dir / name])
    path = stage_dir / name
    if result.exit_code != 0 or not path.is_file():
        raise SimAgentError(ErrorCode.E_ANALYSIS_INPUT, f"cannot read {term} from {stage}.edr",
                            hint="the stage did not write a usable energy file")
    _, rows = parse_xvg(path.read_text(encoding="utf-8"))
    rows = [r for r in rows if len(r) >= 2]
    if not rows:
        raise SimAgentError(ErrorCode.E_ANALYSIS_INPUT, f"{term} has no data in {stage}.edr",
                            hint="the stage wrote no energy frames")
    return [r[0] for r in rows], [r[1] for r in rows]


def _trajectory(job: JobHandle, runner: RunnerLike, stage: str) -> tuple[int, float]:
    result = runner.run(["check", "-f", f"{stage}.xtc"], cwd=job.stage_dir(stage), stage=stage,
                        tool="validate_stage", base=job.dir)
    text = result.stdout + "\n" + result.stderr
    frames = _FRAMES.search(text)
    last = _number(_LAST_FRAME, text)
    return (int(frames.group(1)) if frames else -1), (last if last is not None else -1.0)


def stage_checks(job: JobHandle, stage: str, cfg: Config, runner: RunnerLike,
                 process_state: str | None) -> list[GateCheck]:
    data: dict[str, Any] = job.read()
    plan, request = data["plan"], data["request"]
    simulated = data["execution_mode"] == "simulated"
    log_path = job.stage_dir(stage) / f"{stage}.log"
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() \
        else None
    acknowledged = {a["id"] for a in data["acknowledged_warnings"]}
    unacknowledged = sum(1 for w in data["pending_warnings"] if w["id"] not in acknowledged)
    nan_found = None if log_text is None else bool(re.search(r"\bnan\b", log_text, re.IGNORECASE))
    checks = common_checks(process_state, receipts_ok(job, stage), nan_found, unacknowledged,
                           simulated)
    params = effective_params(stage, plan, data["retries"])
    gates = cfg.gates
    if stage == "em":
        n_molecules = int(request["system"]["n_molecules"])
        epot = _number(_EPOT, log_text or "")
        checks += gate_em(log_text, params["emtol"], _number(_FMAX, log_text or ""),
                          None if epot is None else epot / n_molecules, gates, simulated)
        return checks
    ref_t = params["ref_t"]
    if stage == "nvt":
        times, temps = ([], []) if simulated else _series(job, runner, stage, "Temperature")
        return checks + gate_nvt(log_text, temps, times, ref_t, gates, simulated)
    ref_p = params.get("ref_p", 1.0)
    if stage == "npt":
        if simulated:
            times, temps, density, pressure = [], [], [], []
        else:
            times, temps = _series(job, runner, stage, "Temperature")
            density = _series(job, runner, stage, "Density")[1]
            pressure = _series(job, runner, stage, "Pressure")[1]
        return checks + gate_npt(log_text, temps, density, pressure, times, ref_t, ref_p, gates,
                                 simulated)
    if simulated:
        return checks + gate_prod(log_text, nsteps=params["nsteps"], dt=params["dt"],
                                  interval_ps=0.5, n_frames=0, last_frame_ps=0.0, temps=[],
                                  density=[], ref_t=ref_t, conserved_drift=None, gates=gates,
                                  ref_p=ref_p, simulated=True)
    times, temps = _series(job, runner, stage, "Temperature")
    density = _series(job, runner, stage, "Density")[1]
    c_times, conserved = _series(job, runner, stage, "Conserved-En.")
    slope, _ = linear_drift(c_times, conserved)
    n_frames, last_frame = _trajectory(job, runner, stage)
    return checks + gate_prod(
        log_text, nsteps=params["nsteps"], dt=params["dt"], interval_ps=0.5, n_frames=n_frames,
        last_frame_ps=last_frame, temps=temps, density=density, ref_t=ref_t,
        conserved_drift=abs(slope) / read_atom_count(job), gates=gates, ref_p=ref_p)



