"""The CLI against the real MCP server and the fake gmx (simulated mode). `main` runs its own
event loop, so these tests are plain functions; the server is started by `main` itself."""

import json
import re
import shutil
from pathlib import Path

import pytest
from mcp_flow import FAKE, TOKEN, make_params, prepare_approved_job

from scripted import ScriptedConsole, StubLlm, say, submit
from simagent import cli
from simagent.core.provenance import Ledger
from simagent.mcp_client import McpSession

pytestmark = pytest.mark.posix

REQUEST = {"system": {"kind": "water", "n_molecules": 1000, "water_model": "tip4p"},
           "force_field": "oplsaa", "thermo": {"temperature_K": 300.0, "pressure_bar": 1.0},
           "protocol": {"prod_ns": 1.0}, "analyses": ["rdf_OO"]}


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def quick(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(0.05)  # the 10 s floor itself is covered by the controller tests


@pytest.fixture
def env(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "gmx"
    binary.parent.mkdir()
    shutil.copy(FAKE, binary)
    binary.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GMX_BIN", str(binary))
    monkeypatch.setenv("GMX_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("SIMAGENT_ALLOW_FAKE", "1")
    monkeypatch.setenv("LUNA_API_KEY", "key-for-tests-0123456789")
    monkeypatch.setenv("LUNA_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setattr(cli, "_poll_sleep", quick)
    monkeypatch.delenv("SIMAGENT_APPROVAL_TOKEN", raising=False)
    monkeypatch.delenv("SIMAGENT_ALLOW_UNAPPROVED", raising=False)
    return tmp_path, binary


def job_json(tmp_path: Path) -> dict:
    (path,) = list((tmp_path / "runs_simulated").glob("water-*/job.json"))
    data: dict = json.loads(path.read_text(encoding="utf-8"))
    return data


def request_file(tmp_path: Path) -> str:
    path = tmp_path / "req.json"
    path.write_text(json.dumps(REQUEST), encoding="utf-8")
    return str(path)


def test_run_request_file_yes_runs_to_done_with_auto_approval(env, capsys):
    tmp_path, binary = env
    code = cli.main(["run", "--request-file", request_file(tmp_path), "--yes"])
    out = capsys.readouterr().out
    assert code == 0, out
    data = job_json(tmp_path)
    assert data["approval"]["mode"] == "auto"
    assert data["approval"]["plan_sha256"] == data["plan_sha256"]  # the hash that was shown
    assert [data["stage_status"][s] for s in ("em", "nvt", "npt", "prod")] == ["PASSED"] * 4
    assert "Run finished: all four stages passed" in out and "simulated" in out
    assert "Plan for" in out and "seed" in out
    assert "key-for-tests" not in out


@pytest.mark.anyio
async def test_the_server_refuses_a_stale_hash_and_a_wrong_token(env):
    tmp_path, binary = env
    params = make_params(tmp_path, binary=binary, token=TOKEN, SIMAGENT_ALLOW_FAKE="1")
    async with McpSession(params) as session:
        prepared = await prepare_approved_job(session)
        job_id = prepared["job_id"]
        stale = await session.call("record_approval", {
            "job_id": job_id, "approval_token": TOKEN, "kind": "plan",
            "plan_sha256": "0" * 64, "mode": "auto"})
        no_token = await session.call("record_approval", {
            "job_id": job_id, "approval_token": "wrong", "kind": "plan",
            "plan_sha256": "0" * 64, "mode": "auto"})
        fresh = await session.call("get_job", {"job_id": job_id})
    assert stale["error"]["code"] == "E_PLAN_MISMATCH"
    assert no_token["error"]["code"] == "E_BAD_TOKEN"
    assert fresh["data"]["plan_sha256"] != "0" * 64


def test_run_without_an_approval_is_refused_by_the_server(env):
    tmp_path, binary = env
    import asyncio

    async def attempt():
        params = make_params(tmp_path, binary=binary, token=TOKEN, SIMAGENT_ALLOW_FAKE="1")
        async with McpSession(params) as session:
            protocol = {"emtol": 1000.0, "nvt_ps": 100, "npt_ps": 100, "prod_ns": 1.0,
                        "dt_ps": 0.002, "tau_t": 0.1, "tau_p": 2.0}
            made = await session.call("create_job", {"request": {
                **REQUEST, "seed": None, "protocol": protocol}})
            return await session.call("run_mdrun", {
                "job_id": made["data"]["job_id"], "stage": "em", "resume": False})

    result = asyncio.run(attempt())
    assert result["error"]["code"] == "E_NOT_APPROVED"


def scripted_chat(monkeypatch, inputs, replies, release="Run finished"):
    console = ScriptedConsole(inputs, release_on=release)
    monkeypatch.setattr(cli, "make_console", lambda: console)
    monkeypatch.setattr(cli, "make_llm", lambda cfg: StubLlm(replies))
    return console


FLOW = [
    "run a 1 ns md of 1000 tip4p waters",
    ("Details I need", "yes yes yes"),
    ("production run", "yes yes"),
]


def test_interactive_session_with_stub_llm(env, monkeypatch):
    tmp_path, binary = env
    console = scripted_chat(monkeypatch, [*FLOW, ("Approve?", "y")], [
        submit(n_molecules=1000, water_model="tip4p"), say("Plan ready.")])
    assert cli.main([]) == 0
    text = console.transcript
    assert "GROMACS" in text and "simulated" in text
    assert "Run finished: all four stages passed" in text
    data = job_json(tmp_path)
    assert data["approval"]["mode"] == "interactive"
    assert data["request"]["provenance_of_fields"]["n_molecules"] == "user"
    assert data["request"]["provenance_of_fields"]["force_field"] == "user"
    assert data["request"]["provenance_of_fields"]["emtol"] == "default"


def test_quit_during_a_run_leaves_it_running(env, monkeypatch):
    tmp_path, binary = env
    (binary.parent / "knobs.json").write_text(
        json.dumps({"FAKE_GMX_SLEEP_ONCE": "prod:12"}), encoding="utf-8")
    first = scripted_chat(monkeypatch, [*FLOW, ("Approve?", "y"), ("[S5 prod]", "/quit")], [
        submit(n_molecules=1000, water_model="tip4p"), say("ok")], release=None)
    assert cli.main([]) == 0
    match = re.search(r"/resume (water-\S+)", first.transcript)
    assert match, first.transcript
    job_id = match.group(1)
    assert "Run finished" not in first.transcript

    second = scripted_chat(monkeypatch, [("GROMACS", f"/resume {job_id}")], [say("hi")])
    assert cli.main([]) == 0
    assert "Run finished: all four stages passed" in second.transcript
    starts = [r for r in Ledger(tmp_path / "runs_simulated" / job_id / "provenance.jsonl").records()
              if r.get("event") == "start" and r.get("stage") == "prod"
              and r.get("tool") == "run_mdrun"]
    assert len(starts) == 1
