import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from test_controller import FakeServer, ok
from test_controller import err as envelope_error

from scripted import ScriptedConsole, StubLlm, say, submit
from simagent.agent.controller import Outcome
from simagent.agent.session import Session, SessionDecisions, final_message
from simagent.config import load_config
from simagent.core.provenance import Ledger

TOKEN = "tok-session-0123456789"
JOB_ID = "water-1000-tip4p-300K-20261023-1"
PROCESS = ("em", "nvt", "npt", "prod")


@pytest.fixture
def anyio_backend():
    return "asyncio"


class SessionServer(FakeServer):
    """The controller tests' fake server plus the tools the session itself calls."""

    def __init__(self, tmp_path: Path | None = None, warnings=None, mode="simulated", **kw):
        super().__init__(**kw)
        self.tmp = tmp_path
        self.warnings = warnings or []
        self.mode = mode
        self.request: dict[str, Any] | None = None
        self.plan: dict[str, Any] | None = None
        self.digest: str | None = None
        self.approval: dict[str, Any] | None = None
        self.acked: list[str] = []
        self.sets = 0

    async def _gmx_check_environment(self, args):
        return ok({"version": "2025.3", "execution_mode": self.mode,
                   "probe": {"ok": True, "wall_s": 0.5}})

    async def _create_job(self, args):
        self.request = args["request"]
        return ok({"job_id": JOB_ID, "job_dir": str(self.tmp), "warnings": [], "environment": {}})

    async def _build_water_box(self, args):
        return ok({"box_nm": 3.107, "build_sha256": "b" * 64, "target_density_g_cm3": 0.997,
                   "n_molecules": args["n_molecules"], "water_model": args["water_model"],
                   "n_atoms": 4 * args["n_molecules"]})

    async def _set_plan(self, args):
        self.request, self.plan = args["request"], args["plan"]
        self.sets += 1
        self.digest = f"{self.sets:064d}"
        self.approval = None
        return ok({"plan_sha256": self.digest, "invalidated": []})

    async def _get_job(self, args):
        approval = self.approval
        return ok({
            "job_id": JOB_ID, "request": self.request, "plan": self.plan,
            "plan_sha256": self.digest, "stage_status": dict(self.status),
            "failures": list(self.failures), "retries": [dict(r) for r in self.retries],
            "execution_mode": self.mode, "acknowledged_warnings": list(self.acked),
            "approval": {"present": bool(approval), "mode": approval and approval["mode"],
                         "matches_plan": bool(approval)}})

    async def _record_approval(self, args):
        if args["kind"] == "plan":
            if args["plan_sha256"] != self.digest:
                return envelope_error("E_PLAN_MISMATCH", "hash differs", "read again")
            self.approval = {"mode": args["mode"], "plan_sha256": args["plan_sha256"]}
        else:
            self.acked += list(args["warning_ids"])
        return ok({"approved_at": "now"})

    async def _run_grompp(self, args):
        open_ids = [w for w in self.warnings if w["id"] not in self.acked]
        if open_ids:
            shown = [{**w, "acknowledged": w["id"] in self.acked} for w in self.warnings]
            return envelope_error("E_GROMPP_FAILED", "warnings", "decide", {"warnings": shown})
        return await super()._run_grompp(args)

    async def _list_jobs(self, args):
        return ok({"jobs": [{"job_id": JOB_ID, "created": "2026-10-23", "title": "1000 tip4p",
                             "status": "planned", "execution_mode": self.mode,
                             "path": str(self.tmp)}]})

    async def _list_artifacts(self, args):
        return ok({"files": [{"path": "02_em/em.tpr", "bytes": 12, "has_receipt": True}]})


def make(server, console, llm=None, **kwargs) -> Session:
    async def quick(seconds: float) -> None:
        await asyncio.sleep(0.01)

    kwargs.setdefault("sleep", quick)
    return Session(server, llm, load_config(env={}), console, TOKEN, **kwargs)


