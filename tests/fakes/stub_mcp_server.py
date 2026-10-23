"""A tiny MCP server used to test the client helpers before the real server exists."""

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

server = MCPServer("stub")


@server.tool()
def echo(x: int) -> dict[str, Any]:
    return {"ok": True, "data": {"x": x}, "error": None}


@server.tool()
def env_has(name: str) -> dict[str, Any]:
    return {"ok": True, "data": {"present": name in os.environ}, "error": None}


if __name__ == "__main__":
    server.run()
