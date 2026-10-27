"""Command-line entry point: ``simagent`` (chat) and ``simagent run --request-file`` (design 10).

Exit codes: 0 the run finished or was left running in the background, 1 the run did not
complete, 2 a usage or request-file error, 3 the environment check failed or the model is not
configured. The approval token is generated here for each server launch and only ever goes to the
server's environment and to this process's own ``record_approval`` calls.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import sys
import threading
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from simagent import __version__
from simagent.agent.controller import Outcome
from simagent.agent.conversation import ChatClient
from simagent.agent.request import draft_from_request_file, finalize, non_interactive_errors
from simagent.agent.session import Console, Session
from simagent.config import Config, ConfigError, default_config_path, load_config
from simagent.core.gmx import GmxRunner
from simagent.core.provenance import Ledger
from simagent.envfile import EnvFileError, load_env_file
from simagent.gromacs_paths import apply_gromacs_paths
from simagent.llm import LlmClient
from simagent.mcp_client import McpSession, server_params
from simagent.structure import StructureRun, StructureRunError, run_structure
from simagent.structure_analysis import (
    StructureAnalysisError,
    analysis_labels,
    analyze_structure,
    default_analyses,
    parse_analysis_text,
)

# Tests replace this to poll quickly; the controller enforces and tests the 10 s floor itself.
_poll_sleep: Callable[[float], Awaitable[None]] | None = None


def load_system_prompt() -> str:
    return resources.files("simagent.agent").joinpath("prompts", "system.md").read_text(
        encoding="utf-8")


def fd_line_reader(fd: int) -> Callable[[], str | None]:
    """A blocking line reader over a file descriptor, ``None`` at end of input.

    It uses ``os.read``, not ``input()``: a thread blocked in ``input()`` holds the C stdio lock,
    and on a Linux terminal that makes the interpreter hang at exit until the user presses Enter.
    """
    buffer = b""
    ended = False

    def read_line() -> str | None:
        nonlocal buffer, ended
        while True:
            if b"\n" in buffer:
                raw, _, buffer = buffer.partition(b"\n")
                return raw.decode("utf-8", errors="replace").rstrip("\r")
            if ended:
                if buffer:
                    raw, buffer = buffer, b""
                    return raw.decode("utf-8", errors="replace").rstrip("\r")
                return None
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            if not chunk:
                ended = True
            buffer += chunk

    return read_line


class TerminalConsole:
    """Standard input and output. One daemon thread reads lines into a queue, so a cancelled
    ``ask`` never loses the next line, and a thread blocked in a read never keeps the process
    alive."""

    def __init__(self, reader: Callable[[], str | None] | None = None) -> None:
        self._reader = reader
        self._queue: asyncio.Queue[str | None] | None = None
        self._ended = False

    async def say(self, text: str) -> None:
        print(text, flush=True)

    def _start(self) -> None:
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        queue = self._queue
        read_line = self._reader or fd_line_reader(sys.stdin.fileno())

        def run() -> None:
            while True:
                line = read_line()
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, line)
                except RuntimeError:  # the loop is gone
                    return
                if line is None:
                    return

        threading.Thread(target=run, daemon=True).start()

    async def ask(self, prompt: str) -> str | None:
        if self._ended:
            return None
        if self._queue is None:
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
    parser.add_argument("--env-file", metavar="PATH",
                        help="read LUNA_*, GMX_*, and SIMAGENT_* settings from this file "
                        "(default: ./.env for chat and run; real environment variables win)")
    commands = parser.add_subparsers(dest="command")
    run = commands.add_parser("run", help="run a finished request file without a conversation")
    run.add_argument("--request-file", required=True, help="a JSON request (design 3.1 shape)")
    run.add_argument("--yes", action="store_true",
                     help="approve the plan automatically (recorded as mode auto)")
    run.add_argument("--confirm-long-run", action="store_true",
                     help="also confirm a production run above the long-run limit")
    structure = commands.add_parser(
        "structure", help="simulate a PDB/GRO structure using installed .rtp residues")
    structure.add_argument("--input", required=True, help="PDB or GRO coordinate file")
    structure.add_argument("--force-field", required=True,
                           help="installed force field directory name, without .ff")
    structure.add_argument("--water-model", required=True,
                           help="water model accepted by the selected force field")
    structure.add_argument("--stages", default="em,nvt",
                           choices=("em,nvt", "em,nvt,npt", "em,nvt,npt,prod"))
    structure.add_argument("--temperature", type=float, default=300.0, metavar="K")
    structure.add_argument("--pressure", type=float, default=1.0, metavar="BAR")
    structure.add_argument("--dt-ps", type=float, default=0.002)
    structure.add_argument("--nvt-ps", type=float, default=100.0)
    structure.add_argument("--npt-ps", type=float, default=100.0)
    structure.add_argument("--prod-ns", type=float, default=1.0)
    structure.add_argument("--padding-nm", type=float, default=1.0)
    structure.add_argument("--seed", type=int, default=42)
    structure.add_argument("--neutralize", action="store_true",
                           help="replace water with enough Na+/Cl- to neutralize the system")
    structure.add_argument("--analyses", metavar="TEXT",
                           help="plain-text measurements, e.g. 'RMSD, Rg and temperature'")
    structure.add_argument("--output-dir", help="new directory for this run")
    structure.add_argument("--yes", action="store_true", help="approve this structure run")
    analysis = commands.add_parser(
        "analyze-structure", help="analyze a completed structure run and write report.md")
    analysis.add_argument("--run-dir", required=True, help="completed structure run directory")
    analysis.add_argument("--analyses", metavar="TEXT",
                          help="select measurements for reanalysis without rerunning MD")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.version:
        print(f"simagent {__version__}")
        return 0
    if not _load_env(args.env_file,
                     use_default=args.command not in ("structure", "analyze-structure")):
        return 2
    try:
        cfg = load_config(yaml_path=default_config_path())
        cfg = apply_gromacs_paths(cfg, Path("gromacs_paths.yaml"))
    except (ConfigError, OSError, ValueError) as exc:
        _error(f"configuration: {exc}")
        return 2
    try:
        if args.command == "run":
            return _run_file(args, cfg)
        if args.command == "structure":
            return _run_structure(args, cfg)
        if args.command == "analyze-structure":
            return _analyze_structure(args, cfg)
        return _chat(cfg)
    except KeyboardInterrupt:
        print("\nInterrupted. A stage that was running keeps running; start simagent again "
              "and use /resume <job_id> to follow it.", file=sys.stderr)
        return 130
    except Exception as exc:  # report, never a traceback; the run itself is untouched
        _error(f"{type(exc).__name__}: {exc}")
        return 1


def _load_env(option: str | None, *, use_default: bool = True) -> bool:
    """Load an explicit env file, or the default for LLM-backed commands."""
    path = Path(option) if option else (
        Path(".env") if use_default and Path(".env").is_file() else None)
    if path is None:
        return True
    try:
        _, warnings = load_env_file(path, os.environ)
    except EnvFileError as exc:
        _error(str(exc))
        return False
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return True


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


def _run_structure(args: argparse.Namespace, cfg: Config) -> int:
    source = Path(args.input)
    if not source.is_file() or source.suffix.lower() not in (".pdb", ".gro"):
        _error("input must be an existing PDB or GRO coordinate file; "
               "in WSL, use /mnt/c/... for a file on Windows C:")
        return 2
    if not args.yes and not sys.stdin.isatty():
        _error("input is not a terminal: add --yes to approve this structure run")
        return 2
    default_name = f"{source.stem}-{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    output = Path(args.output_dir) if args.output_dir else (
        cfg.paths.runs_dir / "structures" / default_name)
    stage = args.stages.split(",")[-1]
    defaults = default_analyses(stage)
    if not args.yes:
        print(f"Structure: {source}\nForce field: {args.force_field}\n"
              f"Water model: {args.water_model}\nStages: {args.stages}\n"
              f"Neutralize with Na+/Cl-: {'yes' if args.neutralize else 'no'}\n"
              f"Output: {output}")
    if args.analyses is not None:
        try:
            selected = parse_analysis_text(args.analyses, stage)
        except StructureAnalysisError as exc:
            _error(str(exc))
            return 2
    elif args.yes:
        selected = defaults
    else:
        while True:
            response = input(f"Analyses [Enter for {analysis_labels(defaults)}]: ")
            try:
                selected = parse_analysis_text(response, stage)
                break
            except StructureAnalysisError as exc:
                _error(str(exc))
    options = StructureRun(source, output, args.force_field, args.water_model,
                           tuple(args.stages.split(",")), args.temperature, args.pressure,
                           args.dt_ps, args.nvt_ps, args.npt_ps, args.prod_ns,
                           args.padding_nm, args.seed, args.neutralize,
                           analyses=selected)
    if not args.yes:
        print(f"Analyses: {analysis_labels(selected)}")
        answer = input("Approve? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Not approved.")
            return 1
    try:
        destination = run_structure(
            options, cfg, GmxRunner(cfg, Ledger(output / "provenance.jsonl")))
    except StructureRunError as exc:
        _error(str(exc))
        return 1
    print(f"Structure run completed: {destination}")
    print(f"Analysis report: {destination / 'report.md'}")
    return 0


def _analyze_structure(args: argparse.Namespace, cfg: Config) -> int:
    run_dir = Path(args.run_dir)
    try:
        selected = (parse_analysis_text(args.analyses, "prod")
                    if args.analyses is not None else None)
        report = analyze_structure(
            run_dir, cfg, GmxRunner(cfg, Ledger(run_dir / "provenance.jsonl")),
            requested=selected)
    except StructureAnalysisError as exc:
        _error(str(exc))
        return 1
    print(f"Analysis report: {report}")
    return 0


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
    limit = cfg.workflow.max_production_ns_without_confirm
    if args.yes and request["protocol"]["prod_ns"] > limit and not args.confirm_long_run:
        _error(f"the production run is {request['protocol']['prod_ns']:g} ns, above the "
               f"{limit:g} ns limit: add --confirm-long-run to approve it automatically")
        return 2
    return asyncio.run(_run_async(cfg, request, args.yes, args.confirm_long_run))


async def _run_async(cfg: Config, request: dict[str, Any], yes: bool,
                     long_run_confirmed: bool) -> int:
    token = secrets.token_urlsafe(32)
    async with McpSession(server_params(_environment(), token)) as tools:
        session = Session(tools, None, cfg, make_console(), token,
                          interactive=sys.stdin.isatty(), sleep=_poll_sleep)
        if not await session.start():
            return 3
        outcome: Outcome = await session.run_request(
            request, auto_approve=yes, long_run_confirmed=long_run_confirmed)
    return 0 if outcome.status in ("done", "detached") else 1


def _environment() -> dict[str, str]:
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
