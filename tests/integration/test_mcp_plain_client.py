import pytest
from mcp_flow import TOKEN, dummy_binary, make_params, prepared_job

from simagent.mcp_client import McpSession

M2_TOOLS = {
    "gmx_check_environment", "create_job", "set_plan", "record_approval", "build_water_box",
    "write_mdp", "run_grompp", "run_mdrun", "get_run_status", "cancel_run", "validate_stage",
    "get_log_excerpt", "list_artifacts", "get_job", "list_jobs",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_plain_mcp_client_lists_15_m2_tools_and_run_is_unavailable(tmp_path):
    """T-INT-08 slice: a client that holds no approval token can plan and read, never run."""
    binary = dummy_binary(tmp_path)
    job = prepared_job(tmp_path, binary)
    async with McpSession(make_params(tmp_path, binary=binary)) as session:
        names = {t.name for t in await session.list_tools()}
        assert names == M2_TOOLS and len(names) == 15
        run = await session.call("run_mdrun", {"job_id": job.job_id, "stage": "em",
                                               "resume": False})
        approval = await session.call("record_approval", {
            "job_id": job.job_id, "approval_token": TOKEN, "kind": "plan",
            "plan_sha256": job.read()["plan_sha256"]})
        readable = await session.call("get_job", {"job_id": job.job_id})
    assert run["ok"] is False and run["error"]["code"] == "E_APPROVAL_UNAVAILABLE"
    assert approval["ok"] is False and approval["error"]["code"] == "E_APPROVAL_UNAVAILABLE"
    assert readable["ok"] is True and readable["data"]["approval"]["present"] is False


@pytest.mark.anyio
async def test_run_with_a_token_still_needs_an_approval_of_the_stored_plan(tmp_path):
    binary = dummy_binary(tmp_path)
    job = prepared_job(tmp_path, binary)
    async with McpSession(make_params(tmp_path, binary=binary, token=TOKEN)) as session:
        run = await session.call("run_mdrun", {"job_id": job.job_id, "stage": "em",
                                               "resume": False})
    assert run["ok"] is False and run["error"]["code"] == "E_NOT_APPROVED"
    assert TOKEN not in str(run)
