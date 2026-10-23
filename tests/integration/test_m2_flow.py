import json
import os

import pytest
from mcp_flow import (
    TOKEN,
    dummy_binary,
    fake_binary,
    make_params,
    prepared_job,
    run_em_flow,
)

from simagent.mcp_client import McpSession


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.posix
async def test_simulated_flow_with_fake_binary(tmp_path):
    """T-INT-04 slice: the whole EM flow against the fake binary, in simulated mode."""
    binary = fake_binary(tmp_path)
    params = make_params(tmp_path, binary=binary, token=TOKEN, SIMAGENT_ALLOW_FAKE="1")
    async with McpSession(params) as session:
        flow = await run_em_flow(session)
        job_id = flow["created"]["job_id"]
        artifacts = await session.call("list_artifacts", {"job_id": job_id, "stage": "em"})
    assert flow["created"]["environment"]["execution_mode"] == "simulated"
    assert str(tmp_path / "runs_simulated") in flow["created"]["job_dir"]
    assert flow["built"]["n_atoms"] == 4 * flow["built"]["n_molecules"]
    assert flow["final"]["exit_code"] == 0
    validated = flow["validated"]
    assert validated["ok"] and validated["data"]["passed"] is True
    statuses = {c["name"]: c["status"] for c in validated["data"]["checks"]}
    assert statuses["converged"] == "skipped_simulated"
    assert statuses["process_state"] == "pass" and statuses["receipts"] == "pass"
    files = {f["path"]: f for f in artifacts["data"]["files"]}
    assert files["02_em/em.log"]["has_receipt"] is True and files["02_em/em.log"]["kind"] == "gmx"
    assert not any(k.endswith(("run.exit", "run.json")) and v["has_receipt"]
                   for k, v in files.items())


@pytest.mark.anyio
@pytest.mark.real_gmx
async def test_real_flow_em(tmp_path):
    """T-INT-01 slice: build and EM against the real binary; the EM gate passes."""
    binary = os.environ["GMX_BIN"]
    params = make_params(tmp_path, binary=binary, token=TOKEN)
    async with McpSession(params) as session:
        flow = await run_em_flow(session)
    assert flow["created"]["environment"]["execution_mode"] == "real"
    assert flow["built"]["n_atoms"] == 4000
    checks = {c["name"]: c["status"] for c in flow["validated"]["data"]["checks"]}
    assert flow["validated"]["data"]["passed"] is True, checks
    assert checks["converged"] == "pass"


@pytest.mark.anyio
async def test_job_survives_server_restart(tmp_path):
    """T-INT-10: state lives on disk, so a new server process sees the same job."""
    binary = dummy_binary(tmp_path)
    job = prepared_job(tmp_path, binary)
    digest = job.read()["plan_sha256"]
    params = make_params(tmp_path, binary=binary, token=TOKEN)

    async with McpSession(params) as first:
        approved = await first.call("record_approval", {
            "job_id": job.job_id, "approval_token": TOKEN, "kind": "plan", "plan_sha256": digest})
        mdp = await first.call("write_mdp", {"job_id": job.job_id, "stage": "em"})
        before = await first.call("get_job", {"job_id": job.job_id})
    assert approved["ok"] and mdp["ok"] and before["ok"]

    async with McpSession(params) as second:
        after = await second.call("get_job", {"job_id": job.job_id})
        listing = await second.call("list_jobs", {})
        artifacts = await second.call("list_artifacts", {"job_id": job.job_id, "stage": "em"})
    assert after["ok"] and after["data"] == before["data"]
    assert after["data"]["plan_sha256"] == digest
    assert after["data"]["approval"] == {"present": True, "mode": "interactive",
                                         "matches_plan": True}
    assert after["data"]["stage_status"]["em"] == "PENDING"
    assert [j["job_id"] for j in listing["data"]["jobs"]] == [job.job_id]
    assert listing["data"]["jobs"][0]["status"] == "planned"
    mdp_entry = next(f for f in artifacts["data"]["files"] if f["path"] == "02_em/em.mdp")
    assert mdp_entry["has_receipt"] is True
    assert mdp_entry["sha256"] == mdp["data"]["rendered_sha256"]
    assert TOKEN not in json.dumps(after)