FULL_SCRIPT = [
    "run 1000 tip4p waters for 1 ns",
    ("Details I need", "yes yes yes"),
    ("production run", "yes yes"),
]


async def finish_conversation(server, inputs, llm, release="Run finished", **kwargs):
    console = ScriptedConsole(inputs, release_on=release)
    session = make(server, console, llm, **kwargs)
    assert await session.start()
    await session.chat()
    return session, console


@pytest.mark.anyio
async def test_failing_environment_check_stops_the_session():
    server = SessionServer()

    async def failing(args):
        return envelope_error("E_GMX_NOT_FOUND", "GROMACS binary not found", "check GMX_BIN")

    server._gmx_check_environment = failing
    console = ScriptedConsole([])
    assert await make(server, console).start() is False
    assert "GROMACS binary not found" in console.transcript
    assert "check GMX_BIN" in console.transcript
    assert server.names() == ["gmx_check_environment"]


@pytest.mark.anyio
async def test_start_prints_mode_and_probe():
    console = ScriptedConsole([])
    assert await make(SessionServer(), console).start() is True
    assert "2025.3" in console.transcript and "simulated" in console.transcript


@pytest.mark.anyio
async def test_full_conversation_asks_at_most_three_questions_none_stated(tmp_path):
    server = SessionServer(tmp_path)
    llm = StubLlm([submit(n_molecules=1000, water_model="tip4p"), say("Plan ready.")])
    inputs = [*FULL_SCRIPT, ("Approve?", "y")]
    session, console = await finish_conversation(server, inputs, llm)
    text = console.transcript
    questions = [line for line in text.splitlines() if line.strip()[:2] in
                 ("1.", "2.", "3.")]
    assert 3 <= len(questions) <= 5
    assert not any("molecules" in q or "water model" in q.lower() for q in questions)
    assert server.sets == 1 and len(server.plan["stages"]) == 7
    assert "seed 1904784231*" in text and "Estimated disk" in text
    assert server.approval == {"mode": "interactive", "plan_sha256": server.digest}
    assert "Run finished" in text and server.status["prod"] == "PASSED"


@pytest.mark.anyio
@pytest.mark.parametrize("answers", [["", "", "", "", ""], ["no"] * 6, ["maybe later"] * 6])
async def test_clarification_is_bounded(tmp_path, answers):
    server = SessionServer(tmp_path)
    llm = StubLlm([submit(n_molecules=1000, water_model="tip4p"), say("What else?")])
    session, console = await finish_conversation(
        server, ["run waters", *answers], llm, release=None)
    assert "create_job" not in server.names()
    assert "could not" in console.transcript.lower()


@pytest.mark.anyio
async def test_approval_n_leaves_the_job_planned(tmp_path):
    server = SessionServer(tmp_path)
    llm = StubLlm([submit(n_molecules=1000, water_model="tip4p"), say("ok")])
    session, console = await finish_conversation(
        server, [*FULL_SCRIPT, ("Approve?", "n")], llm, release=None)
    assert server.approval is None and "run_mdrun" not in server.names()
    assert "not approved" in console.transcript.lower() and JOB_ID in console.transcript


@pytest.mark.anyio
async def test_edit_changes_a_field_replans_and_asks_again(tmp_path):
    server = SessionServer(tmp_path)
    llm = StubLlm([submit(n_molecules=1000, water_model="tip4p"), say("ok")])
    inputs = [*FULL_SCRIPT, ("Approve?", "edit"), ("Change", "prod_ns=2"), ("2 ns", "y")]
    session, console = await finish_conversation(server, inputs, llm)
    assert server.sets == 2
    assert server.request["protocol"]["prod_ns"] == 2.0
    assert server.approval["plan_sha256"] == f"{2:064d}"
    assert "Run finished" in console.transcript


@pytest.mark.anyio
async def test_a_bad_edit_is_reported_and_asked_again(tmp_path):
    server = SessionServer(tmp_path)
    llm = StubLlm([submit(n_molecules=1000, water_model="tip4p"), say("ok")])
    inputs = [*FULL_SCRIPT, ("Approve?", "edit"), ("Change", "bogus=1"),
              ("Approve?", "n")]
    session, console = await finish_conversation(server, inputs, llm, release=None)
    assert "bogus" in console.transcript and server.sets == 1


