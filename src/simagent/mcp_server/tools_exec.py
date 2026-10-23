"""Execution tools: write_mdp, run_grompp, run_mdrun, get_run_status, cancel_run, validate_stage
(tools 6 to 11). Check order follows design 4.2."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from simagent.config import Config
from simagent.core.approval import _build_gate, require_run_approval
from simagent.core.gates import GateCheck, apply_validation
from simagent.core.gmx import build_gmx_env
from simagent.core.grompp import ORDER, check_binary, check_inputs, run_grompp
from simagent.core.jobs import PROCESS_STAGES, STAGE_DIRS, JobHandle
from simagent.core.mdp import check_retry, effective_params, render_mdp, stage_params
from simagent.core.mdrun_wrapper import launch_wrapper
from simagent.core.paths import sha256_file, sha256_text
from simagent.core.provenance import file_entry, record_internal
from simagent.core.runs import (
    RunFiles,
    archive_run_files,
    decide_process_state,
    estimate_rate,
    parse_last_step,
    parse_performance,
    read_proc_snapshot,
    read_run_files,
    update_run_json,
)
from simagent.core.stage_checks import read_atom_count, stage_checks
from simagent.errors import ErrorCode, SimAgentError
from simagent.mcp_server.registry import ServerContext, tool
from simagent.mcp_server.tools_build import get_runner
from simagent.mcp_server.tools_job import StageName, get_store

ProcessStage = Literal["em", "nvt", "npt", "prod"]
_RETRY_STAGES: dict[str, tuple[str, ...]] = {
    "emtol": ("em",), "em_nsteps": ("em",), "dt": PROCESS_STAGES, "tau_p": PROCESS_STAGES,
}
_RETRYABLE_STATUS = ("PENDING", "READY", "FAILED", "PASSED")
_MAX_SAMPLES = 400
_CANCEL_WAIT_S = 10.0


def _refuse_if_running(data: dict[str, Any]) -> None:
    if any(data["stage_status"][s] == "RUNNING" for s in PROCESS_STAGES):
        raise SimAgentError(ErrorCode.E_MDRUN_RUNNING, "a stage process is running",
                            hint="wait for it to finish or cancel it first")


def _process_state(ctx: ServerContext, job: JobHandle, stage: str) -> tuple[str, RunFiles]:
    files = read_run_files(job.stage_dir(stage))
    proc = None
    pid = files.run_json.get("pid") if files.run_json else None
    if pid and not files.has_exit:
        snapshot = ctx.extras.get("proc_snapshot", read_proc_snapshot)
        proc = snapshot(int(pid))
    clock = ctx.extras.get("clock", time.time)
    return decide_process_state(files, proc, clock()), files


class RetryArg(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Literal["emtol", "dt", "tau_p", "em_nsteps"]
    value: float


class WriteMdpArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    stage: ProcessStage
    retry: RetryArg | None = None
    failing_stage: ProcessStage | None = None
    reset: bool = False


@tool("write_mdp", WriteMdpArgs,
      "Render a stage's .mdp from the stored plan (no free edits). With retry, apply one bounded "
      "automatic retry after a failure; with reset, clear a FAILED or CANCELLED stage.")
def write_mdp_tool(ctx: ServerContext, args: WriteMdpArgs) -> dict[str, Any]:
    stage = args.stage
    if args.retry is not None and args.reset:
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE, "retry and reset cannot be combined",
                            hint="send one or the other")
    job = get_store(ctx).get(args.job_id)
    data = job.read()
    _refuse_if_running(data)
    plan = data["plan"]
    if plan is None:
        raise SimAgentError(ErrorCode.E_STAGE_ORDER, "no plan is stored for this job",
                            hint="call set_plan first")
    status = data["stage_status"][stage]
    if args.retry is not None:
        if status not in _RETRYABLE_STATUS:
            raise SimAgentError(ErrorCode.E_STAGE_STATE,
                                f"{stage} is {status}; a retry needs a failed, ready, or passed "
                                f"stage", hint="reset a cancelled stage instead")
        key, value = args.retry.key, args.retry.value
        if stage not in _RETRY_STAGES[key]:
            raise SimAgentError(ErrorCode.E_RETRY_NOT_ALLOWED,
                                f"{key} does not apply to {stage}",
                                hint=f"{key} applies to {', '.join(_RETRY_STAGES[key])}")
        attributed = check_retry(plan, data, key, value, args.failing_stage, stage)
        job.invalidate(stage)
        entry = {"key": key, "value": value, "stage": stage, "failing_stage": attributed,
                 "time": datetime.now(UTC).isoformat()}
        job.update(lambda d: d["retries"].append(entry))
    elif args.reset:
        if status not in ("FAILED", "CANCELLED"):
            raise SimAgentError(ErrorCode.E_STAGE_STATE, f"{stage} is {status}, not failed or "
                                "cancelled", hint="only a failed or cancelled stage is reset")
        job.invalidate(stage)
    elif status != "PENDING":
        raise SimAgentError(ErrorCode.E_STAGE_STATE, f"{stage} is {status}, not PENDING",
                            hint="reset the stage before rendering its .mdp again")

    retries = job.read()["retries"]
    text = render_mdp(stage, plan, data["request"], retries)
    digest = sha256_text(text)
    directory = job.stage_dir(stage)
    (directory / f"{stage}.mdp").write_text(text, encoding="utf-8", newline="\n")
    job.update(lambda d: d["mdp_expected_sha256"].__setitem__(stage, digest))
    rel = f"{STAGE_DIRS[stage]}/{stage}.mdp"
    record_internal(job.ledger, stage=stage, tool="write_mdp", base=job.dir, outputs=[rel])
    return {"path": rel, "rendered_sha256": digest,
            "params": effective_params(stage, plan, retries)}


class GromppArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    stage: ProcessStage


@tool("run_grompp", GromppArgs,
      "Run grompp for a stage with the approved inputs. Unacknowledged warnings stop the stage.")
def run_grompp_tool(ctx: ServerContext, args: GromppArgs) -> dict[str, Any]:
    job = get_store(ctx).get(args.job_id)
    return run_grompp(job, args.stage, get_runner(ctx, job), ctx.cfg)


class MdrunArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    stage: ProcessStage
    resume: bool = False


def _thread_flags(cfg: Config, atoms: int) -> list[str]:
    flags: list[str] = []
    if cfg.gmx.ntomp != "auto":
        flags += ["-ntomp", str(cfg.gmx.ntomp)]
    if cfg.gmx.ntmpi == "auto":
        if atoms < 10000:
            flags += ["-ntmpi", "1"]
    else:
        flags += ["-ntmpi", str(cfg.gmx.ntmpi)]
    return flags


def _check_resume(job: JobHandle, data: dict[str, Any], stage: str, resume: bool) -> bool:
    """True when this start resumes an earlier run; E_STAGE_STATE when the stage cannot start."""
    status = data["stage_status"][stage]
    if status == "READY":
        return False
    if status not in ("FAILED", "CANCELLED") or not resume:
        raise SimAgentError(ErrorCode.E_STAGE_STATE, f"{stage} is {status}, not READY",
                            hint="run grompp first; to continue a died or cancelled run, "
                            "set resume")
    if status == "FAILED":
        causes = [f["cause"] for f in data["failures"] if f["stage"] == stage]
        if not causes or causes[-1] not in ("process_died", "timeout"):
            raise SimAgentError(ErrorCode.E_STAGE_STATE,
                                f"{stage} failed a gate, so it cannot resume",
                                hint="reset the stage with write_mdp(reset=true) and run grompp")
    if not (job.stage_dir(stage) / f"{stage}.cpt").is_file():
        raise SimAgentError(ErrorCode.E_STAGE_STATE, f"{stage} has no checkpoint to resume from",
                            hint="reset the stage with write_mdp(reset=true) and run grompp")
    return True


@tool("run_mdrun", MdrunArgs,
      "Start mdrun for a READY stage through the detached wrapper. Needs a recorded approval of "
      "the stored plan.")
def run_mdrun_tool(ctx: ServerContext, args: MdrunArgs) -> dict[str, Any]:
    stage = args.stage
    job = get_store(ctx).get(args.job_id)
    check_binary(job, ctx.cfg)
    data = job.read()
    plan = data["plan"]
    prod = stage_params(plan, "prod") if plan else None
    planned_ns = prod["nsteps"] * prod["dt"] / 1000 if prod else 0.0
    require_run_approval(job, ctx.cfg, planned_ns)
    data = job.read()
    _refuse_if_running(data)
    if plan is None or any(data["stage_status"][s] != "PASSED"
                           for s in ORDER[:ORDER.index(stage)]):
        raise SimAgentError(ErrorCode.E_STAGE_ORDER,
                            f"an earlier stage is not complete for {stage}",
                            hint="pass the earlier stages first")
    resuming = _check_resume(job, data, stage, args.resume)
    check_inputs(job, stage, plan, data["request"])
    stage_dir = job.stage_dir(stage)
    tpr = stage_dir / f"{stage}.tpr"
    rel_tpr = f"{STAGE_DIRS[stage]}/{stage}.tpr"
    if not tpr.is_file() or not job.ledger.has_receipt(rel_tpr, sha256_file(tpr)):
        raise SimAgentError(ErrorCode.E_INPUT_MODIFIED,
                            f"{rel_tpr} was not produced by grompp from the approved inputs",
                            hint="run grompp again for this stage")

    argv = [ctx.cfg.gmx.bin, "mdrun", "-deffnm", stage, "-cpt", str(ctx.cfg.gmx.cpt_minutes),
            *_thread_flags(ctx.cfg, read_atom_count(job))]
    if resuming:
        argv += ["-cpi", f"{stage}.cpt", "-append"]
        archive_run_files(stage_dir)
    previous = data["stage_status"][stage]
    job.set_status(stage, "RUNNING")
    launcher = ctx.extras.get("launch_wrapper", launch_wrapper)
    try:
        pid = launcher(stage_dir, argv, build_gmx_env(ctx.cfg.gmx, os.environ),
                       ledger_path=job.dir / "provenance.jsonl", stage=stage,
                       max_wall_hours=ctx.cfg.gmx.max_wall_hours)
    except OSError as exc:
        job.update(lambda d: d["stage_status"].__setitem__(stage, previous))
        (stage_dir / "run.json").unlink(missing_ok=True)
        raise SimAgentError(ErrorCode.E_MDRUN_FAILED, f"could not start the mdrun wrapper: {exc}",
                            hint="check that the server can start processes") from exc
    return {"pid": pid, "started_at": datetime.now(UTC).isoformat(),
            "log_path": f"{STAGE_DIRS[stage]}/{stage}.log"}


class StatusArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    stage: StageName


def _process_stage(stage: str) -> str:
    if stage not in PROCESS_STAGES:
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE, f"{stage} has no mdrun process",
                            hint="stage must be em, nvt, npt, or prod")
    return stage


def _planned_ps(job: JobHandle, stage: str) -> float | None:
    if stage == "em":
        return None
    data = job.read()
    params = effective_params(stage, data["plan"], data["retries"])
    return float(params["nsteps"] * params["dt"])


@tool("get_run_status", StatusArgs,
      "Report whether a stage's mdrun is running, finished, died, or cancelled, with progress, "
      "ns/day, an ETA, and the last log lines. Does not change stage status.")
def get_run_status_tool(ctx: ServerContext, args: StatusArgs) -> dict[str, Any]:
    stage = _process_stage(args.stage)
    job = get_store(ctx).get(args.job_id)
    state, files = _process_state(ctx, job, stage)
    stage_dir = job.stage_dir(stage)
    log = stage_dir / f"{stage}.log"
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    last = parse_last_step(text)
    step, time_ps = last if last is not None else (None, None)
    ns_per_day: float | None = None
    eta_s: float | None = None
    if state == "running" and time_ps is not None and files.run_json is not None:
        clock = ctx.extras.get("clock", time.time)
        stamp = clock()

        def add_sample(d: dict[str, Any]) -> None:
            samples = d.setdefault("samples", [])
            samples.append([stamp, time_ps])
            del samples[:-_MAX_SAMPLES]

        run = update_run_json(stage_dir, add_sample)
        total = _planned_ps(job, stage)
        remaining = None if total is None else max(total - time_ps, 0.0)
        ns_per_day, eta_s = estimate_rate([(s[0], s[1]) for s in run["samples"]], remaining or 0.0)
        if remaining is None:
            eta_s = None
    elif state == "finished":
        ns_per_day = parse_performance(text)
    return {"state": state, "exit_code": files.exit_code, "step": step, "time_ps": time_ps,
            "ns_per_day": ns_per_day, "eta_s": eta_s, "log_tail": text.splitlines()[-20:]}


class CancelArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    stage: ProcessStage


def _wait_for_exit(stage_dir: Path, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if (stage_dir / "run.exit").exists():
            return True
        time.sleep(0.1)
    return (stage_dir / "run.exit").exists()


def _kill_group(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],  # noqa: S603, S607
                       capture_output=True, check=False)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


@tool("cancel_run", CancelArgs,
      "Cancel a running stage: stop the wrapper and mdrun and mark the stage CANCELLED. Does "
      "nothing when nothing is running.")
def cancel_run_tool(ctx: ServerContext, args: CancelArgs) -> dict[str, Any]:
    stage = args.stage
    job = get_store(ctx).get(args.job_id)
    if job.status(stage) != "RUNNING":
        return {"cancelled": False, "signal_sent": None}
    state, files = _process_state(ctx, job, stage)
    if state != "running" or files.run_json is None:
        return {"cancelled": False, "signal_sent": None}
    stage_dir = job.stage_dir(stage)
    (stage_dir / "run.cancelled").write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    pid = int(files.run_json.get("pid", 0))
    signal_sent = None
    if sys.platform != "win32" and pid:
        try:
            os.kill(pid, signal.SIGTERM)
            signal_sent = "SIGTERM"
        except ProcessLookupError:
            pass
    if not _wait_for_exit(stage_dir, _CANCEL_WAIT_S):
        if pid:
            _kill_group(pid)
        if not _wait_for_exit(stage_dir, 2.0):
            job.ledger.append({
                "event": "end", "kind": "gmx", "stage": stage, "tool": "cancel_run",
                "cancelled": True, "exit_code": None, "outputs": [
                    file_entry(job.dir, f"{STAGE_DIRS[stage]}/{p.name}")
                    for p in sorted(stage_dir.iterdir())
                    if p.is_file() and p.suffix in (".log", ".edr", ".xtc", ".cpt", ".gro")]})
            (stage_dir / "run.exit").write_text("-9\n", encoding="utf-8")
        signal_sent = "SIGKILL" if sys.platform != "win32" else signal_sent
    job.set_status(stage, "CANCELLED")
    return {"cancelled": True, "signal_sent": signal_sent}


class ValidateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    stage: StageName


@tool("validate_stage", ValidateArgs,
      "Check a stage against its gate and move a finished run to PASSED or FAILED. A failed gate "
      "is a result, not an error.")
def validate_stage_tool(ctx: ServerContext, args: ValidateArgs) -> dict[str, Any]:
    stage = args.stage
    job = get_store(ctx).get(args.job_id)
    data = job.read()
    simulated = data["execution_mode"] == "simulated"
    if stage == "build":
        return apply_validation(
            job, stage, process_state=None,
            gate_fn=lambda: _build_gate(job, data["plan"], ctx.cfg.gates, simulated))
    if stage == "analysis":  # the analysis gate arrives with the analysis tools
        return apply_validation(job, stage, process_state=None, gate_fn=lambda: [
            GateCheck("analysis_not_run", "not_run")])
    state, files = _process_state(ctx, job, stage)
    process_state = "timeout" if files.timeout and state == "died" else state
    runner = get_runner(ctx, job)
    return apply_validation(
        job, stage, process_state=process_state,
        gate_fn=lambda: stage_checks(job, stage, ctx.cfg, runner, process_state))
