"""Plan hashing, set_plan, and server-side approval (design 4.3).

The model never holds the approval token; the CLI records approval for the hash it read back from
the server, and ``run_mdrun`` refuses to start without a matching approval.
"""

from __future__ import annotations

import hmac
import json
import re
from datetime import UTC, datetime
from typing import Any

from simagent.config import Config
from simagent.core.gates import GateCheck, apply_validation, gate_build
from simagent.core.jobs import PROCESS_STAGES, JobHandle
from simagent.core.mdp import stage_params, validate_plan
from simagent.core.paths import sha256_file, sha256_text
from simagent.core.provenance import record_ack
from simagent.errors import ErrorCode, SimAgentError

_COMPARED_STAGES = ("em", "nvt", "npt", "prod", "analysis")


def plan_sha256(request: dict[str, Any], plan: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of what the researcher approves (design 3.2)."""
    payload = {
        "request": request,
        "analyses": request.get("analyses"),
        "stages": [{"id": s["id"], "name": s["name"], "params": s["params"]}
                   for s in plan["stages"]],
        "automatic_retries_allowed": plan.get("automatic_retries_allowed", []),
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256_text(text)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _build_gate(job: JobHandle, plan: dict[str, Any], gates: Any,
                simulated: bool) -> list[GateCheck]:
    build_dir = job.stage_dir("build")
    declared = stage_params(plan, "build")
    info = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    water, top = build_dir / "water.gro", build_dir / "topol.top"
    actual = sha256_text(sha256_file(water) + sha256_file(top))
    atoms = int(water.read_text(encoding="utf-8").splitlines()[1])
    match = re.search(r"^SOL\s+(\d+)", top.read_text(encoding="utf-8"), re.MULTILINE)
    return gate_build(
        hash_matches_plan=actual == declared["build_sha256"],
        atoms_in_gro=atoms, n_molecules=declared["n_molecules"],
        molecules_in_topology=int(match.group(1)) if match else -1,
        build_info_matches_plan=(info.get("build_sha256") == declared["build_sha256"]
                                 and info.get("n_molecules") == declared["n_molecules"]),
        dry_grompp_ok=info.get("dry_grompp_ok") is True,
        density=info.get("density_g_cm3"), gates=gates, simulated=simulated)


def set_plan(job: JobHandle, cfg: Config, request: dict[str, Any],
             plan: dict[str, Any]) -> dict[str, Any]:
    data = job.read()
    if any(data["stage_status"][s] == "RUNNING" for s in PROCESS_STAGES):
        raise SimAgentError(ErrorCode.E_MDRUN_RUNNING, "a stage process is running",
                            hint="cancel the run before changing the plan")
    validate_plan(plan, request)
    seed = request.get("seed")
    if seed is None or stage_params(plan, "nvt").get("seed") != seed:
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE,
                            "the request seed is missing or differs from the plan seed",
                            hint="derive the seed from the job id and use it in both")
    build_status = data["stage_status"]["build"]
    if build_status in ("PENDING", "RUNNING") or not (
            job.stage_dir("build") / "build.json").is_file():
        raise SimAgentError(ErrorCode.E_STAGE_ORDER, "no finished build exists for this job",
                            hint="call build_water_box before set_plan")
    if build_status not in ("GATING", "PASSED"):
        raise SimAgentError(ErrorCode.E_STAGE_STATE, f"build is {build_status}",
                            hint="rebuild the system with build_water_box")
    declared = stage_params(plan, "build")
    info = json.loads((job.stage_dir("build") / "build.json").read_text(encoding="utf-8"))
    if (info.get("n_molecules"), info.get("water_model")) != (
            declared["n_molecules"], declared["water_model"]) or \
            abs(info.get("box_nm", 0) - declared["box_nm"]) > 1e-9:
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE,
                            "the plan's build parameters differ from build.json",
                            hint="declare the molecule count, model, and box the build produced")
    simulated = data["execution_mode"] == "simulated"
    validation = apply_validation(
        job, "build", process_state=None,
        gate_fn=lambda: _build_gate(job, plan, cfg.gates, simulated))
    if not validation["passed"]:
        raise SimAgentError(ErrorCode.E_GATE_FAILED, "the build gate failed",
                            hint="rebuild the system or correct the declared build hash",
                            data={"checks": validation["checks"]})

    digest = plan_sha256(request, plan)
    invalidated: list[str] = []
    changed: set[str] = set()
    old_plan = data["plan"]
    if old_plan is not None:
        changed = {n for n in _COMPARED_STAGES
                   if stage_params(old_plan, n) != stage_params(plan, n)}
        first = next((n for n in _COMPARED_STAGES if n in changed), None)
        if first is not None:
            invalidated = job.invalidate(first)

    def store(d: dict[str, Any]) -> None:
        d["request"] = request
        d["plan"] = plan
        d["plan_sha256"] = digest
        d["approval"] = None
        d["acknowledged_warnings"] = []
        d["pending_warnings"] = []
        # design 4.3 item 7: a stage whose params changed starts again without old failures or
        # retries, so the approved plan alone decides what runs
        d["failures"] = [f for f in d["failures"] if f["stage"] not in changed]
        d["retries"] = [r for r in d["retries"]
                        if r["stage"] not in changed and r["failing_stage"] not in changed]

    job.update(store)
    return {"plan_sha256": digest, "invalidated": invalidated}


def record_approval(job: JobHandle, cfg: Config, *, token: str, kind: str,
                    plan_sha256: str | None = None, warning_ids: list[str] | None = None,
                    mode: str = "interactive", long_run_confirmed: bool = False,
                    note: str = "") -> dict[str, Any]:
    if cfg.approval_token is None:
        raise SimAgentError(ErrorCode.E_APPROVAL_UNAVAILABLE,
                            "this server was started without an approval token",
                            hint="start the server through the SimAgent CLI")
    if not hmac.compare_digest(token.encode(), cfg.approval_token.encode()):
        raise SimAgentError(ErrorCode.E_BAD_TOKEN, "the approval token is wrong",
                            hint="only the CLI that launched the server holds the token")
    data = job.read()
    if data["plan_sha256"] is None:
        raise SimAgentError(ErrorCode.E_STAGE_ORDER, "no plan is stored for this job",
                            hint="call set_plan first")
    now = _now()
    if kind == "plan":
        if mode not in ("interactive", "auto"):
            raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE, f"unknown approval mode: {mode}",
                                hint="mode is interactive or auto")
        if plan_sha256 != data["plan_sha256"]:
            raise SimAgentError(ErrorCode.E_PLAN_MISMATCH,
                                "the approved hash differs from the stored plan",
                                hint="read the plan with get_job and approve that hash")
        job.update(lambda d: d.__setitem__("approval", {
            "plan_sha256": plan_sha256, "time": now, "mode": mode,
            "long_run_confirmed": long_run_confirmed}))
        return {"approved_at": now}
    if kind != "warnings":
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE, f"unknown approval kind: {kind}",
                            hint="kind is plan or warnings")
    pending = {w["id"]: w for w in data["pending_warnings"]}
    ids = warning_ids or []
    unknown = [i for i in ids if i not in pending]
    if not ids or unknown:
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE,
                            f"unknown or missing warning ids: {unknown or 'none given'}",
                            hint="acknowledge ids returned by run_grompp")

    def acknowledge(d: dict[str, Any]) -> None:
        known = {a["id"] for a in d["acknowledged_warnings"]}
        for warning_id in ids:
            if warning_id not in known:
                d["acknowledged_warnings"].append({
                    "id": warning_id, "text": pending[warning_id]["text"], "time": now,
                    "note": note})

    job.update(acknowledge)
    for warning_id in ids:
        record_ack(job.ledger, warning_id=warning_id, text=pending[warning_id]["text"],
                   note=note)
    return {"approved_at": now}


def require_run_approval(job: JobHandle, cfg: Config, planned_prod_ns: float) -> None:
    data = job.read()
    if cfg.approval_token is None:
        if not cfg.allow_unapproved:
            raise SimAgentError(
                ErrorCode.E_APPROVAL_UNAVAILABLE,
                "this server cannot start runs without the CLI's approval token",
                hint="use the SimAgent CLI, or ask the operator to set SIMAGENT_ALLOW_UNAPPROVED=1")
        if data["plan_sha256"] is None:
            raise SimAgentError(ErrorCode.E_STAGE_ORDER, "no plan is stored for this job",
                                hint="call set_plan first")
        current = data["approval"]
        if not current or current.get("plan_sha256") != data["plan_sha256"]:
            job.update(lambda d: d.__setitem__("approval", {
                "plan_sha256": d["plan_sha256"], "time": _now(), "mode": "operator_override",
                "long_run_confirmed": False}))
        approval = job.read()["approval"]
    else:
        approval = data["approval"]
        if (not approval or data["plan_sha256"] is None
                or approval.get("plan_sha256") != data["plan_sha256"]):
            raise SimAgentError(ErrorCode.E_NOT_APPROVED,
                                "the current plan has no matching approval",
                                hint="show the plan to the researcher and record approval")
    if planned_prod_ns > cfg.workflow.max_production_ns_without_confirm \
            and not approval.get("long_run_confirmed"):
        raise SimAgentError(
            ErrorCode.E_NOT_APPROVED,
            f"a {planned_prod_ns:g} ns production run needs explicit long-run confirmation",
            hint="approve the plan again with long_run_confirmed set")