@pytest.mark.anyio
async def test_unacknowledged_warning_prompt_flow(tmp_path):
    warning = {"id": "w1", "text": "a grompp warning", "source": "topol.top:3",
               "acknowledged": False}
    server = SessionServer(tmp_path, warnings=[warning])
    llm = StubLlm([submit(n_molecules=1000, water_model="tip4p"), say("ok")])
    inputs = [*FULL_SCRIPT, ("Approve?", "y"), ("w1", "y")]
    session, console = await finish_conversation(server, inputs, llm)
    approvals = [a for a in server.args_of("record_approval") if a["kind"] == "warnings"]
    assert approvals == [{"job_id": JOB_ID, "approval_token": TOKEN, "kind": "warnings",
                          "warning_ids": ["w1"]}]
    assert server.status["prod"] == "PASSED" and "Warnings acknowledged: 1" in console.transcript
    assert TOKEN not in console.transcript


@pytest.mark.anyio
async def test_session_decisions_interrupted_prompt():
    console = ScriptedConsole(["restart"])
    decisions = SessionDecisions(SessionServer(), console, TOKEN)
    assert await decisions.on_interrupted(JOB_ID, "prod", "process_died", True) == "restart"
    console = ScriptedConsole(["resume", "stop"])
    decisions = SessionDecisions(SessionServer(), console, TOKEN)
    assert await decisions.on_interrupted(JOB_ID, "prod", "cancelled", False) == "stop"
    assert "[restart/stop]" in console.transcript


def test_final_message_reports_the_gate_failure_even_if_the_model_claims_success():
    job = {"execution_mode": "simulated", "retries": [], "acknowledged_warnings": [],
           "stage_status": {"em": "PASSED", "nvt": "FAILED", "npt": "PENDING", "prod": "PENDING"},
           "failures": [{"stage": "nvt", "cause": "gate failed: mean_temperature"}]}
    outcome = Outcome("escalate", "nvt", "nvt failed its gate: mean_temperature")
    text = final_message(outcome, job)
    assert "mean_temperature" in text and "nvt FAILED" in text
    assert "all good" not in text and "escalate" in text.lower() or "stopped" in text.lower()


def test_final_message_lists_mode_stages_warnings_and_retries():
    job = {"execution_mode": "simulated", "acknowledged_warnings": ["w1"], "failures": [],
           "retries": [{"key": "emtol", "value": 100.0, "stage": "em", "failing_stage": "nvt"}],
           "stage_status": {s: "PASSED" for s in PROCESS}}
    outcome = Outcome("done", "prod", "ok",
                      disclosures=["Retried em with emtol=100 after the settle failure in nvt "
                                   "(automatic, 1 of 2)"])
    text = final_message(outcome, job)
    assert "simulated" in text and all(f"{s} PASSED" in text for s in PROCESS)
    assert "Warnings acknowledged: 1" in text and "emtol=100" in text
    assert "analysis" in text.lower() and "skipped" in text.lower()


@pytest.mark.anyio
async def test_commands(tmp_path):
    ledger = Ledger(tmp_path / "provenance.jsonl")
    ledger.append({"event": "end", "kind": "gmx", "stage": "em", "tool": "run_grompp",
                   "argv": ["/opt/gmx", "grompp", "-f", "em.mdp"], "exit_code": 0})
    ledger.append({"event": "end", "kind": "internal", "stage": "em", "outputs": []})
    server = SessionServer(tmp_path)
    llm = StubLlm([submit(n_molecules=1000, water_model="tip4p"), say("ok")])
    inputs = [*FULL_SCRIPT, ("Approve?", "n"), "/status", "/plan", "/files", "/jobs",
              "/provenance", "/bogus", "/quit"]
    session, console = await finish_conversation(server, inputs, llm, release=None)
    text = console.transcript
    assert "em PENDING" in text and "S3 nvt" in text and "02_em/em.tpr" in text
    assert "1000 tip4p" in text and "grompp -f em.mdp" in text and "exit 0" in text
    assert "Unknown command" in text


