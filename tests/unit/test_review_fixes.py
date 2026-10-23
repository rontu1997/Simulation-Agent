"""Regression tests for the findings of the final whole-branch review (C1, I1 to I7)."""

import asyncio
import copy
import errno
import json
import time
from pathlib import Path

import pytest
from test_exec_tools import (
    exec_ctx,
    finish_run,
    prepare,
    set_status,
)
from test_job_tools import make_ctx, planned

from simagent.config import load_config
from simagent.core.mdp import build_default_plan
from simagent.core.provenance import Ledger
from simagent.errors import ErrorCode, SimAgentError
from simagent.llm import LlmClient, LlmUnavailable
from simagent.mcp_server.registry import TOOLS, ServerContext, dispatch, tool
from simagent.mcp_server.tools_exec import (
    CancelArgs,
    GromppArgs,
    MdrunArgs,
    RetryArg,
    ValidateArgs,
    WriteMdpArgs,
    cancel_run_tool,
    run_grompp_tool,
    run_mdrun_tool,
    validate_stage_tool,
    write_mdp_tool,
)
from simagent.mcp_server.tools_job import (
    LogExcerptArgs,
    SetPlanArgs,
    get_log_excerpt,
    get_store,
    set_plan_tool,
)


def code_of(fn, *args):
    with pytest.raises(SimAgentError) as exc:
        fn(*args)
    return exc.value.code


# C1: a new plan must not inherit retries and failures of the old one -------------------------

def replan(ctx, job_id, emtol):
    job = get_store(ctx).get(job_id)
    request = copy.deepcopy(job.read()["request"])
    request["protocol"]["emtol"] = emtol
    info = json.loads((job.stage_dir("build") / "build.json").read_text(encoding="utf-8"))
    plan = build_default_plan(request, info)
    return set_plan_tool(ctx, SetPlanArgs(job_id=job_id, request=request, plan=plan))


def fail_em_and_retry(ctx, job_id, value=900.0):
    job = get_store(ctx).get(job_id)
    prepare(ctx, job_id)
    set_status(job, em="FAILED")
    job.update(lambda d: d["failures"].append({"stage": "em", "cause": "gate failed: converged"}))
    return write_mdp_tool(ctx, WriteMdpArgs(
        job_id=job_id, stage="em", retry=RetryArg(key="emtol", value=value)))


