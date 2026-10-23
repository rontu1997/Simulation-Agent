import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_approval import REQUEST, TOKEN
from test_build import StubRunner
from test_job_tools import planned

from simagent.core.mdp import render_mdp
from simagent.core.paths import sha256_file
from simagent.core.provenance import file_entry
from simagent.core.runs import ProcSnapshot, update_run_json
from simagent.errors import ErrorCode, SimAgentError
from simagent.mcp_server.registry import TOOLS
from simagent.mcp_server.tools_exec import (
    CancelArgs,
    GromppArgs,
    MdrunArgs,
    RetryArg,
    StatusArgs,
    ValidateArgs,
    WriteMdpArgs,
    cancel_run_tool,
    get_run_status_tool,
    run_grompp_tool,
    run_mdrun_tool,
    validate_stage_tool,
    write_mdp_tool,
)
from simagent.mcp_server.tools_job import (
    GetJobArgs,
    RecordApprovalArgs,
    get_job_tool,
    get_store,
    record_approval_tool,
)

WRAPPER_CMD = "python -m simagent.core.mdrun_wrapper /x"


class RecordingRunner(StubRunner):
    """StubRunner that also writes the receipts a real GmxRunner would write."""

    def __init__(self, ledger):
        super().__init__()
        self.ledger = ledger

    def run(self, args, *, cwd, stage="", tool="", outputs=(), base=None, **kwargs):
        result = super().run(args, cwd=cwd)
        if outputs and result.exit_code == 0:
            self.ledger.append({
                "event": "end", "kind": "gmx", "stage": stage, "tool": tool,
                "outputs": [file_entry(base, Path(p).relative_to(base).as_posix())
                            for p in outputs if Path(p).exists()]})
        return result


class FakeLauncher:
    def __init__(self):
        self.calls = []

    def __call__(self, stage_dir, argv, env, *, ledger_path, stage, max_wall_hours):
        self.calls.append({"stage_dir": Path(stage_dir), "argv": argv, "env": dict(env),
                           "stage": stage, "max_wall_hours": max_wall_hours})
        update_run_json(Path(stage_dir), lambda d: d.update(
            state="launching", launched_at=time.time(), pid=4242, samples=[]))
        return 4242


