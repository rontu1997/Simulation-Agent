"""Turn a finished request into a stored plan, show it, and record approval (design 3.2, 10).

The plan the researcher sees is always rendered from the server's copy (``get_job``), and the
approval is recorded for the hash of the copy that was shown, so what is approved is exactly what
the server will run (RULE-C-07).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from simagent.agent.controller import ToolCaller
from simagent.agent.request import ANALYSIS_NAMES, BASELINE, SCIENCE_CRITICAL
from simagent.core.mdp import build_default_plan, derive_seed
from simagent.errors import SimAgentError


class PlanError(Exception):
    """Preparing or approving a plan failed; the message is meant for the researcher."""


@dataclass
class Prepared:
    job_id: str
    request: dict[str, Any]
    plan_sha256: str
    warnings: list[str] = field(default_factory=list)
    execution_mode: str = "real"


def _failure(step: str, envelope: dict[str, Any]) -> PlanError:
    error = envelope.get("error") or {}
    text = f"{step} failed: {error.get('code', 'E_UNKNOWN')}: {error.get('message', '')}"
    return PlanError(f"{text}. {error['hint']}" if error.get("hint") else text)


async def _call(tools: ToolCaller, step: str, args: dict[str, Any]) -> dict[str, Any]:
    envelope = await tools.call(step, args)
    if not envelope["ok"]:
        raise _failure(step, envelope)
    data: dict[str, Any] = envelope["data"]
    return data


def _plan_for(request: dict[str, Any], build: dict[str, Any]) -> dict[str, Any]:
    try:
        return build_default_plan(request, build)
    except SimAgentError as exc:
        raise PlanError(f"the request is not valid: {exc.code.value}: {exc.message}. "
                        f"{exc.hint}") from exc


async def prepare_job(tools: ToolCaller, request: dict[str, Any], *,
                      job_id: str | None = None) -> Prepared:
    """create_job, build, plan, and set_plan; the server stores the plan and returns its hash."""
    warnings: list[str] = []
    if job_id is None:
        created = await _call(tools, "create_job", {"request": request})
        job_id = str(created["job_id"])
        warnings = list(created.get("warnings", []))
    seeded = copy.deepcopy(request)
    if seeded.get("seed") is None:
        seeded["seed"] = derive_seed(job_id)
        seeded.setdefault("provenance_of_fields", {})["seed"] = "default"
    system = seeded["system"]
    build = await _call(tools, "build_water_box", {
        "job_id": job_id, "n_molecules": system["n_molecules"],
        "water_model": system["water_model"]})
    return await _store(tools, job_id, seeded, build, warnings)


async def _store(tools: ToolCaller, job_id: str, request: dict[str, Any],
                 build: dict[str, Any], warnings: list[str]) -> Prepared:
    plan = _plan_for(request, build)
    stored = await _call(tools, "set_plan", {"job_id": job_id, "request": request, "plan": plan})
    job = await _call(tools, "get_job", {"job_id": job_id})
    return Prepared(job_id, request, str(stored["plan_sha256"]), warnings,
                    str(job.get("execution_mode", "real")))


_SYSTEM = ("n_molecules", "water_model")
_THERMO = ("temperature_K", "pressure_bar")
_PROTOCOL = ("prod_ns", "emtol", "nvt_ps", "npt_ps", "dt_ps", "tau_t", "tau_p")


def _apply(request: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(request)
    origin = updated.setdefault("provenance_of_fields", {})
    for name, value in changes.items():
        if name in _SYSTEM:
            updated["system"][name] = value
        elif name in _THERMO:
            updated["thermo"][name] = value
        elif name in _PROTOCOL:
            updated["protocol"][name] = value
        elif name in ("force_field", "seed"):
            updated[name] = value
        elif name == "analyses":
            bad = [a for a in value if a not in ANALYSIS_NAMES]
            if bad:
                raise PlanError(f"unknown analysis: {bad[0]}")
            updated["analyses"] = [*BASELINE, *[a for a in value if a not in BASELINE]]
            for analysis in value:
                origin[f"analyses.{analysis}"] = "user"
            continue
        else:
            raise PlanError(f"cannot change {name}: it is not a plan field")
        origin[name] = "user"
    return updated


async def replan(tools: ToolCaller, prepared: Prepared, changes: dict[str, Any]) -> Prepared:
    """Apply the researcher's edits and store a new plan (the server clears the approval)."""
    request = _apply(prepared.request, changes)
    if any(name in changes for name in _SYSTEM):
        system = request["system"]
        build = await _call(tools, "build_water_box", {
            "job_id": prepared.job_id, "n_molecules": system["n_molecules"],
            "water_model": system["water_model"]})
    else:
        job = await _call(tools, "get_job", {"job_id": prepared.job_id})
        build = next(s["params"] for s in job["plan"]["stages"] if s["name"] == "build")
    return await _store(tools, prepared.job_id, request, build, prepared.warnings)


