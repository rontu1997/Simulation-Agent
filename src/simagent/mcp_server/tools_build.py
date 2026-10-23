"""Build tools. Tool 5: build_water_box (tool 6, write_mdp, is added with the execution tools)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from simagent.core.build import build_water_box
from simagent.core.gmx import GmxRunner, RunnerLike
from simagent.core.jobs import JobHandle
from simagent.mcp_server.registry import ServerContext, tool
from simagent.mcp_server.tools_job import get_store


class BuildArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: str
    n_molecules: int
    water_model: str
    target_density_g_cm3: float = 0.997


def get_runner(ctx: ServerContext, job: JobHandle) -> RunnerLike:
    """The gmx runner for a job; tests can inject ``runner_factory`` through ``ctx.extras``."""
    factory = ctx.extras.get("runner_factory")
    if factory is not None:
        runner: RunnerLike = factory(ctx.cfg, job.ledger)
        return runner
    return GmxRunner(ctx.cfg, job.ledger)


@tool("build_water_box", BuildArgs,
      "Build a cubic box with exactly the requested number of water molecules, its topology, and "
      "a dry grompp check. Call before set_plan; a call after a plan exists clears the approval.")
def build_water_box_tool(ctx: ServerContext, args: BuildArgs) -> dict[str, Any]:
    job = get_store(ctx).get(args.job_id)
    return build_water_box(job, get_runner(ctx, job), n_molecules=args.n_molecules,
                           water_model=args.water_model,
                           target_density_g_cm3=args.target_density_g_cm3)
