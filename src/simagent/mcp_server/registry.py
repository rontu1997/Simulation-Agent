"""Tool registry and dispatcher for the MCP server.

Every tool is a function ``fn(ctx, args) -> dict`` registered with a pydantic argument model
(extra fields forbidden). The dispatcher validates arguments itself, so failures come back in the
tool envelope (design 4) instead of as plain-text SDK errors, and returns a ``CallToolResult``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anyio.to_thread
from mcp import types
from pydantic import BaseModel, ConfigDict, ValidationError

from simagent.config import Config, load_config
from simagent.core.paths import validate_job_id
from simagent.errors import ErrorCode, SimAgentError, error_envelope, fail, ok
from simagent.events import EventLog, Redactor, timed_tool


class EmptyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class ServerContext:
    cfg: Config
    events: EventLog
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, cfg: Config | None = None) -> ServerContext:
        cfg = cfg or load_config()
        redactor = Redactor([cfg.llm.api_key or "", cfg.approval_token or ""])
        events = EventLog(cfg.paths.runs_dir / "_server" / "events.jsonl", redactor)
        return cls(cfg=cfg, events=events)


ToolFn = Callable[[ServerContext, Any], dict[str, Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    model: type[BaseModel]
    fn: ToolFn


TOOLS: dict[str, ToolSpec] = {}


def tool(name: str, model: type[BaseModel] = EmptyArgs, description: str = "") -> Callable[
        [ToolFn], ToolFn]:
    def decorator(fn: ToolFn) -> ToolFn:
        TOOLS[name] = ToolSpec(name, description or (fn.__doc__ or "").strip(), model, fn)
        return fn

    return decorator


def _result(envelope: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(envelope))],
        structured_content=envelope,
        is_error=not envelope["ok"],
    )


def _validation_message(exc: ValidationError) -> str:
    first = exc.errors()[0]
    where = ".".join(str(p) for p in first["loc"]) or "arguments"
    return f"{where}: {first['msg']}"


async def dispatch(ctx: ServerContext, name: str,
                   arguments: dict[str, Any]) -> types.CallToolResult:
    spec = TOOLS.get(name)
    if spec is None:
        return _result(fail(ErrorCode.E_PARAM_OUT_OF_RANGE, f"unknown tool: {name}",
                            hint="call list_tools to see the available tools"))
    job_id = arguments.get("job_id") if isinstance(arguments, dict) else None
    try:
        if isinstance(job_id, str):
            validate_job_id(job_id)
        try:
            validated = spec.model.model_validate(arguments or {})
        except ValidationError as exc:
            raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE, _validation_message(exc),
                                hint=f"see the input schema of {name}") from exc
        with timed_tool(ctx.events, name, job_id if isinstance(job_id, str) else None):
            data = await anyio.to_thread.run_sync(spec.fn, ctx, validated)
        return _result(ok(data))
    except SimAgentError as exc:
        return _result(error_envelope(exc))
    except Exception as exc:  # unexpected server bug: report the type only, log the details
        ctx.events.emit("error", "server", "unexpected_exception", data={
            "tool": name, "type": type(exc).__name__, "detail": str(exc)})
        return _result(fail(ErrorCode.E_INTERNAL, f"unexpected {type(exc).__name__} in {name}",
                            hint="see the server event log; this is a bug"))
