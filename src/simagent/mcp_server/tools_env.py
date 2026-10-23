"""Tool 1: gmx_check_environment."""

from __future__ import annotations

from typing import Any

from simagent.core.envcheck import check_environment
from simagent.mcp_server.registry import EmptyArgs, ServerContext, tool


@tool("gmx_check_environment", EmptyArgs,
      "Check that the configured GROMACS binary exists, is the expected build, can find its data "
      "files, and can run a tiny simulation in this server's environment.")
def gmx_check_environment(ctx: ServerContext, args: EmptyArgs) -> dict[str, Any]:
    return check_environment(ctx.cfg)
