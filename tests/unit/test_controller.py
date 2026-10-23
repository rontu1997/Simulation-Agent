import asyncio
import time
from typing import Any

import pytest

from simagent.agent.controller import POLL_FLOOR_S, Controller, NoDecisions, Outcome
from simagent.config import load_config

STAGES = ("build", "em", "nvt", "npt", "prod", "analysis")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def ok(data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": True, "data": data or {}, "error": None}


def err(code: str, message: str = "boom", hint: str = "", data=None) -> dict[str, Any]:
    return {"ok": False, "data": data or {}, "error": {"code": code, "message": message,
                                                      "hint": hint}}


def job_data(**status: str) -> dict[str, Any]:
    stage_status = {s: "PASSED" for s in STAGES}
    stage_status["analysis"] = "PENDING"
    stage_status.update(status)
    return {"job_id": "j1", "stage_status": stage_status, "failures": [], "retries": [],
            "plan": {"automatic_retries_allowed": []}, "execution_mode": "simulated",
            "approval": {"present": True, "mode": "interactive", "matches_plan": True}}


ALL_PASSED = job_data()


class FakeTools:
    """Scripted tool double. Each tool maps to a list; the last item repeats forever."""

    def __init__(self, script: dict[str, list[Any]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        queue = self.script.get(name)
        if not queue:
            raise AssertionError(f"unscripted call: {name} {args}")
        item = queue[0] if len(queue) == 1 else queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(args) if callable(item) else item

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


def base_script(**extra) -> dict[str, list[Any]]:
    script: dict[str, list[Any]] = {
        "gmx_check_environment": [ok({"execution_mode": "simulated"})],
        "get_job": [ok(ALL_PASSED)],
    }
    script.update(extra)
    return script


class Sleeper:
    def __init__(self):
        self.intervals: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.intervals.append(seconds)


def make(tools, decisions=None, sleeper=None, **kwargs) -> Controller:
    return Controller(tools, load_config(env={}), decisions, sleep=sleeper or Sleeper(), **kwargs)


class Decider(NoDecisions):
    def __init__(self, accept: list[str] | None = None, interrupted: str = "stop"):
        self.accept = accept if accept is not None else []
        self.interrupted = interrupted
        self.seen: list[Any] = []

    async def acknowledge_warnings(self, job_id, warnings):
        self.seen.append(("warnings", job_id, warnings))
        return list(self.accept)

    async def on_interrupted(self, job_id, stage, cause, can_resume):
        self.seen.append(("interrupted", job_id, stage, cause, can_resume))
        return self.interrupted


@pytest.mark.anyio
async def test_start_check_failure_stops_before_any_job_call():
    tools = FakeTools({"gmx_check_environment": [err(
        "E_GMX_NOT_FOUND", "GROMACS binary not found", "check GMX_BIN")]})
    outcome = await make(tools).run("j1")
    assert outcome.status == "failed" and "not found" in outcome.message
    assert "check GMX_BIN" in outcome.message
    assert tools.names() == ["gmx_check_environment"]


@pytest.mark.anyio
async def test_all_stages_passed_is_done_without_running_anything():
    tools = FakeTools(base_script())
    outcome = await make(tools).run("j1")
    assert outcome.status == "done" and outcome.stage == "prod"
    assert tools.names() == ["gmx_check_environment", "get_job"]


@pytest.mark.anyio
async def test_runs_a_pending_stage_in_order():
    events: list[dict[str, Any]] = []
    tools = FakeTools(base_script(
        get_job=[ok(job_data(em="PENDING")), ok(ALL_PASSED)],
        write_mdp=[ok({"path": "02_em/em.mdp"})],
        run_grompp=[ok({"tpr": "02_em/em.tpr", "warnings": []})],
        run_mdrun=[ok({"pid": 1})],
        get_run_status=[ok({"state": "running"}), ok({"state": "finished", "exit_code": 0})],
        validate_stage=[ok({"passed": True, "checks": []})]))
    outcome = await make(tools, on_event=events.append).run("j1")
    assert outcome.status == "done"
    assert tools.names() == [
        "gmx_check_environment", "get_job", "write_mdp", "run_grompp", "run_mdrun",
        "get_run_status", "get_run_status", "validate_stage", "get_job"]
    assert tools.calls[2][1] == {"job_id": "j1", "stage": "em"}
    assert tools.calls[4][1] == {"job_id": "j1", "stage": "em", "resume": False}
    assert [e["state"] for e in events][:1] == ["EXECUTE"] and events[-1]["state"] == "DONE"


@pytest.mark.anyio
async def test_polling_floor():
    sleeper = Sleeper()
    tools = FakeTools(base_script(
        get_job=[ok(job_data(prod="RUNNING")), ok(ALL_PASSED)],
        get_run_status=[ok({"state": "running"}), ok({"state": "running"}),
                        ok({"state": "finished"})],
        validate_stage=[ok({"passed": True, "checks": []})]))
    controller = make(tools, sleeper=sleeper, poll_interval_s=0.1)
    assert (await controller.run("j1")).status == "done"
    assert sleeper.intervals == [POLL_FLOOR_S, POLL_FLOOR_S]
    assert POLL_FLOOR_S == 10.0


@pytest.mark.anyio
async def test_cancel_during_poll_sleep():
    tools = FakeTools(base_script(
        get_job=[ok(job_data(prod="RUNNING"))],
        get_run_status=[ok({"state": "running"})],
        cancel_run=[ok({"cancelled": True, "signal_sent": "SIGTERM"})]))
    controller = Controller(tools, load_config(env={}), poll_interval_s=10.0)  # real sleeper
    task = asyncio.ensure_future(controller.run("j1"))
    for _ in range(100):
        if tools.names().count("get_run_status") >= 1:
            break
        await asyncio.sleep(0.01)
    started = time.monotonic()
    result = await controller.cancel("j1")
    outcome = await asyncio.wait_for(task, timeout=3)
    assert time.monotonic() - started < 1.0
    assert result["ok"] and outcome.status == "cancelled" and outcome.stage == "prod"
    assert "validate_stage" not in tools.names()
    cancel_call = next(c for c in tools.calls if c[0] == "cancel_run")
    assert cancel_call[1] == {"job_id": "j1", "stage": "prod"}


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [
    BrokenPipeError("server gone"), {"unexpected": "shape"}, None])
async def test_tool_failure_stops_the_run(failure):
    tools = FakeTools(base_script(
        get_job=[ok(job_data(em="READY"))], run_mdrun=[failure]))
    outcome = await make(tools).run("j1")
    assert outcome.status == "failed" and "run_mdrun" in outcome.message
    assert tools.names() == ["gmx_check_environment", "get_job", "run_mdrun"]


@pytest.mark.anyio
async def test_call_budget_stops_with_limit():
    tools = FakeTools(base_script(get_job=[ok(job_data(em="READY"))],
                                  run_mdrun=[ok({"pid": 1})]))
    outcome = await make(tools, max_tool_calls=3).run("j1")
    assert outcome.status == "limit" and len(tools.calls) == 3


@pytest.mark.anyio
async def test_warnings_flow():
    warning = {"id": "w1", "text": "a warning", "source": "topol.top:3", "acknowledged": False}
    decider = Decider(accept=["w1"])
    tools = FakeTools(base_script(
        get_job=[ok(job_data(em="PENDING")), ok(ALL_PASSED)],
        write_mdp=[ok()],
        run_grompp=[err("E_GROMPP_FAILED", "warnings", data={"warnings": [warning]}),
                    ok({"tpr": "x", "warnings": [{**warning, "acknowledged": True}]})],
        run_mdrun=[ok({"pid": 1})], get_run_status=[ok({"state": "finished"})],
        validate_stage=[ok({"passed": True, "checks": []})]))
    outcome = await make(tools, decider).run("j1")
    assert outcome.status == "done"
    assert tools.names().count("run_grompp") == 2 and "record_approval" not in tools.names()
    assert decider.seen == [("warnings", "j1", [warning])]


@pytest.mark.anyio
async def test_declined_warning_escalates():
    warning = {"id": "w1", "text": "a very specific warning", "source": "t:3",
               "acknowledged": False}
    tools = FakeTools(base_script(
        get_job=[ok(job_data(em="PENDING"))], write_mdp=[ok()],
        run_grompp=[err("E_GROMPP_FAILED", "warnings", data={"warnings": [warning]})]))
    outcome = await make(tools, Decider(accept=[])).run("j1")
    assert outcome.status == "escalate" and "a very specific warning" in outcome.message
    assert tools.names().count("run_grompp") == 1 and "run_mdrun" not in tools.names()


@pytest.mark.anyio
async def test_stage_already_passed_is_skipped_and_ready_goes_straight_to_mdrun():
    tools = FakeTools(base_script(
        get_job=[ok(job_data(nvt="READY")), ok(ALL_PASSED)],
        run_mdrun=[ok({"pid": 1})], get_run_status=[ok({"state": "finished"})],
        validate_stage=[ok({"passed": True, "checks": []})]))
    outcome = await make(tools).run("j1")
    assert outcome.status == "done"
    assert "write_mdp" not in tools.names() and "run_grompp" not in tools.names()
    assert tools.calls[tools.names().index("run_mdrun")][1]["stage"] == "nvt"


@pytest.mark.anyio
async def test_failed_validation_without_diagnosis_escalates_for_now():
    tools = FakeTools(base_script(
        get_job=[ok(job_data(prod="RUNNING"))],
        get_run_status=[ok({"state": "finished"})],
        validate_stage=[ok({"passed": False, "checks": [
            {"name": "mean_temperature", "status": "fail"}]})]))
    outcome = await make(tools).run("j1")
    assert isinstance(outcome, Outcome) and outcome.status == "escalate"
    assert outcome.stage == "prod"
