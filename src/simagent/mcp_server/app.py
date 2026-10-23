"""MCP server entry point (`gmx-mcp`): stdio transport, tools from the registry.

The SDK's ``stdio_server`` keeps file descriptor 1 for protocol frames and diverts stray writes to
stderr, so a tool that prints cannot corrupt the stream.
"""

from __future__ import annotations

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from simagent.config import Config
from simagent.mcp_server import tools_env  # noqa: F401  (registers tool 1)
from simagent.mcp_server.registry import TOOLS, ServerContext, dispatch


def build_server(cfg: Config | None = None) -> Server:
    ctx = ServerContext.create(cfg)

    async def on_list_tools(_rctx, _params) -> types.ListToolsResult:  # type: ignore[no-untyped-def]
        return types.ListToolsResult(tools=[
            types.Tool(name=spec.name, description=spec.description,
                       input_schema=spec.model.model_json_schema())
            for spec in TOOLS.values()
        ])

    async def on_call_tool(_rctx, params) -> types.CallToolResult:  # type: ignore[no-untyped-def]
        return await dispatch(ctx, params.name, params.arguments or {})

    return Server("gmx-mcp", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def _serve() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    anyio.run(_serve)


if __name__ == "__main__":
    main()
