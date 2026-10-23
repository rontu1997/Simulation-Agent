"""The controller against the real MCP server and the fake gmx (simulated mode). The fake reads
its failure knobs from ``knobs.json`` next to the binary, because the server hands gmx an
allow-listed environment."""

import asyncio
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
from mcp_flow import TOKEN, fake_binary, make_params, prepare_approved_job

from simagent.agent.controller import Controller, NoDecisions
from simagent.config import load_config
from simagent.core.provenance import Ledger
from simagent.mcp_client import McpSession

pytestmark = [pytest.mark.anyio, pytest.mark.posix]


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def quick_sleep(seconds: float) -> None:
    await asyncio.sleep(0.2)  # the 10 s polling floor is checked in the unit tests


def knobs(binary: Path, **values: str) -> None:
    (binary.parent / "knobs.json").write_text(json.dumps(values), encoding="utf-8")


def controller(session, decisions=None) -> Controller:
    return Controller(session, load_config(env={}), decisions, sleep=quick_sleep)


def server(tmp_path: Path):
    binary = fake_binary(tmp_path)
    return binary, McpSession(make_params(tmp_path, binary=binary, token=TOKEN,
                                          SIMAGENT_ALLOW_FAKE="1"))


async def statuses(session, job_id):
    job = await session.call("get_job", {"job_id": job_id})
    assert job["ok"], job
    return job["data"]


class AcknowledgingDecisions(NoDecisions):
    def __init__(self, session):
        self.session = session
        self.asked = []

    async def acknowledge_warnings(self, job_id, warnings):
        self.asked.append([w["id"] for w in warnings])
        ids = [w["id"] for w in warnings]
        done = await self.session.call("record_approval", {
            "job_id": job_id, "approval_token": TOKEN, "kind": "warnings", "warning_ids": ids})
        assert done["ok"], done
        return ids


class ResumeDecisions(NoDecisions):
    async def on_interrupted(self, job_id, stage, cause, can_resume):
        return "resume" if can_resume else "restart"


async def test_controller_runs_all_four_stages_in_simulated_mode(tmp_path):
    binary, session = server(tmp_path)
    async with session:
        prepared = await prepare_approved_job(session)
        outcome = await controller(session).run(prepared["job_id"])
        job = await statuses(session, prepared["job_id"])
    assert outcome.status == "done", outcome.message
    assert [job["stage_status"][s] for s in ("em", "nvt", "npt", "prod")] == ["PASSED"] * 4
    assert job["execution_mode"] == "simulated" and outcome.disclosures == []


async def test_settle_failure_is_retried_automatically(tmp_path):
    binary, session = server(tmp_path)
    async with session:
        prepared = await prepare_approved_job(session)
        knobs(binary, FAKE_GMX_FAIL_ONCE="nvt:settle")
        outcome = await controller(session).run(prepared["job_id"])
        job = await statuses(session, prepared["job_id"])
    assert outcome.status == "done", outcome.message
    retries = job["retries"]
    assert [(r["key"], r["value"], r["stage"], r["failing_stage"]) for r in retries] == [
        ("emtol", 100.0, "em", "nvt")]
    assert len(outcome.disclosures) == 1 and "emtol=100" in outcome.disclosures[0]
    assert job["stage_status"]["prod"] == "PASSED"


async def test_warning_acknowledgement_flow(tmp_path):
    binary, session = server(tmp_path)
    async with session:
        prepared = await prepare_approved_job(session)
        knobs(binary, FAKE_GMX_WARNINGS="1")
        decisions = AcknowledgingDecisions(session)
        outcome = await controller(session, decisions).run(prepared["job_id"])
        job = await statuses(session, prepared["job_id"])
    assert outcome.status == "done", outcome.message
    assert len(decisions.asked) == 1 and len(decisions.asked[0]) == 1
    assert job["stage_status"]["prod"] == "PASSED"


async def test_unacknowledged_warning_stops_the_run(tmp_path):
    binary, session = server(tmp_path)
    async with session:
        prepared = await prepare_approved_job(session)
        knobs(binary, FAKE_GMX_WARNINGS="1")
        outcome = await controller(session).run(prepared["job_id"])
        job = await statuses(session, prepared["job_id"])
    assert outcome.status == "escalate" and "fake warning 1" in outcome.message
    assert job["stage_status"]["em"] == "PENDING"


