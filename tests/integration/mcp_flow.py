"""Shared helpers for the MCP integration tests: server parameters, a prepared job on disk, and the
controller-side flow (the test plays the CLI: it holds the approval token)."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.client.stdio import StdioServerParameters

from simagent.config import load_config
from simagent.core.approval import plan_sha256
from simagent.core.jobs import JobHandle, JobStore
from simagent.core.mdp import build_default_plan, derive_seed
from simagent.core.paths import sha256_file
from simagent.mcp_client import McpSession, server_params

TOKEN = "tok-integration-0123456789"
FAKE = Path(__file__).resolve().parents[1] / "fakes" / "fake_gmx"
REQUEST: dict[str, Any] = {
    "system": {"kind": "water", "n_molecules": 1000, "water_model": "tip4p"},
    "force_field": "oplsaa",
    "thermo": {"temperature_K": 300.0, "pressure_bar": 1.0},
    "protocol": {"emtol": 1000.0, "nvt_ps": 100, "npt_ps": 100, "prod_ns": 1.0, "dt_ps": 0.002,
                 "tau_t": 0.1, "tau_p": 2.0},
    "seed": None,
    "analyses": ["rdf_OO", "density"],
}


def make_params(work: Path, *, binary: Path, token: str | None = None,
                **extra_env: str) -> StdioServerParameters:
    """Server launch parameters with every run directory under ``work``."""
    parent = {k: v for k, v in os.environ.items() if not k.upper().startswith("SIMAGENT_")}
    parent.update({"GMX_BIN": str(binary), "GMX_RUNS_DIR": str(work / "runs"), **extra_env})
    params = server_params(parent, token)
    return params.model_copy(update={"cwd": str(work)})


def fake_binary(work: Path) -> Path:
    """The fake gmx, copied somewhere executable (a script with a #! line)."""
    target = work / "bin" / "gmx"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FAKE, target)
    target.chmod(0o755)
    return target


def dummy_binary(work: Path) -> Path:
    """A file that stands in for the binary where no process is ever started."""
    target = work / "bin" / "gmx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    return target


def store_for(work: Path) -> JobStore:
    cfg = load_config(env={})
    cfg = replace(cfg, paths=replace(cfg.paths, runs_dir=work / "runs",
                                     sim_runs_dir=work / "runs_simulated"))
    return JobStore(cfg, clock=lambda: datetime(2026, 10, 23, tzinfo=UTC))


def prepared_job(work: Path, binary: Path) -> JobHandle:
    """A job on disk that already holds a plan, as if set_plan had run (no GROMACS needed)."""
    job = store_for(work).create(dict(REQUEST), "real", {"sha256": sha256_file(binary)})
    request = {**REQUEST, "seed": derive_seed(job.job_id)}
    build = {"box_nm": 3.107, "build_sha256": "b" * 64, "target_density_g_cm3": 0.997}
    plan = build_default_plan(request, build)
    stat = binary.stat()

    def store(data: dict[str, Any]) -> None:
        data["request"], data["plan"] = request, plan
        data["plan_sha256"] = plan_sha256(request, plan)
        data["environment"]["bin_stat"] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}

    job.update(store)
    return job


async def wait_for_state(session: McpSession, job_id: str, stage: str, want: str,
                         timeout: float = 90.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        status = await session.call("get_run_status", {"job_id": job_id, "stage": stage})
        assert status["ok"], status
        if status["data"]["state"] == want:
            result: dict[str, Any] = status["data"]
            return result
        assert time.monotonic() < deadline, f"stage {stage} never became {want}: {status}"
        await asyncio.sleep(0.5)


async def prepare_approved_job(session: McpSession) -> dict[str, Any]:
    """create_job, build, set_plan, and plan approval (the test plays the CLI and holds the
    token). Returns the created job and the build."""
    created = await session.call("create_job", {"request": dict(REQUEST)})
    assert created["ok"], created
    job_id = created["data"]["job_id"]
    request = {**REQUEST, "seed": derive_seed(job_id)}
    built = await session.call("build_water_box", {
        "job_id": job_id, "n_molecules": 1000, "water_model": "tip4p"})
    assert built["ok"], built
    plan = build_default_plan(request, built["data"])
    planned = await session.call("set_plan", {"job_id": job_id, "request": request, "plan": plan})
    assert planned["ok"], planned
    approved = await session.call("record_approval", {
        "job_id": job_id, "approval_token": TOKEN, "kind": "plan",
        "plan_sha256": planned["data"]["plan_sha256"]})
    assert approved["ok"], approved
    return {"created": created["data"], "built": built["data"], "job_id": job_id}


async def run_em_flow(session: McpSession) -> dict[str, Any]:
    """create_job to a validated EM stage; returns the envelopes the tests assert on."""
    prepared = await prepare_approved_job(session)
    job_id = prepared["job_id"]
    for step in ("write_mdp", "run_grompp"):
        done = await session.call(step, {"job_id": job_id, "stage": "em"})
        assert done["ok"], done
    started = await session.call("run_mdrun", {"job_id": job_id, "stage": "em", "resume": False})
    assert started["ok"], started
    final = await wait_for_state(session, job_id, "em", "finished")
    validated = await session.call("validate_stage", {"job_id": job_id, "stage": "em"})
    return {"created": prepared["created"], "built": prepared["built"],
            "started": started["data"], "final": final, "validated": validated}