# -- rendering ---------------------------------------------------------------------------------


def _g(value: Any) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def _list(values: list[Any]) -> str:
    return " or ".join(str(v) for v in values)


def _retry_line(entry: dict[str, Any]) -> str:
    key = entry["key"]
    if "allowed" in entry:
        values = entry["allowed"]
        if not values:
            return f"{key} (none for this plan)"
        return f"EM steps {_list(values)}" if key == "em_nsteps" else (
            f"{key} {_list(values)} only")
    return f"{key} between {_g(entry['min'])} and {_g(entry['max'])}"


def render_plan(job: dict[str, Any]) -> str:
    """The plan table, from a ``get_job`` data dict only."""
    plan, request = job["plan"], job["request"]
    origin = request.get("provenance_of_fields", {})
    stages = {s["name"]: s for s in plan["stages"]}

    def star(name: str) -> str:
        return "*" if origin.get(name, "default") == "default" else ""

    build, em = stages["build"]["params"], stages["em"]["params"]
    nvt, npt, prod = (stages[n]["params"] for n in ("nvt", "npt", "prod"))
    dt = nvt["dt"]
    lines = [f"Plan for {job.get('job_id', request.get('job_id', ''))} "
             f"(* = my default, not your choice; mode: {job.get('execution_mode', 'real')})"]
    lines += [
        f" S1 build    {build['n_molecules']} {build['water_model']} waters, cubic box "
        f"{_g(build['box_nm'])} nm, density {_g(build['target_density_g_cm3'])} g/cm3 start",
        f" S2 em       steepest descent, Fmax < {_g(em['emtol'])}{star('emtol')}",
        f" S3 nvt      {_g(nvt['nsteps'] * dt)} ps{star('nvt_ps')}, {_g(nvt['ref_t'])} K, "
        f"V-rescale*, dt {_g(dt * 1000)} fs{star('dt_ps')}, seed {nvt['seed']}"
        f"{star('seed')}",
        f" S4 npt      {_g(npt['nsteps'] * dt)} ps{star('npt_ps')}, {_g(npt['ref_p'])} bar, "
        f"C-rescale*",
        f" S5 prod     {_g(prod['nsteps'] * dt / 1000)} ns{star('prod_ns')}",
        f" S6 analysis {', '.join(stages['analysis']['params']['analyses'])}",
        " S7 report   report.md",
        "Your science-critical choices:",
    ]
    shown = {
        "n_molecules": build["n_molecules"], "water_model": build["water_model"],
        "force_field": request.get("force_field"),
        "temperature_K": request["thermo"]["temperature_K"],
        "pressure_bar": request["thermo"]["pressure_bar"],
        "prod_ns": request["protocol"]["prod_ns"],
    }
    for name in SCIENCE_CRITICAL[:-1]:
        lines.append(f"  {name:<14}{_g(shown[name]):<10}{origin.get(name, 'user')}")
    retries = plan.get("automatic_retries_allowed", [])
    lines.append("Automatic retries allowed (from the plan): "
                 + ", ".join(_retry_line(e) for e in retries))
    estimates = plan.get("estimates", {})
    tail = f"Estimated disk {_g(estimates.get('disk_mb', '?'))} MB"
    if estimates.get("wall_hours") is not None:
        tail += f", wall time about {_g(estimates['wall_hours'])} h"
    lines.append(tail + ".")
    return "\n".join(lines)


async def record_plan_approval(tools: ToolCaller, token: str, job_id: str, mode: str, *,
                               shown: dict[str, Any] | None = None) -> str:
    """Record approval for the plan hash of the ``get_job`` read that was shown (``shown``), or of
    a fresh read. The token only ever goes to the server."""
    job = shown if shown is not None else await _call(tools, "get_job", {"job_id": job_id})
    digest = str(job["plan_sha256"])
    envelope = await tools.call("record_approval", {
        "job_id": job_id, "approval_token": token, "kind": "plan", "plan_sha256": digest,
        "mode": mode})
    if not envelope["ok"]:
        raise _failure("approval", envelope)
    return digest
