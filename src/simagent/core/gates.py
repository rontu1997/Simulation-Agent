"""Stage gates (design 6): pure functions that turn measured values into checks.

Thresholds come from ``GatesConfig`` (heuristics for the reference water system). In simulated mode
physical checks report ``skipped_simulated`` and structural checks still run.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from simagent.analysis.stats import block_average, linear_drift
from simagent.config import GatesConfig
from simagent.core.jobs import PROCESS_STAGES, JobHandle
from simagent.errors import ErrorCode, SimAgentError

FAILING = {"fail", "no_receipt", "not_run"}


@dataclass
class GateCheck:
    name: str
    status: str
    value: Any = None
    threshold: Any = None
    evidence_file: str | None = None


def gate_result(checks: Sequence[GateCheck]) -> bool:
    return not any(c.status in FAILING for c in checks)


def _skipped(name: str) -> GateCheck:
    return GateCheck(name, "skipped_simulated")


def _second_half(values: Sequence[float]) -> list[float]:
    return list(values[len(values) // 2:])


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _marker(log_text: str | None, text: str, name: str = "completion_marker") -> GateCheck:
    # [verify TASK-034] the completion marker written by mdrun
    if log_text is None:
        return GateCheck(name, "not_run")
    return GateCheck(name, "pass" if text in log_text else "fail", value=text in log_text,
                     threshold=text)


def _temperature_check(temps: Sequence[float], ref_t: float, tol: float,
                       simulated: bool) -> GateCheck:
    if simulated:
        return _skipped("mean_temperature")
    mean = _mean(_second_half(temps))
    return GateCheck("mean_temperature", "pass" if abs(mean - ref_t) <= tol else "fail",
                     value=mean, threshold=tol)


def _near_ambient(ref_t: float, ref_p: float) -> bool:
    return 280 <= ref_t <= 320 and 0.5 <= ref_p <= 10


def _density_check(mean: float, ref_t: float, ref_p: float, gates: GatesConfig) -> GateCheck:
    if not _near_ambient(ref_t, ref_p):  # no reference density away from ambient conditions
        return GateCheck("mean_density", "warn", value=mean, threshold=None)
    warn_lo, warn_hi = gates.density_warn_g_cm3
    fail_lo, fail_hi = gates.density_fail_g_cm3
    if not fail_lo <= mean <= fail_hi:
        status = "fail"
    elif not warn_lo <= mean <= warn_hi:
        status = "warn"
    else:
        status = "pass"
    return GateCheck("mean_density", status, value=mean,
                     threshold={"warn": [warn_lo, warn_hi], "fail": [fail_lo, fail_hi]})


def common_checks(process_state: str | None, receipts_ok: bool | None, nan_found: bool | None,
                  unacknowledged_warnings: int, simulated: bool = False) -> list[GateCheck]:
    if process_state == "finished":
        process = GateCheck("process_state", "pass", value=process_state)
    elif process_state == "died":
        process = GateCheck("process_died", "fail", value=process_state)
    elif process_state == "timeout":
        process = GateCheck("timeout", "fail", value=process_state)
    elif process_state == "cancelled":
        process = GateCheck("process_cancelled", "fail", value=process_state)
    else:
        process = GateCheck("process_state", "not_run", value=process_state)
    if receipts_ok is None:
        receipts = GateCheck("receipts", "not_run")
    else:
        receipts = GateCheck("receipts", "pass" if receipts_ok else "no_receipt")
    if simulated:
        nan = _skipped("no_nan")
    elif nan_found is None:
        nan = GateCheck("no_nan", "not_run")
    else:
        nan = GateCheck("no_nan", "fail" if nan_found else "pass")
    warnings = GateCheck("grompp_warnings", "fail" if unacknowledged_warnings else "pass",
                         value=unacknowledged_warnings)
    return [process, receipts, nan, warnings]


def gate_build(*, hash_matches_plan: bool, atoms_in_gro: int, n_molecules: int,
               molecules_in_topology: int, build_info_matches_plan: bool, dry_grompp_ok: bool,
               density: float | None, gates: GatesConfig, simulated: bool = False
               ) -> list[GateCheck]:
    checks = [
        GateCheck("build_hash", "pass" if hash_matches_plan else "fail"),
        GateCheck("build_info", "pass" if build_info_matches_plan else "fail"),
        GateCheck("atoms_per_molecule", "pass" if atoms_in_gro == 4 * n_molecules else "fail",
                  value=atoms_in_gro, threshold=4 * n_molecules),
        GateCheck("molecule_count", "pass" if molecules_in_topology == n_molecules else "fail",
                  value=molecules_in_topology, threshold=n_molecules),
        GateCheck("dry_grompp", "pass" if dry_grompp_ok else "fail"),
    ]
    if simulated or density is None:
        checks.append(_skipped("density"))
    else:
        warn_lo, warn_hi = gates.density_warn_g_cm3
        fail_lo, fail_hi = gates.density_fail_g_cm3
        status = ("fail" if not fail_lo <= density <= fail_hi
                  else "warn" if not warn_lo <= density <= warn_hi else "pass")
        checks.append(GateCheck("density", status, value=density,
                                threshold={"warn": [warn_lo, warn_hi], "fail": [fail_lo, fail_hi]}))
    return checks


def gate_em(log_text: str | None, emtol: float, fmax: float | None,
            epot_per_molecule: float | None, gates: GatesConfig,
            simulated: bool = False) -> list[GateCheck]:
    if simulated:
        return [_skipped("converged"), _skipped("epot_finite_negative"), _skipped("epot_range")]
    converged = (log_text is not None and "converged to Fmax" in log_text  # [verify TASK-034]
                 and fmax is not None and fmax < emtol)
    checks = [GateCheck("converged", "pass" if converged else "fail", value=fmax, threshold=emtol)]
    finite = epot_per_molecule is not None and math.isfinite(epot_per_molecule) \
        and epot_per_molecule < 0
    checks.append(GateCheck("epot_finite_negative", "pass" if finite else "fail",
                            value=epot_per_molecule))
    lo, hi = gates.em_epot_per_molecule_warn
    in_range = finite and epot_per_molecule is not None and lo <= epot_per_molecule <= hi
    checks.append(GateCheck("epot_range", "pass" if in_range else "warn",
                            value=epot_per_molecule, threshold=[lo, hi]))
    return checks


def gate_nvt(log_text: str | None, temps: Sequence[float], times: Sequence[float], ref_t: float,
             gates: GatesConfig, simulated: bool = False) -> list[GateCheck]:
    checks = [_marker(log_text, "Finished mdrun"),
              _temperature_check(temps, ref_t, gates.nvt_temp_tol_K, simulated)]
    if simulated:
        checks.append(_skipped("temperature_drift"))
    else:
        slope, _ = linear_drift(times, temps)
        per_100ps = abs(slope) * 100
        status = ("fail" if per_100ps > gates.nvt_temp_drift_fail_K_per_100ps
                  else "warn" if per_100ps > gates.nvt_temp_drift_warn_K_per_100ps else "pass")
        checks.append(GateCheck("temperature_drift", status, value=per_100ps,
                                threshold=[gates.nvt_temp_drift_warn_K_per_100ps,
                                           gates.nvt_temp_drift_fail_K_per_100ps]))
    return checks


def gate_npt(log_text: str | None, temps: Sequence[float], density: Sequence[float],
             pressure: Sequence[float], times: Sequence[float], ref_t: float, ref_p: float,
             gates: GatesConfig, simulated: bool = False) -> list[GateCheck]:
    checks = [_marker(log_text, "Finished mdrun"),
              _temperature_check(temps, ref_t, gates.nvt_temp_tol_K, simulated)]
    if simulated:
        return [*checks, _skipped("mean_density"), _skipped("density_drift"),
                _skipped("mean_pressure")]
    mean_density = _mean(_second_half(density))
    checks.append(_density_check(mean_density, ref_t, ref_p, gates))
    slope, _ = linear_drift(times, density)
    relative = abs(slope) * (times[-1] - times[0]) / mean_density
    checks.append(GateCheck("density_drift", "warn" if relative > 0.01 else "pass",
                            value=relative, threshold=0.01))
    half = _second_half(pressure)
    mean_p, sem = block_average(half, 5)
    tol = 3 * sem + gates.npt_pressure_tol_bar_floor
    checks.append(GateCheck("mean_pressure", "pass" if abs(mean_p - ref_p) <= tol else "warn",
                            value=mean_p, threshold=tol))
    return checks


def gate_prod(log_text: str | None, *, nsteps: int, dt: float, interval_ps: float, n_frames: int,
              last_frame_ps: float, temps: Sequence[float], density: Sequence[float],
              ref_t: float, conserved_drift: float | None, gates: GatesConfig,
              ref_p: float = 1.0, simulated: bool = False) -> list[GateCheck]:
    total = nsteps * dt
    checks = [_marker(log_text, "Finished mdrun")]
    if simulated:
        return [*checks, _skipped("trajectory_length"), _skipped("frame_count"),
                _skipped("mean_temperature"), _skipped("mean_density"),
                _skipped("conserved_drift")]
    checks.append(GateCheck(
        "trajectory_length", "pass" if abs(last_frame_ps - total) <= interval_ps else "fail",
        value=last_frame_ps, threshold=total))
    expected_frames = math.floor(total / interval_ps) + 1
    checks.append(GateCheck("frame_count", "pass" if n_frames == expected_frames else "fail",
                            value=n_frames, threshold=expected_frames))
    checks.append(_temperature_check(temps, ref_t, gates.prod_temp_tol_K, False))
    checks.append(_density_check(_mean(_second_half(density)), ref_t, ref_p, gates))
    if conserved_drift is None:
        checks.append(GateCheck("conserved_drift", "not_run"))
    else:
        warn = conserved_drift > gates.conserved_drift_warn_kJ_mol_ps_atom
        checks.append(GateCheck("conserved_drift", "warn" if warn else "pass",
                                value=conserved_drift,
                                threshold=gates.conserved_drift_warn_kJ_mol_ps_atom))
    return checks


def gate_analysis(requested: Sequence[str], outputs: dict[str, bool], nan_found: bool,
                  summary_valid: bool) -> list[GateCheck]:
    checks = [GateCheck(f"output_{name}", "pass" if outputs.get(name) else "fail")
              for name in requested]
    checks.append(GateCheck("no_nan", "fail" if nan_found else "pass"))
    checks.append(GateCheck("summary_schema", "pass" if summary_valid else "fail"))
    return checks


def apply_validation(job: JobHandle, stage: str, *, process_state: str | None,
                     gate_fn: Callable[[], list[GateCheck]]) -> dict[str, Any]:
    """Run a stage's gate and move the stage through GATING to PASSED or FAILED (design 3.4)."""
    status = job.status(stage)
    if status == "CANCELLED":
        raise SimAgentError(ErrorCode.E_CANCELLED, f"{stage} was cancelled",
                            hint="reset the stage and run it again")

    def report(checks: list[GateCheck], passed: bool) -> dict[str, Any]:
        return {"passed": passed, "checks": [asdict(c) for c in checks],
                "warnings": [c.name for c in checks if c.status == "warn"]}

    def record_failure(cause: str) -> None:
        job.update(lambda d: d["failures"].append(
            {"stage": stage, "cause": cause, "time": datetime.now(UTC).isoformat()}))

    if status in ("PENDING", "READY"):
        return report([GateCheck("stage_ran", "not_run", value=status)], False)
    if stage in PROCESS_STAGES and status == "RUNNING":
        if process_state in ("died", "timeout"):
            name = "process_died" if process_state == "died" else "timeout"
            job.set_status(stage, "FAILED")
            record_failure(name)
            return report([GateCheck(name, "fail", value=process_state)], False)
        if process_state != "finished":
            return report([GateCheck("process_state", "not_run", value=process_state)], False)
        job.set_status(stage, "GATING")
        status = "GATING"
    elif status == "RUNNING":  # build or analysis still in progress
        return report([GateCheck("stage_running", "not_run")], False)
    checks = gate_fn()
    passed = gate_result(checks)
    if status == "GATING":
        job.set_status(stage, "PASSED" if passed else "FAILED")
        if not passed:
            record_failure("gate failed: " + ", ".join(
                c.name for c in checks if c.status in FAILING))
    return report(checks, passed)
