"""MDP rendering, range validation, the default plan, and bounded retries (design 3, 5, 4.3).

Parameter values come only from the stored plan (plus the plan's bounded retries), never from free
overrides, so what runs is what was approved (RULE-X-12).
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Sequence
from typing import Any

import jinja2

from simagent.core.jobs import STAGES
from simagent.errors import ErrorCode, SimAgentError

EXAMPLE_JOB_ID = "water-1000-tip4p-300K-20261023-1"
NUMBER_DENSITY_PER_NM3 = 33.3  # molecules per nm^3 for the minimum-size rule
CUTOFF_NM = 1.0
MIN_MOLECULES = max(100, math.ceil(NUMBER_DENSITY_PER_NM3 * (2 * CUTOFF_NM + 0.1) ** 3))  # 309
BASELINE_ANALYSES = ("temperature", "pressure", "density", "energy")
MAX_RETRIES_PER_STAGE = 2

DEFAULT_PROTOCOL: dict[str, float] = {
    "emtol": 1000.0, "nvt_ps": 100, "npt_ps": 100, "prod_ns": 1.0, "dt_ps": 0.002,
    "tau_t": 0.1, "tau_p": 2.0,
}

_ENV = jinja2.Environment(
    loader=jinja2.PackageLoader("simagent", "templates/mdp"),
    undefined=jinja2.StrictUndefined, keep_trailing_newline=True, autoescape=False)


def derive_seed(job_id: str) -> int:
    """Default seed: first 8 hex characters of SHA-256 of the UTF-8 job id, modulo 2**31."""
    return int(hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:8], 16) % (2 ** 31)


def _bad(field_name: str, value: Any, lo: Any, hi: Any) -> SimAgentError:
    return SimAgentError(
        ErrorCode.E_PARAM_OUT_OF_RANGE, f"{field_name} = {value} is outside {lo} to {hi}",
        hint=f"choose {field_name} between {lo} and {hi} (design 5.3)")


def _check(field_name: str, value: Any, lo: float, hi: float) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not lo <= value <= hi:
        raise _bad(field_name, value, lo, hi)


def protocol_of(request: dict[str, Any]) -> dict[str, float]:
    return {**DEFAULT_PROTOCOL, **request.get("protocol", {})}


def validate_request(request: dict[str, Any]) -> list[str]:
    """Hard ranges of design 5.3; returns the warning-band messages."""
    warnings: list[str] = []
    system = request.get("system", {})
    thermo = request.get("thermo", {})
    proto = protocol_of(request)
    if system.get("kind", "water") != "water":
        raise _bad("system.kind", system.get("kind"), "water", "water")
    model = system.get("water_model")
    force_field = request.get("force_field", "oplsaa")
    if model == "tip4p":
        if force_field != "oplsaa":
            raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE,
                                "tip4p is only supported with the oplsaa force field",
                                hint="use force_field oplsaa for tip4p")
    elif model == "tip4pew":
        if not str(force_field).startswith("amber99sb-ildn"):
            raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE,
                                "tip4pew is only accepted with an amber99sb-ildn force field",
                                hint="use force_field amber99sb-ildn")
    else:
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE, f"unsupported water_model: {model}",
                            hint="use tip4p (reference) or tip4pew")
    n = system.get("n_molecules")
    _check("n_molecules", n, MIN_MOLECULES, 20000)
    _check("temperature_K", thermo.get("temperature_K"), 200, 450)
    _check("pressure_bar", thermo.get("pressure_bar"), 0.1, 5000)
    _check("dt_ps", proto["dt_ps"], 0.0005, 0.002)
    _check("prod_ns", proto["prod_ns"], 0.01, 100)
    _check("tau_t", proto["tau_t"], 0.05, 2.0)
    _check("tau_p", proto["tau_p"], 1.0, 10.0)
    _check("emtol", proto["emtol"], 10, 5000)
    _check("nvt_ps", proto["nvt_ps"], 10, 10000)
    _check("npt_ps", proto["npt_ps"], 10, 10000)
    seed = request.get("seed")
    if seed is not None and (not isinstance(seed, int) or isinstance(seed, bool) or seed < 0):
        raise _bad("seed", seed, 0, "2**31 - 1")
    temperature = thermo["temperature_K"]
    if not 260 <= temperature <= 370:
        warnings.append(f"temperature_K {temperature} is outside 260 to 370 K for liquid water")
    if not 0.5 <= thermo["pressure_bar"] <= 2000:
        warnings.append(f"pressure_bar {thermo['pressure_bar']} is outside 0.5 to 2000 bar")
    if n > 5000:
        warnings.append(f"n_molecules {n} is above 5000; runs will be slow")
    if proto["prod_ns"] > 10:
        warnings.append("production above 10 ns needs long_run_confirmed at approval")
    if proto["tau_t"] < 0.1:
        warnings.append("tau_t below 0.1 ps")
    return warnings


def _steps(ps: float, dt: float) -> int:
    return round(ps / dt)


def build_default_plan(request: dict[str, Any], build: dict[str, Any]) -> dict[str, Any]:
    """The seven-stage plan of design 3.2 for a validated request and a finished build."""
    validate_request(request)
    seed = request.get("seed")
    if seed is None:
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE, "seed is not set",
                            hint="derive the default seed from the job id before planning")
    proto = protocol_of(request)
    dt = float(proto["dt_ps"])
    temperature = float(request["thermo"]["temperature_K"])
    pressure = float(request["thermo"]["pressure_bar"])
    tau_t, tau_p = float(proto["tau_t"]), float(proto["tau_p"])
    emtol = float(proto["emtol"])
    named = list(request.get("analyses", []))
    analyses = [*BASELINE_ANALYSES, *[a for a in named if a not in BASELINE_ANALYSES]]
    wet = {"dt": dt, "ref_t": temperature, "tau_t": tau_t}
    npt_like = {**wet, "ref_p": pressure, "tau_p": tau_p}
    stages = [
        {"id": "S1", "name": "build", "params": {
            "n_molecules": request["system"]["n_molecules"],
            "water_model": request["system"]["water_model"],
            "target_density_g_cm3": build.get("target_density_g_cm3", 0.997),
            "box_nm": build["box_nm"], "build_sha256": build["build_sha256"]},
         "why": "Cubic box of the requested water molecules at about the liquid density."},
        {"id": "S2", "name": "em", "params": {"emtol": emtol, "nsteps": 50000},
         "why": "Steepest descent removes bad contacts before dynamics."},
        {"id": "S3", "name": "nvt", "params": {
            "nsteps": _steps(float(proto["nvt_ps"]), dt), **wet, "seed": seed},
         "why": "Equilibrate temperature with V-rescale at constant volume."},
        {"id": "S4", "name": "npt", "params": {
            "nsteps": _steps(float(proto["npt_ps"]), dt), **npt_like},
         "why": "Equilibrate density with C-rescale at constant pressure."},
        {"id": "S5", "name": "prod", "params": {
            "nsteps": _steps(float(proto["prod_ns"]) * 1000, dt), **npt_like},
         "why": "Production run for the requested length."},
        {"id": "S6", "name": "analysis", "params": {"discard_ps": 50, "analyses": analyses},
         "why": "Block-averaged statistics and the requested structural analyses."},
        {"id": "S7", "name": "report", "params": {}, "why": "Report built from trusted results."},
    ]
    retries: list[dict[str, Any]] = [
        {"key": "emtol", "min": 100, "max": emtol},
        {"key": "dt", "allowed": [0.001] if dt > 0.001 else []},
        {"key": "tau_p", "allowed": [5.0] if tau_p < 5.0 else []},
        {"key": "em_nsteps", "allowed": [100000, 200000]},
    ]
    return {"stages": stages, "automatic_retries_allowed": retries,
            "estimates": {"wall_hours": None, "disk_mb": 120},
            "execution_mode_expected": "real"}


def stage_params(plan: dict[str, Any], name: str) -> dict[str, Any]:
    for stage in plan["stages"]:
        if stage["name"] == name:
            params: dict[str, Any] = stage["params"]
            return params
    raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE, f"plan has no stage {name}",
                        hint="a plan needs the seven stages S1 to S7")


def validate_plan(plan: dict[str, Any], request: dict[str, Any]) -> None:
    validate_request(request)
    names = [s.get("name") for s in plan.get("stages", [])]
    if names != ["build", "em", "nvt", "npt", "prod", "analysis", "report"]:
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE, f"plan stages are {names}",
                            hint="a plan needs build, em, nvt, npt, prod, analysis, report")
    system = request["system"]
    build = stage_params(plan, "build")
    if (build["n_molecules"], build["water_model"]) != (system["n_molecules"],
                                                        system["water_model"]):
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE,
                            "plan build parameters differ from the request",
                            hint="the plan must describe the requested system")
    em = stage_params(plan, "em")
    _check("emtol", em["emtol"], 10, 5000)
    _check("em nsteps", em["nsteps"], 1000, 1000000)
    temperature = request["thermo"]["temperature_K"]
    pressure = request["thermo"]["pressure_bar"]
    for name in ("nvt", "npt", "prod"):
        p = stage_params(plan, name)
        _check(f"{name} dt", p["dt"], 0.0005, 0.002)
        _check(f"{name} tau_t", p["tau_t"], 0.05, 2.0)
        _check(f"{name} nsteps", p["nsteps"], 1, 10 ** 9)
        if p["ref_t"] != temperature:
            raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE,
                                f"{name} ref_t {p['ref_t']} differs from the request {temperature}",
                                hint="the plan must use the requested temperature")
        if name != "nvt":
            _check(f"{name} tau_p", p["tau_p"], 1.0, 10.0)
            if p["ref_p"] != pressure:
                raise SimAgentError(
                    ErrorCode.E_PARAM_OUT_OF_RANGE,
                    f"{name} ref_p {p['ref_p']} differs from the request {pressure}",
                    hint="the plan must use the requested pressure")
    prod = stage_params(plan, "prod")
    _check("production length (ns)", prod["nsteps"] * prod["dt"] / 1000, 0.01, 100)
    dt = stage_params(plan, "nvt")["dt"]
    for entry in plan.get("automatic_retries_allowed", []):
        if entry.get("key") == "dt":
            for value in entry.get("allowed", []):
                ratio = dt / value
                if value >= dt or abs(ratio - round(ratio)) > 1e-9:
                    raise SimAgentError(
                        ErrorCode.E_PARAM_OUT_OF_RANGE,
                        f"retry dt {value} does not divide the planned dt {dt} into an integer",
                        hint="allowed retry time steps must be smaller integer fractions of dt")


def rescale_for_dt(params: dict[str, Any], new_dt: float) -> dict[str, Any]:
    """Scale nsteps by dt_old/dt_new so simulated time is preserved."""
    ratio = params["dt"] / new_dt
    return {**params, "dt": new_dt, "nsteps": round(params["nsteps"] * ratio)}


def effective_params(stage: str, plan: dict[str, Any],
                     retries: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    params = dict(stage_params(plan, stage))
    for retry in retries:
        key, value, target = retry["key"], retry["value"], retry["stage"]
        if key == "emtol" and stage == "em":
            params["emtol"] = float(value)
        elif key == "em_nsteps" and stage == "em":
            params["nsteps"] = int(value)
        elif key == "dt" and stage in ("nvt", "npt", "prod") and \
                STAGES.index(stage) >= STAGES.index(target):
            params = rescale_for_dt(params, float(value))
        elif key == "tau_p" and stage in ("npt", "prod") and \
                STAGES.index(stage) >= STAGES.index(target):
            params["tau_p"] = float(value)
    return params


def render_mdp(stage: str, plan: dict[str, Any], request: dict[str, Any],
               retries: Sequence[dict[str, Any]] = ()) -> str:
    if stage == "dry":
        return _ENV.get_template("dry.mdp.j2").render()
    p = effective_params(stage, plan, retries)
    if stage == "em":
        return _ENV.get_template("em.mdp.j2").render(emtol=p["emtol"], em_nsteps=p["nsteps"])
    dt = p["dt"]
    context = {
        "dt": dt, "nsteps": p["nsteps"], "T": p["ref_t"], "tau_t": p["tau_t"],
        "P": p.get("ref_p"), "tau_p": p.get("tau_p"), "seed": p.get("seed"),
        "nstxout": round(0.5 / dt), "nstenergy": round(0.2 / dt), "nstlog": round(1.0 / dt),
    }
    return _ENV.get_template(f"{stage}.mdp.j2").render(**context)


def check_retry(plan: dict[str, Any], state: dict[str, Any], key: str, value: float,
                failing_stage: str | None, target_stage: str | None = None) -> str:
    """Validate a retry against the plan's bounds and the recorded failures.

    Returns the failing stage the retry is attributed to.
    """
    entry = next((e for e in plan.get("automatic_retries_allowed", []) if e.get("key") == key),
                 None)
    not_allowed = ErrorCode.E_RETRY_NOT_ALLOWED
    if entry is None:
        raise SimAgentError(not_allowed, f"{key} is not an allowed automatic retry",
                            hint="only the retries listed in the approved plan are allowed")
    if "allowed" in entry and value not in entry["allowed"]:
        raise SimAgentError(not_allowed, f"{key}={value} is not in the allowed values "
                            f"{entry['allowed']}", hint="use a value from the approved plan")
    if ("min" in entry and value < entry["min"]) or ("max" in entry and value > entry["max"]):
        raise SimAgentError(not_allowed, f"{key}={value} is outside the approved bounds",
                            hint="a retry may only make the stage more conservative")
    failures = state.get("failures", [])
    if not failures:
        raise SimAgentError(not_allowed, "no failure is recorded",
                            hint="a retry is only allowed after a stage has failed")
    counts = Counter(r["failing_stage"] for r in state.get("retries", []))
    failed = [f["stage"] for f in failures]
    if failing_stage is not None:
        if failing_stage not in failed:
            raise SimAgentError(not_allowed, f"no failure is recorded for {failing_stage}",
                                hint="name a stage that has failed")
        attributed = failing_stage
        if counts[attributed] >= MAX_RETRIES_PER_STAGE:
            raise SimAgentError(ErrorCode.E_RETRY_LIMIT,
                                f"{attributed} already used {MAX_RETRIES_PER_STAGE} retries",
                                hint="ask the researcher to decide")
    else:
        open_stages = [s for s in reversed(failed) if counts[s] < MAX_RETRIES_PER_STAGE]
        if not open_stages:
            raise SimAgentError(ErrorCode.E_RETRY_LIMIT, "every failed stage used its retries",
                                hint="ask the researcher to decide")
        attributed = open_stages[0]
    if target_stage is not None and STAGES.index(target_stage) > STAGES.index(attributed):
        raise SimAgentError(not_allowed,
                            f"a retry may not target {target_stage}, which is after {attributed}",
                            hint="retry the failing stage or an earlier one")
    return attributed
