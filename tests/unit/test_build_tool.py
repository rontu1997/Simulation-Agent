from dataclasses import replace

import pytest
from pydantic import ValidationError
from test_build import REQUEST, StubRunner

from simagent.config import load_config
from simagent.events import EventLog, Redactor
from simagent.mcp_server.registry import TOOLS, ServerContext
from simagent.mcp_server.tools_build import BuildArgs, build_water_box_tool
from simagent.mcp_server.tools_job import get_store


def make_ctx(tmp_path):
    cfg = load_config(env={})
    cfg = replace(cfg, paths=replace(cfg.paths, runs_dir=tmp_path / "runs",
                                     sim_runs_dir=tmp_path / "sim"))
    ctx = ServerContext(cfg=cfg, events=EventLog(tmp_path / "ev.jsonl", Redactor([])))
    ctx.extras["runner_factory"] = lambda c, ledger: StubRunner()
    return ctx


def test_tool_registered_with_forbidden_extras():
    assert "build_water_box" in TOOLS
    with pytest.raises(ValidationError):
        BuildArgs(job_id="j", n_molecules=1000, water_model="tip4p", bogus=1)


def test_build_tool_runs_the_builder_on_the_named_job(tmp_path):
    ctx = make_ctx(tmp_path)
    job = get_store(ctx).create(REQUEST, "real", {"sha256": "a" * 64})
    out = build_water_box_tool(ctx, BuildArgs(job_id=job.job_id, n_molecules=1000,
                                              water_model="tip4p"))
    assert out["n_molecules"] == 1000 and out["n_atoms"] == 4000
    assert out["build_sha256"] and job.status("build") == "GATING"
