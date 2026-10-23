import os
import sys
from pathlib import Path

import pytest
from mcp.client.stdio import StdioServerParameters
from mcp.types import Tool

from simagent.mcp_client import LLM_ALLOWED_TOOLS, McpSession, server_params, to_openai_functions

STUB = Path(__file__).resolve().parents[1] / "fakes" / "stub_mcp_server.py"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_tool(name: str) -> Tool:
    return Tool(name=name, description=f"{name} tool",
                input_schema={"type": "object", "properties": {"job_id": {"type": "string"}}})


def test_allow_list_is_exactly_five_and_excludes_record_approval():
    assert LLM_ALLOWED_TOOLS == {
        "get_job", "list_jobs", "get_log_excerpt", "list_artifacts", "get_run_status"}
    assert "record_approval" not in LLM_ALLOWED_TOOLS


def test_to_openai_functions_filters_and_converts():
    tools = [make_tool("record_approval"), make_tool("get_job"), make_tool("run_mdrun")]
    functions = to_openai_functions(tools)
    assert [f["function"]["name"] for f in functions] == ["get_job"]
    assert functions[0] == {
        "type": "function",
        "function": {"name": "get_job", "description": "get_job tool",
                     "parameters": {"type": "object",
                                    "properties": {"job_id": {"type": "string"}}}},
    }


def test_server_params_strips_luna_and_adds_token():
    parent = {"LUNA_API_KEY": "k-123456", "LUNA_BASE_URL": "http://x", "PATH": "/p", "LANG": "C"}
    params = server_params(parent, "tok-abcdef")
    assert params.command == sys.executable
    assert params.args == ["-m", "simagent.mcp_server.app"]
    assert params.env is not None
    assert "LUNA_API_KEY" not in params.env and "LUNA_BASE_URL" not in params.env
    assert params.env["PATH"] == "/p" and params.env["LANG"] == "C"
    assert params.env["SIMAGENT_APPROVAL_TOKEN"] == "tok-abcdef"
    assert "SIMAGENT_APPROVAL_TOKEN" not in server_params(parent, None).env


@pytest.mark.anyio
async def test_session_lists_and_calls_stub_server():
    params = StdioServerParameters(command=sys.executable, args=[str(STUB)])
    async with McpSession(params) as session:
        names = {t.name for t in await session.list_tools()}
        assert {"echo", "env_has"} <= names
        envelope = await session.call("echo", {"x": 1})
        assert envelope["ok"] is True and envelope["data"] == {"x": 1}


@pytest.mark.anyio
async def test_sdk_passes_explicit_env_over_a_minimal_default_environment():
    """[verify TASK-018] a parent-only variable is not inherited; an explicit env entry is."""
    os.environ["SIMAGENT_TEST_PARENT_ONLY"] = "1"
    try:
        params = StdioServerParameters(command=sys.executable, args=[str(STUB)],
                                       env={"SIMAGENT_TEST_EXPLICIT": "1"})
        async with McpSession(params) as session:
            explicit = await session.call("env_has", {"name": "SIMAGENT_TEST_EXPLICIT"})
            inherited = await session.call("env_has", {"name": "SIMAGENT_TEST_PARENT_ONLY"})
    finally:
        del os.environ["SIMAGENT_TEST_PARENT_ONLY"]
    assert explicit["data"]["present"] is True
    assert inherited["data"]["present"] is False
