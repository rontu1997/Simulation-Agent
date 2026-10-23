"""MCP client helpers: server launch parameters, the model allow-list, and a session wrapper.

The model sees only the read-only tools in ``LLM_ALLOWED_TOOLS`` (design 14.1); the controller
and the CLI use the full tool set through ``McpSession``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Collection, Mapping, Sequence
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult, TextContent, Tool

from simagent.errors import ErrorCode, fail

LLM_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {"get_job", "list_jobs", "get_log_excerpt", "list_artifacts", "get_run_status"})


def to_openai_functions(tools: Sequence[Tool],
                        allow: Collection[str] = LLM_ALLOWED_TOOLS) -> list[dict[str, Any]]:
    """Convert MCP tools to OpenAI function schemas, keeping only allow-listed names."""
    return [
        {"type": "function",
         "function": {"name": tool.name, "description": tool.description or "",
                      "parameters": tool.input_schema}}
        for tool in tools if tool.name in allow
    ]


def server_params(parent_env: Mapping[str, str], token: str | None) -> StdioServerParameters:
    """Launch parameters for the MCP server (design 2.1 item 3).

    The environment is the caller's minus every LUNA_* variable, plus the approval token. The SDK
    merges ``env`` over its own minimal default environment, so nothing else leaks in.
    """
    env = {k: v for k, v in parent_env.items() if not k.upper().startswith("LUNA_")}
    if token:
        env["SIMAGENT_APPROVAL_TOKEN"] = token
    return StdioServerParameters(
        command=sys.executable, args=["-m", "simagent.mcp_server.app"], env=env)


def parse_envelope(result: CallToolResult) -> dict[str, Any]:
    """Unwrap the tool envelope; SDK-level text errors become an error envelope."""
    structured = result.structured_content
    if isinstance(structured, dict):
        if "ok" in structured:
            return structured
        inner = structured.get("result")
        if isinstance(inner, dict) and "ok" in inner:
            return inner
    for item in result.content:
        if isinstance(item, TextContent):
            try:
                parsed = json.loads(item.text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and "ok" in parsed:
                return parsed
    text = " ".join(c.text for c in result.content if isinstance(c, TextContent))
    return fail(ErrorCode.E_PARAM_OUT_OF_RANGE, text or "tool returned no envelope",
                hint="the SDK rejected the call before the tool ran")


class McpSession:
    """Async context manager that starts the server over stdio and calls its tools."""

    def __init__(self, params: StdioServerParameters) -> None:
        self._params = params
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None

    async def __aenter__(self) -> McpSession:
        read, write = await self._stack.enter_async_context(stdio_client(self._params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    async def list_tools(self) -> list[Tool]:
        assert self._session is not None
        return list((await self._session.list_tools()).tools)

    async def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._session is not None
        result = await self._session.call_tool(name, args or {})
        assert isinstance(result, CallToolResult)
        return parse_envelope(result)
