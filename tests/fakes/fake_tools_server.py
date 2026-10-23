"""The real MCP server plus test-only tools, registered through the public registry."""

from pydantic import BaseModel, ConfigDict

from simagent.errors import ErrorCode, SimAgentError
from simagent.mcp_server import tools_env  # noqa: F401  (registers the real tools)
from simagent.mcp_server.app import main
from simagent.mcp_server.registry import EmptyArgs, tool


class JobArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str


@tool("test_noise", EmptyArgs, "prints to stdout")
def noise_tool(ctx, args):
    print("noise that must never reach the protocol stream")
    return {"printed": True}


@tool("test_stage_order", EmptyArgs, "always fails with E_STAGE_ORDER")
def stage_order_tool(ctx, args):
    raise SimAgentError(ErrorCode.E_STAGE_ORDER, "an earlier stage is not complete", hint="run em")


@tool("test_explode", EmptyArgs, "raises an unexpected exception")
def explode_tool(ctx, args):
    raise RuntimeError("kaboom with details")


@tool("test_needs_job", JobArgs, "takes a job_id")
def needs_job_tool(ctx, args):
    return {"job_id": args.job_id}


if __name__ == "__main__":
    main()
