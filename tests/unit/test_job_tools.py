import copy
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError
from test_approval import REQUEST, TOKEN
from test_build import StubRunner

from simagent.config import load_config
from simagent.core.mdp import build_default_plan
from simagent.errors import ErrorCode, SimAgentError
from simagent.events import EventLog, Redactor
from simagent.mcp_server import tools_job
from simagent.mcp_server.registry import TOOLS, ServerContext
from simagent.mcp_server.tools_build import BuildArgs, build_water_box_tool
from simagent.mcp_server.tools_job import (
    ArtifactsArgs,
    CreateJobArgs,
    GetJobArgs,
    ListJobsArgs,
    LogExcerptArgs,
    RecordApprovalArgs,
    SetPlanArgs,
    create_job,
    get_job_tool,
    get_log_excerpt,
    list_artifacts,
    list_jobs_tool,
    record_approval_tool,
    set_plan_tool,
)

TOOL_NAMES = {"set_plan", "record_approval", "get_job", "list_jobs", "get_log_excerpt",
              "list_artifacts", "create_job"}


def make_ctx(tmp_path, env=None):
    cfg = load_config(env={"SIMAGENT_APPROVAL_TOKEN": TOKEN} if env is None else env)
    cfg = replace(cfg, paths=replace(cfg.paths, runs_dir=tmp_path / "runs",
                                     sim_runs_dir=tmp_path / "sim"))
    ctx = ServerContext(cfg=cfg, events=EventLog(tmp_path / "ev.jsonl", Redactor([TOKEN])))
    ctx.extras["runner_factory"] = lambda c, ledger: StubRunner()
    ctx.extras["check_environment"] = lambda c: {
        "sha256": "a" * 64, "execution_mode": "real", "env_allow": {}, "version": "2025.3",
        "probe": {"ok": True}}
    return ctx


def planned(tmp_path, env=None):
    ctx = make_ctx(tmp_path, env)
    made = create_job(ctx, CreateJobArgs(request=copy.deepcopy(REQUEST)))
    job_id = made["job_id"]
    info = build_water_box_tool(ctx, BuildArgs(job_id=job_id, n_molecules=1000,
                                               water_model="tip4p"))
    plan = build_default_plan(copy.deepcopy(REQUEST), info)
    out = set_plan_tool(ctx, SetPlanArgs(job_id=job_id, request=copy.deepcopy(REQUEST), plan=plan))
    return ctx, job_id, plan, out["plan_sha256"]


def test_tools_are_registered_with_forbidden_extras():
    assert TOOL_NAMES <= set(TOOLS)
    with pytest.raises(ValidationError):
        GetJobArgs(job_id="j", bogus=1)
    with pytest.raises(ValidationError):
        ListJobsArgs(status="exploded")
    with pytest.raises(ValidationError):
        LogExcerptArgs(job_id="j", stage="em", which="nope")


def test_set_plan_and_record_approval_tools(tmp_path):
    ctx, job_id, plan, digest = planned(tmp_path)
    assert len(digest) == 64
    out = record_approval_tool(ctx, RecordApprovalArgs(
        job_id=job_id, approval_token=TOKEN, kind="plan", plan_sha256=digest))
    assert out["approved_at"]
    with pytest.raises(SimAgentError) as exc:
        record_approval_tool(ctx, RecordApprovalArgs(
            job_id=job_id, approval_token="wrong", kind="plan", plan_sha256=digest))
    assert exc.value.code == ErrorCode.E_BAD_TOKEN


def test_get_job_reports_state_without_secrets(tmp_path):
    ctx, job_id, plan, digest = planned(tmp_path)
    before = get_job_tool(ctx, GetJobArgs(job_id=job_id))
    assert before["approval"] == {"present": False, "mode": None, "matches_plan": False}
    assert before["plan_sha256"] == digest and before["plan"]["stages"][0]["id"] == "S1"
    assert before["stage_status"]["build"] == "PASSED" and before["job_status"] == "planned"
    assert before["summary"] is None and before["execution_mode"] == "real"
    record_approval_tool(ctx, RecordApprovalArgs(
        job_id=job_id, approval_token=TOKEN, kind="plan", plan_sha256=digest))
    after = get_job_tool(ctx, GetJobArgs(job_id=job_id))
    assert after["approval"] == {"present": True, "mode": "interactive", "matches_plan": True}
    assert TOKEN not in json.dumps(after)