def test_set_plan_clears_failures_and_retries_of_changed_stages(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    job = get_store(ctx).get(job_id)
    fail_em_and_retry(ctx, job_id)
    assert job.read()["retries"]
    replan(ctx, job_id, emtol=300.0)
    data = job.read()
    assert data["retries"] == [] and [f for f in data["failures"] if f["stage"] == "em"] == []
    out = write_mdp_tool(ctx, WriteMdpArgs(job_id=job_id, stage="em"))
    assert out["params"]["emtol"] == 300.0  # the approved value, not the old retry's 900


def test_a_stored_retry_outside_the_plans_bounds_is_input_modified(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    job = get_store(ctx).get(job_id)
    job.update(lambda d: d["retries"].append({
        "key": "emtol", "value": 5000.0, "stage": "em", "failing_stage": "em", "time": "x"}))
    write_mdp_tool(ctx, WriteMdpArgs(job_id=job_id, stage="em"))  # renders, so the .mdp matches
    assert code_of(run_grompp_tool, ctx, GromppArgs(job_id=job_id, stage="em")
                   ) == ErrorCode.E_INPUT_MODIFIED


# I1: a stage must never be stranded RUNNING --------------------------------------------------

def running_stage(ctx, job_id):
    job = get_store(ctx).get(job_id)
    set_status(job, em="RUNNING")
    return job


def test_cancel_run_finishes_an_interrupted_cancel(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    job = running_stage(ctx, job_id)
    finish_run(job, "em", exit_code=-15)
    (job.stage_dir("em") / "run.cancelled").write_text("x", encoding="utf-8")
    out = cancel_run_tool(ctx, CancelArgs(job_id=job_id, stage="em"))
    assert out["cancelled"] is True and job.status("em") == "CANCELLED"


def test_validate_stage_settles_a_cancelled_run_left_running(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    job = running_stage(ctx, job_id)
    finish_run(job, "em", exit_code=-15)
    (job.stage_dir("em") / "run.cancelled").write_text("x", encoding="utf-8")
    assert code_of(validate_stage_tool, ctx, ValidateArgs(job_id=job_id, stage="em")
                   ) == ErrorCode.E_CANCELLED
    assert job.status("em") == "CANCELLED"


def test_validate_stage_fails_a_running_stage_whose_process_never_started(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    job = running_stage(ctx, job_id)  # no run.json at all
    out = validate_stage_tool(ctx, ValidateArgs(job_id=job_id, stage="em"))
    assert out["passed"] is False and job.status("em") == "FAILED"


def test_run_mdrun_reverts_the_stage_when_the_launcher_fails_unexpectedly(tmp_path):
    from test_exec_tools import approve

    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    approve(ctx, job_id, digest)
    prepare(ctx, job_id)

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    ctx.extras["launch_wrapper"] = broken
    assert code_of(run_mdrun_tool, ctx, MdrunArgs(job_id=job_id, stage="em", resume=False)
                   ) == ErrorCode.E_MDRUN_FAILED
    assert get_store(ctx).get(job_id).status("em") == "READY"


# I2: a gate that cannot be evaluated fails the stage instead of stranding it in GATING -------

def test_unreadable_energy_file_fails_the_stage_not_strands_it(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    job = get_store(ctx).get(job_id)
    set_status(job, nvt="RUNNING")
    finish_run(job, "nvt")  # finished, but no .edr: `gmx energy` yields nothing
    out = validate_stage_tool(ctx, ValidateArgs(job_id=job_id, stage="nvt"))
    assert out["passed"] is False
    assert any(c["name"] == "gate_input" and c["status"] == "fail" for c in out["checks"])
    assert job.status("nvt") == "FAILED"


# I3: the server loads config.yaml ---------------------------------------------------------------

def test_server_context_loads_config_yaml_from_the_working_directory(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("gmx:\n  max_wall_hours: 3\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GMX_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.delenv("SIMAGENT_CONFIG", raising=False)
    assert ServerContext.create().cfg.gmx.max_wall_hours == 3


def test_simagent_config_names_another_file(tmp_path, monkeypatch):
    other = tmp_path / "other.yaml"
    other.write_text("gmx:\n  cpt_minutes: 7\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GMX_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("SIMAGENT_CONFIG", str(other))
    assert ServerContext.create().cfg.gmx.cpt_minutes == 7


# I4: a relative GMX_BIN is made absolute ---------------------------------------------------------

def test_relative_gmx_bin_becomes_absolute():
    cfg = load_config(env={"GMX_BIN": "bin/gmx"})
    assert Path(cfg.gmx.bin).is_absolute() and Path(cfg.gmx.bin).name == "gmx"


# I5: a catastrophic regex cannot freeze the server -----------------------------------------------

def test_get_log_excerpt_survives_a_catastrophic_regex(tmp_path):
    ctx, job_id, plan, digest = planned(tmp_path)
    job = get_store(ctx).get(job_id)
    (job.stage_dir("em") / "em.log").write_text("a" * 60 + "b\n", encoding="utf-8")
    started = time.monotonic()
    assert code_of(get_log_excerpt, ctx, LogExcerptArgs(
        job_id=job_id, stage="em", regex="(a+)+$")) == ErrorCode.E_PARAM_OUT_OF_RANGE
    assert time.monotonic() - started < 15
    assert code_of(get_log_excerpt, ctx, LogExcerptArgs(
        job_id=job_id, stage="em", regex="a" * 500)) == ErrorCode.E_PARAM_OUT_OF_RANGE


# I6: no silent fallback to a third-party endpoint ------------------------------------------------

def test_llm_client_refuses_to_run_without_a_base_url():
    cfg = load_config(env={"LUNA_API_KEY": "key-123456"}).llm
    assert cfg.base_url is None
    client = LlmClient(cfg)
    with pytest.raises(LlmUnavailable) as exc:
        client.chat([{"role": "user", "content": "hi"}])
    assert "LUNA_BASE_URL" in str(exc.value) and "key-123456" not in str(exc.value)


# I7: a torn ledger line and a full disk ----------------------------------------------------------

def test_ledger_survives_a_torn_final_line(tmp_path):
    ledger = Ledger(tmp_path / "provenance.jsonl")
    ledger.append({"event": "end", "stage": "em"})
    with open(ledger.path, "ab") as handle:
        handle.write(b'{"event": "start", "se')  # a write cut short by a full disk or a crash
    assert len(ledger.records()) == 1
    assert ledger.append({"event": "end", "stage": "nvt"}) == 2
    assert [r["seq"] for r in ledger.records()] == [1, 2]


def test_disk_full_is_reported_as_e_disk_full(tmp_path):
    ctx = make_ctx(tmp_path)

    @tool("zz_disk_full")
    def zz_disk_full(ctx, args):
        raise OSError(errno.ENOSPC, "No space left on device")

    try:
        result = asyncio.run(dispatch(ctx, "zz_disk_full", {}))
    finally:
        TOOLS.pop("zz_disk_full", None)
    assert result.structured_content["error"]["code"] == "E_DISK_FULL"
    assert result.is_error is True