@pytest.mark.anyio
async def test_cancel_command_during_a_run_is_handled_at_once():
    server = SessionServer(statuses={"em": "PASSED", "nvt": "PASSED", "npt": "PASSED",
                                     "prod": "RUNNING"}, hold=True)
    console = ScriptedConsole([("[S5 prod]", "/cancel")], release_on="never printed")
    session = Session(server, None, load_config(env={}), console, TOKEN, poll_interval_s=10.0)
    started = time.monotonic()
    outcome = await asyncio.wait_for(session.execute(JOB_ID), timeout=8)
    assert outcome.status == "cancelled" and time.monotonic() - started < 3
    assert len(server.args_of("cancel_run")) == 1


@pytest.mark.anyio
async def test_quit_during_a_run_detaches_without_cancelling():
    server = SessionServer(statuses={"em": "PASSED", "nvt": "PASSED", "npt": "PASSED",
                                     "prod": "RUNNING"}, hold=True)
    console = ScriptedConsole([("[S5 prod]", "/quit")], release_on="never printed")
    session = Session(server, None, load_config(env={}), console, TOKEN, poll_interval_s=10.0)
    outcome = await asyncio.wait_for(session.execute(JOB_ID), timeout=8)
    assert outcome.status == "detached" and server.args_of("cancel_run") == []
    assert f"/resume {JOB_ID}" in console.transcript


@pytest.mark.anyio
async def test_end_of_input_during_a_run_detaches_too():
    server = SessionServer(statuses={"em": "PASSED", "nvt": "PASSED", "npt": "PASSED",
                                     "prod": "RUNNING"}, hold=True)
    console = ScriptedConsole([])
    session = Session(server, None, load_config(env={}), console, TOKEN, poll_interval_s=10.0)
    outcome = await asyncio.wait_for(session.execute(JOB_ID), timeout=8)
    assert outcome.status == "detached" and server.args_of("cancel_run") == []


@pytest.mark.anyio
async def test_progress_line_format():
    server = SessionServer(statuses={"em": "PASSED", "nvt": "PASSED", "npt": "PASSED",
                                     "prod": "RUNNING"}, running_polls=1)

    async def status(args):
        if server.running_polls > 0:
            server.running_polls -= 1
            return ok({"state": "running", "step": 100, "time_ps": 620.0, "ns_per_day": 150.0,
                       "eta_s": 22.0})
        return ok({"state": "finished", "exit_code": 0})

    server._get_run_status = status
    server.plan = None
    console = ScriptedConsole([], release_on="never printed")
    session = make(server, console)
    request = {"system": {"n_molecules": 1000, "water_model": "tip4p"}, "thermo": {
        "temperature_K": 300.0, "pressure_bar": 1.0}, "protocol": {"prod_ns": 1.0}}
    from simagent.core.mdp import build_default_plan
    request = {**request, "seed": 1, "force_field": "oplsaa", "protocol": {
        **request["protocol"], "dt_ps": 0.002}}
    server.plan = build_default_plan(request, {"box_nm": 3.1, "build_sha256": "b" * 64})
    server.request = request
    await session.execute(JOB_ID)
    assert "[S5 prod] 62% | 620.0 ps | 150 ns/day | ETA 00:00:22" in console.transcript


@pytest.mark.anyio
async def test_start_accepts_the_simulated_probe_text_the_real_server_returns():
    server = SessionServer()

    async def simulated(args):
        return ok({"version": "FAKE-0.0", "execution_mode": "simulated",
                   "probe": "skipped (simulated)"})

    server._gmx_check_environment = simulated
    console = ScriptedConsole([])
    assert await make(server, console).start() is True
    assert "skipped (simulated)" in console.transcript and "simulated mode" in console.transcript
