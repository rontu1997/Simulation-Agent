"""Command-line entry point: ``simagent`` (chat) and ``simagent run --request-file`` (design 10).

Exit codes: 0 the run finished or was left running in the background, 1 the run did not
complete, 2 a usage or request-file error, 3 the environment check failed or the model is not
configured. The approval token is generated here for each server launch and only ever goes to the
server's environment and to this process's own ``record_approval`` calls.
"""

from __future__ import annotations

import argparse
import asyncio
import builtins
import json
import secrets
import sys
import threading
from collections.abc import Awaitable, Callable, Sequence
from importlib import resources
from pathlib import Path
from typing import Any

from simagent import __version__
from simagent.agent.controller import Outcome
from simagent.agent.conversation import ChatClient
from simagent.agent.request import draft_from_request_file, finalize, non_interactive_errors
from simagent.agent.session import Console, Session
from simagent.config import Config, ConfigError, default_config_path, load_config
from simagent.llm import LlmClient
from simagent.mcp_client import McpSession, server_params

# Tests replace this to poll quickly; the controller enforces and tests the 10 s floor itself.
_poll_sleep: Callable[[float], Awaitable[None]] | None = None


def load_system_prompt() -> str:
    return resources.files("simagent.agent").joinpath("prompts", "system.md").read_text(
        encoding="utf-8")


class TerminalConsole:
    """Standard input and output. One daemon thread reads lines into a queue, so a cancelled
    ``ask`` never loses the next line and a blocked read never keeps the process alive."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] | None = None
        self._ended = False
        self._started = False

    async def say(self, text: str) -> None:
        print(text, flush=True)

    def _start(self) -> None:
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        queue = self._queue

        def reader() -> None:
            while True:
                try:
                    line: str | None = builtins.input()
                except (EOFError, KeyboardInterrupt):
                    line = None
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, line)
                except RuntimeError:  # the loop is gone
                    return
                if line is None:
                    return

        threading.Thread(target=reader, daemon=True).start()
        self._started = True

    async def ask(self, prompt: str) -> str | None:
        if self._ended:
            return None
        if not self._started:
            self._start()
        assert self._queue is not None
        if prompt:
            print(prompt, end="", flush=True)
        line = await self._queue.get()
        if line is None:
            self._ended = True
        return line


def make_console() -> Console:
    return TerminalConsole()


def make_llm(cfg: Config) -> ChatClient:
    return LlmClient(cfg.llm)


def _error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="simagent", description=__doc__.split("\n")[0]
                                     if __doc__ else None)
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="run a finished request file without a conversation")
    run.add_argument("--request-file", required=True, help="a JSON request (design 3.1 shape)")
    run.add_argument("--yes", action="store_true",
                     help="approve the plan automatically (recorded as mode auto)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.version:
        print(f"simagent {__version__}")
        return 0
    try:
        cfg = load_config(yaml_path=default_config_path())
    except (ConfigError, OSError, ValueError) as exc:
        _error(f"configuration: {exc}")
        return 2
    try:
        if args.command == "run":
            return _run_file(args, cfg)
        return _chat(cfg)
    except KeyboardInterrupt:
        print("\nInterrupted. A stage that was running keeps running; start simagent again "
              "and use /resume <job_id> to follow it.", file=sys.stderr)
        return 130
    except Exception as exc:  # report, never a traceback; the run itself is untouched
        _error(f"{type(exc).__name__}: {exc}")
        return 1


def _read_request(path: str) -> dict[str, Any] | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _error(f"cannot read the request file {path}: {exc}")
        return None
    if not isinstance(data, dict):
        _error(f"{path} must contain a JSON object")
        return None
    return data


def _run_file(args: argparse.Namespace, cfg: Config) -> int:
    data = _read_request(args.request_file)
    if data is None:
        return 2
    try:
        draft = draft_from_request_file(data)
    except ValueError as exc:
        _error(str(exc))
        return 2
    problems = non_interactive_errors(draft)
    if problems:
        for problem in problems:
            _error(problem)
        return 2
    if not args.yes and not sys.stdin.isatty():
        _error("input is not a terminal, so the plan cannot be approved interactively: "
               "pass --yes")
        return 2
    request = finalize(draft)
    return asyncio.run(_run_async(cfg, request, args.yes))


async def _run_async(cfg: Config, request: dict[str, Any], yes: bool) -> int:
    token = secrets.token_urlsafe(32)
    async with McpSession(server_params(_environment(), token)) as tools:
        session = Session(tools, None, cfg, make_console(), token,
                          interactive=sys.stdin.isatty(), sleep=_poll_sleep)
        if not await session.start():
            return 3
        outcome: Outcome = await session.run_request(request, auto_approve=yes)
    return 0 if outcome.status in ("done", "detached") else 1


def _environment() -> dict[str, str]:
    import os
    return dict(os.environ)


def _chat(cfg: Config) -> int:
    if not cfg.llm.api_key:
        _error("LUNA_API_KEY is not set. Set it in your environment (it is never read from a "
               "file), for example: export LUNA_API_KEY=...")
        return 3
    if not cfg.llm.base_url:
        _error("LUNA_BASE_URL is not set. Set it to the OpenAI-compatible endpoint, for "
               "example: export LUNA_BASE_URL=https://host/v1")
        return 3
    return asyncio.run(_chat_async(cfg))


async def _chat_async(cfg: Config) -> int:
    token = secrets.token_urlsafe(32)
    async with McpSession(server_params(_environment(), token)) as tools:
        read_tools = await tools.list_tools()
        session = Session(tools, make_llm(cfg), cfg, make_console(), token,
                          read_tools=read_tools, system_prompt=load_system_prompt(),
                          sleep=_poll_sleep)
        if not await session.start():
            return 3
        await session.chat()
    return 0


if __name__ == "__main__":
    sys.exit(main())