def pids(job_dir: str, stage_dir: str) -> dict:
    return json.loads((Path(job_dir) / stage_dir / "run.json").read_text(encoding="utf-8"))


async def wait_for_gmx_pid(job_dir: str, stage_dir: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            info = pids(job_dir, stage_dir)
            if info.get("gmx_pid"):
                return info
        except (OSError, ValueError):
            pass
        await asyncio.sleep(0.2)
    raise AssertionError(f"{stage_dir} never started a gmx process")


async def test_kill_and_resume(tmp_path):
    """T-INT-09 slice with the fake binary: SIGKILL the wrapper and gmx mid-production."""
    binary, session = server(tmp_path)
    async with session:
        prepared = await prepare_approved_job(session)
        job_id, job_dir = prepared["job_id"], prepared["created"]["job_dir"]
        knobs(binary, FAKE_GMX_SLEEP_ONCE="prod:30")
        task = asyncio.ensure_future(controller(session, ResumeDecisions()).run(job_id))
        info = await wait_for_gmx_pid(job_dir, "05_prod")
        for pid in (info["gmx_pid"], info["pid"]):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        outcome = await asyncio.wait_for(task, timeout=120)
        job = await statuses(session, job_id)
    assert outcome.status == "done", outcome.message
    assert job["stage_status"]["prod"] == "PASSED"
    assert any("Resumed prod" in line for line in outcome.disclosures)
    assert (Path(job_dir) / "05_prod" / "run_history" / "1").is_dir()


async def test_reattach_after_controller_restart(tmp_path):
    binary, session = server(tmp_path)
    async with session:
        prepared = await prepare_approved_job(session)
        job_id, job_dir = prepared["job_id"], prepared["created"]["job_dir"]
        knobs(binary, FAKE_GMX_SLEEP_ONCE="prod:6")
        first = asyncio.ensure_future(controller(session).run(job_id))
        await wait_for_gmx_pid(job_dir, "05_prod")
        first.cancel()  # the agent dies; the wrapper and gmx keep running
    async with McpSession(make_params(tmp_path, binary=binary, token=TOKEN,
                                      SIMAGENT_ALLOW_FAKE="1")) as again:
        outcome = await controller(again).run(job_id)
        job = await statuses(again, job_id)
    assert outcome.status == "done", outcome.message
    assert job["stage_status"]["prod"] == "PASSED"
    starts = [r for r in Ledger(Path(job_dir) / "provenance.jsonl").records()
              if r.get("event") == "start" and r.get("stage") == "prod"
              and r.get("tool") == "run_mdrun"]
    assert len(starts) == 1


@pytest.mark.real_gmx
async def test_resume_continuity_real(tmp_path):
    """T-INT-09 against real GROMACS: after SIGKILL and resume the trajectory is continuous
    and ends at 1,000 ps. Written without a GROMACS host to run it on; first run is on the
    Linux host."""
    config = tmp_path / "config.yaml"
    config.write_text("gmx:\n  cpt_minutes: 1\n", encoding="utf-8")
    binary = Path(os.environ["GMX_BIN"])
    params = make_params(tmp_path, binary=binary, token=TOKEN, SIMAGENT_CONFIG=str(config))
    async with McpSession(params) as session:
        prepared = await prepare_approved_job(session)
        job_id, job_dir = prepared["job_id"], prepared["created"]["job_dir"]
        task = asyncio.ensure_future(controller(session, ResumeDecisions()).run(job_id))
        info = await wait_for_gmx_pid(job_dir, "05_prod", timeout=1800)
        await asyncio.sleep(90)  # let a checkpoint be written
        for pid in (info["gmx_pid"], info["pid"]):
            os.kill(pid, signal.SIGKILL)
        outcome = await asyncio.wait_for(task, timeout=7200)
    assert outcome.status == "done", outcome.message
    check = subprocess.run([str(binary), "check", "-f", str(Path(job_dir) / "05_prod/prod.xtc")],
                           capture_output=True, text=True, check=False)
    assert "Last frame" in check.stdout + check.stderr
    assert "1000.000" in check.stdout + check.stderr