def test_get_job_returns_summary_content_when_it_exists(tmp_path):
    ctx, job_id, plan, digest = planned(tmp_path)
    job = tools_job.get_store(ctx).get(job_id)
    (job.stage_dir("analysis") / "summary.json").write_text(
        json.dumps({"schema": "simagent.summary/1"}), encoding="utf-8")
    assert get_job_tool(ctx, GetJobArgs(job_id=job_id))["summary"] == {
        "schema": "simagent.summary/1"}


def test_list_jobs_filters_and_names_the_title_field(tmp_path):
    ctx, job_id, plan, digest = planned(tmp_path)
    rows = list_jobs_tool(ctx, ListJobsArgs())["jobs"]
    assert [r["job_id"] for r in rows] == [job_id] and "title" in rows[0]
    assert list_jobs_tool(ctx, ListJobsArgs(status="planned"))["jobs"][0]["job_id"] == job_id
    assert list_jobs_tool(ctx, ListJobsArgs(status="failed"))["jobs"] == []
    assert list_jobs_tool(ctx, ListJobsArgs(limit=0))["jobs"] == []


def log_job(tmp_path):
    ctx, job_id, plan, digest = planned(tmp_path)
    job = tools_job.get_store(ctx).get(job_id)
    (job.stage_dir("em") / "em.log").write_text(
        "\n".join(f"line {i}" for i in range(100)) + "\nFinished mdrun on rank 0\n",
        encoding="utf-8")
    (job.stage_dir("em") / "grompp.out").write_text("grompp said hello\nNOTE 1\n", encoding="utf-8")
    (job.stage_dir("em") / "mdrun.err").write_text("oops\n", encoding="utf-8")
    return ctx, job_id


def test_get_log_excerpt_tail_regex_and_mapping(tmp_path):
    ctx, job_id = log_job(tmp_path)
    tail = get_log_excerpt(ctx, LogExcerptArgs(job_id=job_id, stage="em", lines=3))
    assert tail["lines"] == ["line 98", "line 99", "Finished mdrun on rank 0"]
    assert tail["path"] == "02_em/em.log"
    hits = get_log_excerpt(ctx, LogExcerptArgs(job_id=job_id, stage="em", regex=r"line 1\d$",
                                               lines=40))
    assert hits["lines"][0] == "line 10" and len(hits["lines"]) == 10
    assert get_log_excerpt(ctx, LogExcerptArgs(
        job_id=job_id, stage="em", which="grompp"))["lines"][0] == "grompp said hello"
    assert get_log_excerpt(ctx, LogExcerptArgs(
        job_id=job_id, stage="em", which="stderr"))["lines"] == ["oops"]


def test_get_log_excerpt_errors(tmp_path):
    ctx, job_id = log_job(tmp_path)
    for args, code in (
        (LogExcerptArgs(job_id=job_id, stage="em", lines=41), ErrorCode.E_PARAM_OUT_OF_RANGE),
        (LogExcerptArgs(job_id=job_id, stage="em", lines=0), ErrorCode.E_PARAM_OUT_OF_RANGE),
        (LogExcerptArgs(job_id=job_id, stage="em", regex="("), ErrorCode.E_PARAM_OUT_OF_RANGE),
        (LogExcerptArgs(job_id=job_id, stage="nvt"), ErrorCode.E_ANALYSIS_INPUT),
    ):
        with pytest.raises(SimAgentError) as exc:
            get_log_excerpt(ctx, args)
        assert exc.value.code == code


def test_list_artifacts_reports_receipts_and_kinds(tmp_path):
    ctx, job_id, plan, digest = planned(tmp_path)
    job = tools_job.get_store(ctx).get(job_id)
    (job.stage_dir("em") / "stray.txt").write_text("no receipt", encoding="utf-8")
    files = {f["path"]: f for f in list_artifacts(ctx, ArtifactsArgs(job_id=job_id))["files"]}
    top = files["01_build/topol.top"]
    assert top["has_receipt"] is True and top["kind"] == "internal" and top["produced_by_seq"]
    assert top["bytes"] > 0 and len(top["sha256"]) == 64
    stray = files["02_em/stray.txt"]
    assert stray["has_receipt"] is False and stray["kind"] is None
    only_build = list_artifacts(ctx, ArtifactsArgs(job_id=job_id, stage="build"))["files"]
    assert all(f["path"].startswith("01_build/") for f in only_build)
    assert not any("_dry" in f["path"] for f in files.values())
