"""Job tools. Tool 2: create_job (more job tools are added in later tasks)."""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from simagent.core.envcheck import check_environment
from simagent.core.gmx import build_gmx_env
from simagent.core.jobs import JobStore
from simagent.core.mdp import validate_request
from simagent.core.paths import sha256_file
from simagent.mcp_server.registry import ServerContext, tool


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
    return {**cache[key], "host": socket.gethostname()}


@tool("create_job", CreateJobArgs,
      "Create a job directory for a simulation request and record the verified environment.")
def create_job(ctx: ServerContext, args: CreateJobArgs) -> dict[str, Any]:
    warnings = validate_request(args.request)
    environment = _environment(ctx)
    job = get_store(ctx).create(args.request, environment["execution_mode"], environment)
    return {"job_id": job.job_id, "job_dir": str(job.dir), "environment": environment,
            "warnings": warnings}
