"""Job tools: create_job, set_plan, record_approval, get_log_excerpt, list_artifacts, get_job, and
list_jobs (tools 2, 3, 4, 12, 19, 20, 21)."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from simagent.core.approval import record_approval, set_plan
from simagent.core.envcheck import check_environment
from simagent.core.gmx import build_gmx_env
from simagent.core.jobs import STAGE_DIRS, STAGES, JobStore
from simagent.core.mdp import validate_request
from simagent.core.paths import sha256_file
from simagent.errors import ErrorCode, SimAgentError
from simagent.mcp_server.registry import ServerContext, tool

StageName = Literal["build", "em", "nvt", "npt", "prod", "analysis"]


class CreateJobArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: dict[str, Any]


def get_store(ctx: ServerContext) -> JobStore:
    if "store" not in ctx.extras:
        ctx.extras["store"] = JobStore(ctx.cfg)
    store: JobStore = ctx.extras["store"]
    return store


def _environment(ctx: ServerContext) -> dict[str, Any]:
    """The environment check, cached per binary hash, size, mtime, env allow-list, and host."""
    check: Callable[[Any], dict[str, Any]] = ctx.extras.get("check_environment", check_environment)
    binary = Path(ctx.cfg.gmx.bin)
    cache: dict[Any, dict[str, Any]] = ctx.extras.setdefault("env_cache", {})
    try:
        stat = os.stat(binary)
        key: Any = (str(binary), stat.st_size, stat.st_mtime_ns, sha256_file(binary),
                    json.dumps(build_gmx_env(ctx.cfg.gmx, os.environ), sort_keys=True),
                    socket.gethostname())
    except OSError:
        return check(ctx.cfg)  # missing binary: let the check raise the precise error
    if key not in cache:
        cache[key] = check(ctx.cfg)
    return {**cache[key], "host": socket.gethostname(),
            "bin_stat": {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}}


@tool("create_job", CreateJobArgs,
      "Create a job directory for a simulation request and record the verified environment.")
def create_job(ctx: ServerContext, args: CreateJobArgs) -> dict[str, Any]:
    warnings = validate_request(args.request)
    environment = _environment(ctx)
    job = get_store(ctx).create(args.request, environment["execution_mode"], environment)
    return {"job_id": job.job_id, "job_dir": str(job.dir), "environment": environment,
            "warnings": warnings}


class SetPlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    request: dict[str, Any]
    plan: dict[str, Any]


@tool("set_plan", SetPlanArgs,
      "Validate and store the request and the seven-stage plan, check the build against it, and "
      "return the plan hash. Any new plan clears the approval.")
def set_plan_tool(ctx: ServerContext, args: SetPlanArgs) -> dict[str, Any]:
    return set_plan(get_store(ctx).get(args.job_id), ctx.cfg, args.request, args.plan)


class RecordApprovalArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    approval_token: str
    kind: Literal["plan", "warnings"]
    plan_sha256: str | None = None
    warning_ids: list[str] | None = None
    mode: str = "interactive"
    long_run_confirmed: bool = False
    note: str = ""


@tool("record_approval", RecordApprovalArgs,
      "Record the researcher's approval of the plan hash, or acknowledgement of grompp warnings. "
      "Needs the per-launch approval token; never offered to the model.")
def record_approval_tool(ctx: ServerContext, args: RecordApprovalArgs) -> dict[str, Any]:
    return record_approval(
        get_store(ctx).get(args.job_id), ctx.cfg, token=args.approval_token, kind=args.kind,
        plan_sha256=args.plan_sha256, warning_ids=args.warning_ids, mode=args.mode,
        long_run_confirmed=args.long_run_confirmed, note=args.note)


class GetJobArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str


@tool("get_job", GetJobArgs, "Read a job: request, plan, stage statuses, approval state, summary.")
def get_job_tool(ctx: ServerContext, args: GetJobArgs) -> dict[str, Any]:
    job = get_store(ctx).get(args.job_id)
    data = job.read()
    approval = data["approval"]
    summary_path = job.stage_dir("analysis") / "summary.json"
    return {
        "job_id": data["job_id"], "request": data["request"], "plan": data["plan"],
        "plan_sha256": data["plan_sha256"], "stage_status": data["stage_status"],
        "approval": {
            "present": bool(approval), "mode": approval.get("mode") if approval else None,
            "matches_plan": bool(approval and approval.get("plan_sha256") == data["plan_sha256"]),
        },
        "retries": data["retries"], "failures": data["failures"],
        "environment": data["environment"], "execution_mode": data["execution_mode"],
        "job_status": job.job_status(),
        "summary": (json.loads(summary_path.read_text(encoding="utf-8"))
                    if summary_path.is_file() else None),
    }


class ListJobsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int | None = None
    status: Literal["planned", "running", "passed", "failed", "cancelled"] | None = None


@tool("list_jobs", ListJobsArgs, "List jobs (newest first), optionally filtered by outcome.")
def list_jobs_tool(ctx: ServerContext, args: ListJobsArgs) -> dict[str, Any]:
    return {"jobs": get_store(ctx).list(limit=args.limit, status=args.status)}


_MAX_REGEX_CHARS = 200
_MAX_LINE_CHARS = 2000
_REGEX_TIMEOUT_S = 3.0
_FILTER_CODE = (
    "import json, re, sys\n"
    "job = json.load(sys.stdin)\n"
    "pattern = re.compile(job['pattern'])\n"
    "json.dump([ln for ln in job['lines'] if pattern.search(ln)], sys.stdout)\n")


def _filter_lines(lines: list[str], pattern: str) -> list[str]:
    """Lines matching ``pattern``. The model supplies the regex, and ``re`` cannot be interrupted,
    so the match runs in a child process that is killed after a few seconds."""
    env = {k: v for k, v in os.environ.items() if k in ("SYSTEMROOT", "PATH")}
    payload = json.dumps({"pattern": pattern, "lines": [ln[:_MAX_LINE_CHARS] for ln in lines]})
    try:
        done = subprocess.run(  # noqa: S603 - fixed argument list, shell=False
            [sys.executable, "-I", "-c", _FILTER_CODE], input=payload, capture_output=True,
            text=True, timeout=_REGEX_TIMEOUT_S, env=env, shell=False, check=False)
    except subprocess.TimeoutExpired as exc:
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE, "the regex took too long to match",
                            hint="use a simpler pattern without nested repetition") from exc
    if done.returncode != 0:
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE, "the regex could not be applied",
                            hint="use a simpler regular expression")
    matched: list[str] = json.loads(done.stdout)
    return matched


class LogExcerptArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    stage: StageName
    regex: str | None = None
    lines: int = 20
    which: Literal["log", "grompp", "stderr"] = "log"


_LOG_FILES = {"grompp": "grompp.out", "stderr": "mdrun.err"}


@tool("get_log_excerpt", LogExcerptArgs,
      "Read at most 40 lines from a stage's mdrun log, grompp output, or mdrun stderr, optionally "
      "filtered by a regular expression.")
def get_log_excerpt(ctx: ServerContext, args: LogExcerptArgs) -> dict[str, Any]:
    if not 1 <= args.lines <= 40:
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE,
                            f"lines = {args.lines} is outside 1 to 40",
                            hint="ask for at most 40 lines")
    if args.regex is not None:
        if len(args.regex) > _MAX_REGEX_CHARS:
            raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE,
                                f"the regex is longer than {_MAX_REGEX_CHARS} characters",
                                hint="use a shorter pattern")
        try:
            re.compile(args.regex)
        except re.error as exc:
            raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE, f"invalid regex: {exc}",
                                hint="use a valid regular expression") from exc
    job = get_store(ctx).get(args.job_id)
    name = f"{args.stage}.log" if args.which == "log" else _LOG_FILES[args.which]
    path = job.stage_dir(args.stage) / name
    if not path.is_file():
        raise SimAgentError(ErrorCode.E_ANALYSIS_INPUT, f"no {args.which} file for {args.stage}",
                            hint="the stage has not produced that file yet")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if args.regex is not None:
        lines = _filter_lines(lines, args.regex)
    return {"lines": [ln[:_MAX_LINE_CHARS] for ln in lines[-args.lines:]],
            "path": f"{STAGE_DIRS[args.stage]}/{name}"}


class ArtifactsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    stage: StageName | None = None


@tool("list_artifacts", ArtifactsArgs,
      "List a job's files with size, SHA-256, and whether a provenance receipt covers them.")
def list_artifacts(ctx: ServerContext, args: ArtifactsArgs) -> dict[str, Any]:
    job = get_store(ctx).get(args.job_id)
    receipts: dict[tuple[str, str], tuple[int, str | None]] = {}
    for record in job.ledger.records():
        for entry in record.get("outputs", []):
            receipts[(entry["path"], entry["sha256"])] = (record["seq"], record.get("kind"))
    files: list[dict[str, Any]] = []
    for stage in [args.stage] if args.stage else list(STAGES):
        directory = job.stage_dir(stage)
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            rel = path.relative_to(job.dir).as_posix()
            if "/_dry/" in f"/{rel}":
                continue
            digest = sha256_file(path)
            seq, kind = receipts.get((rel, digest), (None, None))
            files.append({"path": rel, "bytes": path.stat().st_size, "sha256": digest,
                          "produced_by_seq": seq, "has_receipt": seq is not None, "kind": kind})
    report = job.dir / "report.md"
    if args.stage is None and report.is_file():
        digest = sha256_file(report)
        recorded = job.read()["report_sha256"] == digest
        files.append({"path": "report.md", "bytes": report.stat().st_size, "sha256": digest,
                      "produced_by_seq": None, "has_receipt": recorded,
                      "kind": "agent" if recorded else None})
    return {"files": files}