def exec_ctx(tmp_path, env=None):
    ctx, job_id, plan, digest = planned(tmp_path, env)
    binary = tmp_path / "gmx"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    ctx.cfg = replace(ctx.cfg, gmx=replace(ctx.cfg.gmx, bin=str(binary)))
    job = get_store(ctx).get(job_id)
    stat = binary.stat()
    job.update(lambda d: d["environment"].update(
        sha256=sha256_file(binary), bin_stat={"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}))
    ctx.extras["runner_factory"] = lambda cfg, ledger: RecordingRunner(ledger)
    ctx.extras["launch_wrapper"] = FakeLauncher()
    return ctx, job_id, plan, digest


def approve(ctx, job_id, digest):
    record_approval_tool(ctx, RecordApprovalArgs(
        job_id=job_id, approval_token=TOKEN, kind="plan", plan_sha256=digest))


def prepare(ctx, job_id, stage="em"):
    write_mdp_tool(ctx, WriteMdpArgs(job_id=job_id, stage=stage))
    run_grompp_tool(ctx, GromppArgs(job_id=job_id, stage=stage))


def code_of(fn, *args):
    with pytest.raises(SimAgentError) as exc:
        fn(*args)
    return exc.value.code


def set_status(job, **statuses):
    """Force stage statuses (bypasses the transition table; the tests stage their own states)."""
    job.update(lambda d: d["stage_status"].update(statuses))


def test_tools_registered_and_extras_forbidden():
    assert {"write_mdp", "run_grompp", "run_mdrun", "get_run_status", "cancel_run",
            "validate_stage"} <= set(TOOLS)
    with pytest.raises(ValidationError):
        WriteMdpArgs(job_id="j", stage="em", bogus=1)
    with pytest.raises(ValidationError):
        WriteMdpArgs(job_id="j", stage="build")
    with pytest.raises(ValidationError):
        RetryArg(key="nsteps", value=1)


def test_write_mdp_renders_from_plan_and_records_the_hash(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    out = write_mdp_tool(ctx, WriteMdpArgs(job_id=job_id, stage="em"))
    job = get_store(ctx).get(job_id)
    expected = render_mdp("em", plan, REQUEST, [])
    assert out["path"] == "02_em/em.mdp" and len(out["rendered_sha256"]) == 64
    assert (job.stage_dir("em") / "em.mdp").read_text(encoding="utf-8") == expected
    assert job.read()["mdp_expected_sha256"]["em"] == out["rendered_sha256"]
    assert out["params"]["emtol"] == 1000.0
    write_mdp_tool(ctx, WriteMdpArgs(job_id=job_id, stage="em"))  # PENDING: repeatable
    run_grompp_tool(ctx, GromppArgs(job_id=job_id, stage="em"))
    assert code_of(write_mdp_tool, ctx, WriteMdpArgs(job_id=job_id, stage="em")) \
        == ErrorCode.E_STAGE_STATE


def test_write_mdp_before_a_plan_is_stage_order(tmp_path):
    from test_job_tools import make_ctx

    from simagent.mcp_server.tools_job import CreateJobArgs, create_job
    ctx = make_ctx(tmp_path)
    made = create_job(ctx, CreateJobArgs(request=dict(REQUEST)))
    assert code_of(write_mdp_tool, ctx, WriteMdpArgs(job_id=made["job_id"], stage="em")) \
        == ErrorCode.E_STAGE_ORDER


def test_write_mdp_retry_and_reset(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    approve(ctx, job_id, digest)
    job = get_store(ctx).get(job_id)
    retry = lambda value, **kw: WriteMdpArgs(  # noqa: E731
        job_id=job_id, stage="em", retry=RetryArg(key="emtol", value=value), **kw)
    assert code_of(write_mdp_tool, ctx, retry(500.0)) == ErrorCode.E_RETRY_NOT_ALLOWED  # no failure
    prepare(ctx, job_id)
    set_status(job, em="FAILED")
    job.update(lambda d: d["failures"].append({"stage": "em", "cause": "gate failed: converged"}))
    assert code_of(write_mdp_tool, ctx, WriteMdpArgs(
        job_id=job_id, stage="nvt", retry=RetryArg(key="emtol", value=500.0))
    ) == ErrorCode.E_RETRY_NOT_ALLOWED  # emtol only applies to em
    assert code_of(write_mdp_tool, ctx, retry(2000.0)) == ErrorCode.E_RETRY_NOT_ALLOWED  # looser

    out = write_mdp_tool(ctx, retry(500.0))
    data = job.read()
    assert out["params"]["emtol"] == 500.0
    assert data["plan_sha256"] == digest and data["approval"]["plan_sha256"] == digest
    assert data["retries"][0]["key"] == "emtol" and data["retries"][0]["failing_stage"] == "em"
    assert data["stage_status"]["em"] == "PENDING"
    assert (job.dir / "superseded").is_dir()
    assert (job.stage_dir("em") / "em.mdp").read_text(encoding="utf-8") == render_mdp(
        "em", plan, REQUEST, data["retries"])

    write_mdp_tool(ctx, retry(400.0))
    assert code_of(write_mdp_tool, ctx, retry(300.0)) == ErrorCode.E_RETRY_LIMIT

    set_status(job, em="FAILED")
    before = len(job.read()["retries"])
    write_mdp_tool(ctx, WriteMdpArgs(job_id=job_id, stage="em", reset=True))
    data = job.read()
    assert data["stage_status"]["em"] == "PENDING" and len(data["retries"]) == before
    assert data["approval"]["plan_sha256"] == digest
    assert code_of(write_mdp_tool, ctx, WriteMdpArgs(job_id=job_id, stage="em", reset=True)
                   ) == ErrorCode.E_STAGE_STATE  # reset needs FAILED or CANCELLED
    set_status(job, nvt="RUNNING")
    assert code_of(write_mdp_tool, ctx, WriteMdpArgs(job_id=job_id, stage="em", reset=True)
                   ) == ErrorCode.E_MDRUN_RUNNING


def test_dt_retry_rescales_later_stages_and_keeps_the_plan_hash(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    job = get_store(ctx).get(job_id)
    set_status(job, em="PASSED", nvt="FAILED")
    job.update(lambda d: d["failures"].append({"stage": "nvt", "cause": "process_died"}))
    out = write_mdp_tool(ctx, WriteMdpArgs(
        job_id=job_id, stage="nvt", retry=RetryArg(key="dt", value=0.001)))
    assert out["params"]["dt"] == 0.001 and out["params"]["nsteps"] == 100000
    assert get_job_tool(ctx, GetJobArgs(job_id=job_id))["plan_sha256"] == digest
    assert job.read()["stage_status"]["em"] == "PASSED"  # earlier stages are kept


def test_run_grompp_tool_marks_the_stage_ready(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    write_mdp_tool(ctx, WriteMdpArgs(job_id=job_id, stage="em"))
    out = run_grompp_tool(ctx, GromppArgs(job_id=job_id, stage="em"))
    assert out["tpr"] == "02_em/em.tpr"
    assert get_store(ctx).get(job_id).status("em") == "READY"


def test_run_mdrun_check_order_and_states(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    job = get_store(ctx).get(job_id)
    run = lambda stage="em", resume=False: run_mdrun_tool(  # noqa: E731
        ctx, MdrunArgs(job_id=job_id, stage=stage, resume=resume))
    assert code_of(run) == ErrorCode.E_NOT_APPROVED  # before every stage check
    approve(ctx, job_id, digest)
    assert code_of(run) == ErrorCode.E_STAGE_STATE  # em is PENDING
    assert code_of(run, "nvt") == ErrorCode.E_STAGE_ORDER  # em has not passed
    set_status(job, nvt="RUNNING")
    assert code_of(run) == ErrorCode.E_MDRUN_RUNNING  # before order and state
    set_status(job, nvt="PENDING")

    prepare(ctx, job_id)
    mdp = job.stage_dir("em") / "em.mdp"
    mdp.write_text(mdp.read_text(encoding="utf-8").replace("steep", "cg"), encoding="utf-8")
    assert code_of(run) == ErrorCode.E_INPUT_MODIFIED  # stale .mdp
    write_mdp_tool(ctx, WriteMdpArgs(job_id=job_id, stage="em", reset=True)) \
        if job.status("em") in ("FAILED", "CANCELLED") else None

    (tmp_path / "gmx").write_text("#!/bin/sh\necho changed\n", encoding="utf-8")
    os.utime(tmp_path / "gmx", (1, 1))
    assert code_of(run) == ErrorCode.E_BINARY_CHANGED  # checked before approval and state


def test_run_mdrun_needs_an_approval_mechanism(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path, env={})
    assert code_of(run_mdrun_tool, ctx, MdrunArgs(job_id=job_id, stage="em", resume=False)
                   ) == ErrorCode.E_APPROVAL_UNAVAILABLE


def test_run_mdrun_launches_the_wrapper_and_marks_running(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    approve(ctx, job_id, digest)
    prepare(ctx, job_id)
    out = run_mdrun_tool(ctx, MdrunArgs(job_id=job_id, stage="em", resume=False))
    call = ctx.extras["launch_wrapper"].calls[0]
    gmx = ctx.cfg.gmx.bin
    assert call["argv"] == [gmx, "mdrun", "-deffnm", "em", "-cpt", "15", "-ntmpi", "1"]
    assert call["stage"] == "em" and call["max_wall_hours"] == ctx.cfg.gmx.max_wall_hours
    assert "SIMAGENT_APPROVAL_TOKEN" not in call["env"] and "GMXDATA" in call["env"]
    assert out["pid"] == 4242 and out["log_path"] == "02_em/em.log" and out["started_at"]
    assert get_store(ctx).get(job_id).status("em") == "RUNNING"
    assert TOKEN not in json.dumps(out)


def test_launch_failure_leaves_the_stage_ready(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    approve(ctx, job_id, digest)
    prepare(ctx, job_id)

    def broken(*args, **kwargs):
        raise OSError("no fork")

    ctx.extras["launch_wrapper"] = broken
    assert code_of(run_mdrun_tool, ctx, MdrunArgs(job_id=job_id, stage="em", resume=False)
                   ) == ErrorCode.E_MDRUN_FAILED
    assert get_store(ctx).get(job_id).status("em") == "READY"


def test_resume_rules(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    approve(ctx, job_id, digest)
    prepare(ctx, job_id)
    job = get_store(ctx).get(job_id)
    stage_dir = job.stage_dir("em")
    resume = lambda: run_mdrun_tool(  # noqa: E731
        ctx, MdrunArgs(job_id=job_id, stage="em", resume=True))

    set_status(job, em="FAILED")
    job.update(lambda d: d["failures"].append({"stage": "em", "cause": "process_died"}))
    assert code_of(resume) == ErrorCode.E_STAGE_STATE  # no checkpoint
    (stage_dir / "em.cpt").write_text("cpt", encoding="utf-8")
    for name, text in (("run.json", "{}"), ("run.exit", "-9\n"), ("run.cancelled", "")):
        (stage_dir / name).write_text(text, encoding="utf-8")
    resume()
    call = ctx.extras["launch_wrapper"].calls[-1]
    assert call["argv"][-3:] == ["-cpi", "em.cpt", "-append"]
    history = stage_dir / "run_history" / "1"
    assert (history / "run.exit").is_file() and (history / "run.cancelled").is_file()
    assert not (stage_dir / "run.exit").exists() and not (stage_dir / "run.cancelled").exists()
    assert job.status("em") == "RUNNING"

    set_status(job, em="CANCELLED")
    resume()
    assert (stage_dir / "run_history" / "2").is_dir()

    set_status(job, em="FAILED")
    job.update(lambda d: d["failures"].append({"stage": "em", "cause": "gate failed: converged"}))
    assert code_of(resume) == ErrorCode.E_STAGE_STATE  # a failed gate needs a reset


def test_get_run_status_not_started_and_finished(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    status = lambda stage="em": get_run_status_tool(  # noqa: E731
        ctx, StatusArgs(job_id=job_id, stage=stage))
    assert status()["state"] == "not_started" and status()["log_tail"] == []
    job = get_store(ctx).get(job_id)
    stage_dir = job.stage_dir("em")
    (stage_dir / "em.log").write_text(
        "Step Time\n 5000 10.0\n\nPerformance:      139.375        0.172\nFinished mdrun\n",
        encoding="utf-8")
    (stage_dir / "run.json").write_text('{"state": "running", "pid": 1}', encoding="utf-8")
    (stage_dir / "run.exit").write_text("0\n", encoding="utf-8")
    out = status()
    assert out["state"] == "finished" and out["exit_code"] == 0
    assert out["step"] == 5000 and out["time_ps"] == 10.0 and out["ns_per_day"] == 139.375
    assert out["log_tail"][-1] == "Finished mdrun"
    assert code_of(status, "analysis") == ErrorCode.E_PARAM_OUT_OF_RANGE


def test_get_run_status_estimates_rate_and_eta_from_samples(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    job = get_store(ctx).get(job_id)
    stage_dir = job.stage_dir("nvt")
    (stage_dir / "run.json").write_text(json.dumps({
        "state": "running", "pid": 4242, "start_time_ticks": 777, "launched_at": 0}),
        encoding="utf-8")
    ctx.extras["proc_snapshot"] = lambda pid: ProcSnapshot(True, 777, WRAPPER_CMD)
    now = {"t": 1000.0}
    ctx.extras["clock"] = lambda: now["t"]
    log = stage_dir / "nvt.log"

    def poll(time_ps):
        log.write_text(f"Step Time\n 100 {time_ps}\n", encoding="utf-8")
        return get_run_status_tool(ctx, StatusArgs(job_id=job_id, stage="nvt"))

    first = poll(20.0)
    assert first["state"] == "running" and first["ns_per_day"] is None and first["eta_s"] is None
    now["t"] = 1060.0
    second = poll(26.0)
    assert second["ns_per_day"] == pytest.approx(8.64)
    assert second["eta_s"] == pytest.approx(740.0)  # 100 ps planned, 26 ps done
    assert len(json.loads((stage_dir / "run.json").read_text(encoding="utf-8"))["samples"]) == 2


def test_cancel_run_is_a_no_op_when_idle(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    out = cancel_run_tool(ctx, CancelArgs(job_id=job_id, stage="em"))
    assert out["cancelled"] is False
    assert get_store(ctx).get(job_id).status("em") == "PENDING"


def test_cancel_run_stops_a_live_wrapper(tmp_path):
    from simagent.core.mdrun_wrapper import launch_wrapper

    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    job = get_store(ctx).get(job_id)
    stage_dir = job.stage_dir("em")
    launch_wrapper(stage_dir, [sys.executable, "-c", "import time; time.sleep(60)"],
                   dict(os.environ), ledger_path=job.dir / "provenance.jsonl", stage="em",
                   max_wall_hours=1.0)
    set_status(job, em="READY")
    set_status(job, em="RUNNING")
    deadline = time.time() + 20
    while time.time() < deadline and "gmx_pid" not in json.loads(
            (stage_dir / "run.json").read_text(encoding="utf-8")):
        time.sleep(0.1)
    out = cancel_run_tool(ctx, CancelArgs(job_id=job_id, stage="em"))
    assert out["cancelled"] is True
    assert (stage_dir / "run.cancelled").exists() and (stage_dir / "run.exit").exists()
    assert job.status("em") == "CANCELLED"
    assert get_run_status_tool(ctx, StatusArgs(job_id=job_id, stage="em"))["state"] == "cancelled"
    assert any(r["event"] == "end" and r["stage"] == "em" for r in job.ledger.records())


def end_record(job, stage, names):
    job.ledger.append({"event": "end", "kind": "gmx", "stage": stage, "tool": "run_mdrun",
                       "outputs": [file_entry(job.dir, f"{job.stage_dir(stage).name}/{n}")
                                   for n in names]})


def finish_run(job, stage, exit_code=0):
    stage_dir = job.stage_dir(stage)
    (stage_dir / "run.json").write_text('{"state": "running", "pid": 1}', encoding="utf-8")
    (stage_dir / "run.exit").write_text(f"{exit_code}\n", encoding="utf-8")


def test_validate_stage_reports_not_run_for_a_stage_that_never_ran(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    out = validate_stage_tool(ctx, ValidateArgs(job_id=job_id, stage="nvt"))
    assert out["passed"] is False and out["checks"][0]["status"] == "not_run"


def test_validate_stage_simulated_skips_physical_checks(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    job = get_store(ctx).get(job_id)
    job.update(lambda d: d.update(execution_mode="simulated"))
    (job.stage_dir("em") / "em.log").write_text("Finished mdrun\n", encoding="utf-8")
    end_record(job, "em", ["em.log"])
    set_status(job, em="READY")
    set_status(job, em="RUNNING")
    finish_run(job, "em")
    out = validate_stage_tool(ctx, ValidateArgs(job_id=job_id, stage="em"))
    status = {c["name"]: c["status"] for c in out["checks"]}
    assert out["passed"] is True and status["converged"] == "skipped_simulated"
    assert status["process_state"] == "pass" and status["receipts"] == "pass"
    assert job.status("em") == "PASSED"


def test_validate_stage_real_em_gate(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    job = get_store(ctx).get(job_id)
    (job.stage_dir("em") / "em.log").write_text(
        "Steepest Descents converged to Fmax < 1000 in 87 steps\n"
        "Potential Energy  = -4.1000000e+04\nMaximum force     =  8.5e+02 on atom 17\n"
        "Norm of force     =  1.2e+02\n", encoding="utf-8")
    end_record(job, "em", ["em.log"])
    set_status(job, em="READY")
    set_status(job, em="RUNNING")
    finish_run(job, "em")
    out = validate_stage_tool(ctx, ValidateArgs(job_id=job_id, stage="em"))
    status = {c["name"]: c["status"] for c in out["checks"]}
    assert out["passed"] is True, out
    assert status["converged"] == "pass" and status["epot_finite_negative"] == "pass"
    assert job.status("em") == "PASSED"


def test_validate_stage_missing_receipts_fail_the_gate(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    job = get_store(ctx).get(job_id)
    job.update(lambda d: d.update(execution_mode="simulated"))
    (job.stage_dir("em") / "em.log").write_text("Finished mdrun\n", encoding="utf-8")
    set_status(job, em="READY")
    set_status(job, em="RUNNING")
    finish_run(job, "em")
    out = validate_stage_tool(ctx, ValidateArgs(job_id=job_id, stage="em"))
    assert out["passed"] is False
    assert {c["name"]: c["status"] for c in out["checks"]}["receipts"] == "no_receipt"
    assert job.status("em") == "FAILED"


def test_validate_stage_died_process_fails_the_stage(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    job = get_store(ctx).get(job_id)
    set_status(job, em="READY")
    set_status(job, em="RUNNING")
    finish_run(job, "em", exit_code=1)
    out = validate_stage_tool(ctx, ValidateArgs(job_id=job_id, stage="em"))
    assert out["passed"] is False and out["checks"][0]["name"] == "process_died"
    assert job.status("em") == "FAILED"
    assert job.read()["failures"][-1]["cause"] == "process_died"


def test_validate_stage_cancelled_is_an_error(tmp_path):
    ctx, job_id, plan, digest = exec_ctx(tmp_path)
    job = get_store(ctx).get(job_id)
    set_status(job, em="READY")
    set_status(job, em="RUNNING")
    set_status(job, em="CANCELLED")
    assert code_of(validate_stage_tool, ctx, ValidateArgs(job_id=job_id, stage="em")
                   ) == ErrorCode.E_CANCELLED
