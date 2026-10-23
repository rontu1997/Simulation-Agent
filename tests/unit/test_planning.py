import copy
from typing import Any

import pytest

from simagent.agent.planning import (
    PlanError,
    prepare_job,
    record_plan_approval,
    render_plan,
    replan,
)
from simagent.agent.request import RequestDraft, finalize
from simagent.core.mdp import build_default_plan, derive_seed

JOB_ID = "water-1000-tip4p-300K-20261023-1"
BUILD = {"box_nm": 3.107, "build_sha256": "b" * 64, "target_density_g_cm3": 0.997,
         "n_molecules": 1000, "water_model": "tip4p", "n_atoms": 4000}
TOKEN = "tok-secret-0123456789"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def ok(data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": True, "data": data or {}, "error": None}


def err(code: str, message: str = "bad", hint: str = "fix it") -> dict[str, Any]:
    return {"ok": False, "data": {}, "error": {"code": code, "message": message, "hint": hint}}


def request_dict(**protocol) -> dict[str, Any]:
    values = {"n_molecules": 1000, "water_model": "tip4p", "force_field": "oplsaa",
              "temperature_K": 300.0, "pressure_bar": 1.0, "prod_ns": 1.0, "analyses": ["rdf_OO"]}
    request = finalize(RequestDraft(values, {k: "user" for k in values}))
    request["protocol"].update(protocol)
    return request


class FakeTools:
    def __init__(self, results: dict[str, Any]):
        self.results = {k: list(v) if isinstance(v, list) else [v] for k, v in results.items()}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        queue = self.results[name]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        return item(args) if callable(item) else item

    def names(self):
        return [n for n, _ in self.calls]

    def args(self, name):
        return [a for n, a in self.calls if n == name]


def stored_job(request: dict[str, Any], digest: str = "h" * 64) -> dict[str, Any]:
    seeded = {**request, "seed": derive_seed(JOB_ID)}
    return {"job_id": JOB_ID, "request": seeded, "plan": build_default_plan(seeded, BUILD),
            "plan_sha256": digest, "execution_mode": "simulated", "stage_status": {}}


def prep_tools(**over) -> FakeTools:
    results = {
        "create_job": ok({"job_id": JOB_ID, "job_dir": "/x", "warnings": ["a warning"],
                          "environment": {}}),
        "build_water_box": ok(BUILD),
        "set_plan": ok({"plan_sha256": "h" * 64, "invalidated": []}),
        "get_job": ok(stored_job(request_dict())),
    }
    results.update(over)
    return FakeTools(results)


@pytest.mark.anyio
async def test_prepare_job_calls_the_tools_in_order_and_derives_the_seed():
    tools = prep_tools()
    prepared = await prepare_job(tools, request_dict())
    assert tools.names() == ["create_job", "build_water_box", "set_plan", "get_job"]
    assert tools.args("create_job")[0]["request"]["seed"] is None
    sent = tools.args("set_plan")[0]
    assert sent["request"]["seed"] == derive_seed(JOB_ID) == 1904784231
    assert sent["request"]["provenance_of_fields"]["seed"] == "default"
    assert [s["name"] for s in sent["plan"]["stages"]] == [
        "build", "em", "nvt", "npt", "prod", "analysis", "report"]
    assert tools.args("build_water_box")[0] == {
        "job_id": JOB_ID, "n_molecules": 1000, "water_model": "tip4p"}
    assert prepared.job_id == JOB_ID and prepared.plan_sha256 == "h" * 64
    assert prepared.warnings == ["a warning"] and prepared.execution_mode == "simulated"


@pytest.mark.anyio
@pytest.mark.parametrize("tool,code", [
    ("create_job", "E_PARAM_OUT_OF_RANGE"), ("build_water_box", "E_BUILD_COUNT"),
    ("set_plan", "E_GATE_FAILED")])
async def test_prepare_job_raises_plan_error_with_code_and_hint(tool, code):
    tools = prep_tools(**{tool: err(code, "it broke", "try this")})
    with pytest.raises(PlanError) as exc:
        await prepare_job(tools, request_dict())
    message = str(exc.value)
    assert code in message and "it broke" in message and "try this" in message


@pytest.mark.anyio
async def test_an_existing_job_id_skips_create_job():
    tools = prep_tools()
    await prepare_job(tools, request_dict(), job_id=JOB_ID)
    assert "create_job" not in tools.names()


def test_plan_is_rendered_from_the_servers_copy():
    """The model said 500 molecules in chat; the server holds 1,000, and that is what shows."""
    chat_claim = "Here is your plan: 500 tip4p waters at 300 K"
    text = render_plan(stored_job(request_dict()))
    assert "1000" in text and "500" not in text
    assert chat_claim not in text


def test_render_marks_defaults_and_shows_origins_seed_and_retries():
    text = render_plan(stored_job(request_dict()))
    lines = {line.split()[0] + " " + line.split()[1]: line for line in text.splitlines()
             if line.strip().startswith("S")}
    assert set(lines) >= {"S1 build", "S2 em", "S3 nvt", "S4 npt", "S5 prod", "S6 analysis",
                          "S7 report"}
    assert "*" in lines["S2 em"] and "seed 1904784231*" in lines["S3 nvt"]
    assert "V-rescale*" in lines["S3 nvt"] and "C-rescale*" in lines["S4 npt"]
    assert "*" not in lines["S5 prod"].replace("prod", "")
    for name in ("n_molecules", "water_model", "force_field", "temperature_K", "pressure_bar",
                 "prod_ns"):
        assert any(name in line and "user" in line for line in text.splitlines()), name
    assert "emtol between 100 and 1000" in text
    assert "dt 0.001 only" in text and "tau_p 5.0 only" in text
    assert "EM steps 100000 or 200000" in text
    assert "rdf_OO" in lines["S6 analysis"] and "Estimated disk 120 MB" in text


@pytest.mark.anyio
async def test_approval_hash_comes_from_the_read_that_was_shown():
    first, second = stored_job(request_dict(), "1" * 64), stored_job(request_dict(), "2" * 64)
    tools = FakeTools({"get_job": [ok(first), ok(second)], "record_approval": ok(
        {"approved_at": "now"})})
    digest = await record_plan_approval(tools, TOKEN, JOB_ID, "interactive")
    assert digest == "1" * 64
    assert tools.args("record_approval")[0] == {
        "job_id": JOB_ID, "approval_token": TOKEN, "kind": "plan", "plan_sha256": "1" * 64,
        "mode": "interactive"}
    shown = FakeTools({"record_approval": ok({"approved_at": "now"})})
    assert await record_plan_approval(shown, TOKEN, JOB_ID, "auto", shown=second) == "2" * 64
    assert shown.names() == ["record_approval"]


@pytest.mark.anyio
async def test_a_failed_approval_raises_without_the_token():
    tools = FakeTools({"get_job": ok(stored_job(request_dict())),
                       "record_approval": err("E_PLAN_MISMATCH", "hash differs", "read again")})
    with pytest.raises(PlanError) as exc:
        await record_plan_approval(tools, TOKEN, JOB_ID, "interactive")
    assert "E_PLAN_MISMATCH" in str(exc.value) and TOKEN not in str(exc.value)


@pytest.mark.anyio
async def test_replan_rebuilds_only_when_the_system_changed():
    prepared = await prepare_job(prep_tools(), request_dict())
    tools = prep_tools()
    await replan(tools, prepared, {"prod_ns": 2.0})
    assert "build_water_box" not in tools.names() and "create_job" not in tools.names()
    sent = tools.args("set_plan")[0]
    assert sent["request"]["protocol"]["prod_ns"] == 2.0
    assert sent["request"]["provenance_of_fields"]["prod_ns"] == "user"
    assert sent["plan"]["stages"][4]["params"]["nsteps"] == 1_000_000

    tools = prep_tools()
    await replan(tools, prepared, {"n_molecules": 800})
    assert tools.args("build_water_box")[0]["n_molecules"] == 800
    assert tools.args("set_plan")[0]["request"]["system"]["n_molecules"] == 800


@pytest.mark.anyio
async def test_replan_rejects_unknown_fields():
    prepared = await prepare_job(prep_tools(), request_dict())
    with pytest.raises(PlanError, match="bogus"):
        await replan(prep_tools(), prepared, {"bogus": 1})
