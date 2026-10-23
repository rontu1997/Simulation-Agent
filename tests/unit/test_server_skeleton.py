import os
import sys
import tempfile
from pathlib import Path

import pytest
from mcp.client.stdio import StdioServerParameters

from simagent.mcp_client import McpSession

TEST_SERVER = Path(__file__).resolve().parents[1] / "fakes" / "test_tools_server.py"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def params(script=None, **env):
    base = {k: v for k, v in os.environ.items() if not k.upper().startswith("LUNA_")}
    base.setdefault("GMX_RUNS_DIR", str(Path(tempfile.gettempdir()) / "simagent-test-runs"))
    base.update(env)
    args = [str(script)] if script else ["-m", "simagent.mcp_server.app"]
    return StdioServerParameters(command=sys.executable, args=args, env=base)


@pytest.mark.anyio
async def test_lists_registered_tools_with_json_schemas():
    async with McpSession(params(GMX_BIN="/nonexistent")) as session:
        tools = {t.name: t for t in await session.list_tools()}
    assert "gmx_check_environment" in tools
    assert tools["gmx_check_environment"].input_schema["type"] == "object"


@pytest.mark.anyio
async def test_check_environment_tool_reports_missing_binary():
    async with McpSession(params(GMX_BIN="/nonexistent/gmx")) as session:
        result = await session.call_result("gmx_check_environment", {})
        envelope = await session.call("gmx_check_environment", {})
    assert result.is_error is True
    assert envelope["ok"] is False and envelope["error"]["code"] == "E_GMX_NOT_FOUND"
    assert "GMX_BIN" in envelope["error"]["hint"]


@pytest.mark.anyio
async def test_error_returns_envelope_and_is_error():
    async with McpSession(params(TEST_SERVER)) as session:
        result = await session.call_result("test_stage_order", {})
        envelope = await session.call("test_stage_order", {})
    assert result.is_error is True
    assert envelope == {"ok": False, "data": {}, "error": {
        "code": "E_STAGE_ORDER", "message": "an earlier stage is not complete",
        "hint": "run em"}}


@pytest.mark.anyio
async def test_unknown_field_rejected_in_the_envelope():
    async with McpSession(params(TEST_SERVER, GMX_BIN="/nonexistent")) as session:
        envelope = await session.call("gmx_check_environment", {"bogus": 1})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "E_PARAM_OUT_OF_RANGE"
    assert "bogus" in envelope["error"]["message"]


@pytest.mark.anyio
async def test_unknown_tool_is_an_envelope_error():
    async with McpSession(params(TEST_SERVER)) as session:
        envelope = await session.call("no_such_tool", {})
    assert envelope["ok"] is False and envelope["error"]["code"] == "E_PARAM_OUT_OF_RANGE"


@pytest.mark.anyio
async def test_stdout_noise_does_not_break_protocol():
    async with McpSession(params(TEST_SERVER)) as session:
        envelope = await session.call("test_noise", {})
        again = await session.call("test_needs_job", {"job_id": "ok-1"})
    assert envelope["ok"] is True and envelope["data"] == {"printed": True}
    assert again["data"] == {"job_id": "ok-1"}


@pytest.mark.anyio
@pytest.mark.parametrize("bad", ["../../x", "a/b", ".hidden", "a..b"])
async def test_bad_job_id_returns_path_escape(bad):
    async with McpSession(params(TEST_SERVER)) as session:
        envelope = await session.call("test_needs_job", {"job_id": bad})
    assert envelope["ok"] is False and envelope["error"]["code"] == "E_PATH_ESCAPE"


@pytest.mark.anyio
async def test_malformed_job_id_is_out_of_range():
    async with McpSession(params(TEST_SERVER)) as session:
        envelope = await session.call("test_needs_job", {"job_id": ""})
    assert envelope["error"]["code"] == "E_PARAM_OUT_OF_RANGE"


@pytest.mark.anyio
async def test_unexpected_exception_becomes_internal_envelope():
    async with McpSession(params(TEST_SERVER)) as session:
        envelope = await session.call("test_explode", {})
    assert envelope["ok"] is False and envelope["error"]["code"] == "E_INTERNAL"
    assert "RuntimeError" in envelope["error"]["message"]
