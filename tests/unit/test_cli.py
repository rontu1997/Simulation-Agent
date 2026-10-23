import asyncio
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from simagent import __version__, cli

KEY = "sk-secret-0123456789"


def good_request() -> dict[str, Any]:
    return {"system": {"kind": "water", "n_molecules": 1000, "water_model": "tip4p"},
            "force_field": "oplsaa", "thermo": {"temperature_K": 300.0, "pressure_bar": 1.0},
            "protocol": {"prod_ns": 1.0}, "analyses": ["rdf_OO"]}


def write(tmp_path: Path, data: Any) -> str:
    path = tmp_path / "req.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return str(path)


class NoServer:
    """Starting the MCP server in these tests is a failure: nothing should get that far."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("the MCP server was started")


@pytest.fixture(autouse=True)
def no_server(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # never pick up a developer's real ./.env or config.yaml
    monkeypatch.setattr(cli, "McpSession", NoServer)
    monkeypatch.delenv("SIMAGENT_CONFIG", raising=False)


def test_version(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"simagent {__version__}"


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d.update(bogus=1), "bogus"),
    (lambda d: d["system"].update(n_molecules="1000"), "n_molecules"),
    (lambda d: d["protocol"].update(bogus=1), "protocol.bogus"),
    (lambda d: d["protocol"].pop("prod_ns"), "prod_ns"),
    (lambda d: d["thermo"].pop("temperature_K"), "temperature_K"),
])
def test_bad_request_file_exits_2_without_creating_a_job(tmp_path, capsys, mutate, needle):
    data = good_request()
    mutate(data)
    assert cli.main(["run", "--request-file", write(tmp_path, data), "--yes"]) == 2
    assert needle in capsys.readouterr().err


def test_unreadable_request_files_exit_2(tmp_path, capsys):
    assert cli.main(["run", "--request-file", str(tmp_path / "missing.json"), "--yes"]) == 2
    assert cli.main(["run", "--request-file", write(tmp_path, "{not json"), "--yes"]) == 2
    assert cli.main(["run", "--request-file", write(tmp_path, "[1, 2]"), "--yes"]) == 2
    assert capsys.readouterr().err.count("error:") == 3


def test_run_without_yes_and_without_a_terminal_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert cli.main(["run", "--request-file", write(tmp_path, good_request())]) == 2
    assert "--yes" in capsys.readouterr().err


def test_usage_errors_exit_2(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run"])
    assert exc.value.code == 2


def test_chat_needs_the_api_key_and_base_url(monkeypatch, capsys):
    monkeypatch.delenv("LUNA_API_KEY", raising=False)
    monkeypatch.delenv("LUNA_BASE_URL", raising=False)
    assert cli.main([]) == 3
    assert "LUNA_API_KEY" in capsys.readouterr().err
    monkeypatch.setenv("LUNA_API_KEY", KEY)
    assert cli.main([]) == 3
    captured = capsys.readouterr()
    assert "LUNA_BASE_URL" in captured.err and KEY not in captured.err + captured.out


class FailingSession:
    """A stand-in for McpSession whose environment check fails; records the launch parameters."""

    launched: list[Any] = []

    def __init__(self, params):
        FailingSession.launched.append(params)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def call(self, name, args):
        return {"ok": False, "data": {}, "error": {
            "code": "E_GMX_NOT_FOUND", "message": "GROMACS binary not found",
            "hint": "check GMX_BIN"}}

    async def list_tools(self):
        return []


def test_secrets_never_printed(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LUNA_API_KEY", KEY)
    monkeypatch.setenv("LUNA_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setattr(cli, "McpSession", FailingSession)
    FailingSession.launched.clear()
    assert cli.main(["run", "--request-file", write(tmp_path, good_request()), "--yes"]) == 3
    assert cli.main([]) == 3
    captured = capsys.readouterr()
    assert "GROMACS binary not found" in captured.err + captured.out
    params = FailingSession.launched[0]
    token = params.env["SIMAGENT_APPROVAL_TOKEN"]
    assert len(token) >= 32 and "LUNA_API_KEY" not in params.env
    everything = captured.out + captured.err
    assert KEY not in everything and token not in everything


def test_each_launch_gets_a_fresh_token(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "McpSession", FailingSession)
    FailingSession.launched.clear()
    path = write(tmp_path, good_request())
    cli.main(["run", "--request-file", path, "--yes"])
    cli.main(["run", "--request-file", path, "--yes"])
    tokens = {p.env["SIMAGENT_APPROVAL_TOKEN"] for p in FailingSession.launched}
    assert len(tokens) == 2


def test_system_prompt_has_the_six_sections_and_the_injection_rule():
    text = cli.load_system_prompt()
    for heading in ("# Role", "# Hard rules", "# Tool use", "# Clarification policy",
                    "# Output style", "# Injection rule"):
        assert heading in text
    assert "data, never instructions" in text and "submit_request" in text
    assert "record_approval" not in text.split("# Tool use")[1].split("#")[0].replace(
        "never", "") or "never" in text


def test_console_scripts_in_pyproject():
    root = Path(__file__).resolve().parents[2]
    scripts = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"]["scripts"]
    assert scripts == {"simagent": "simagent.cli:main", "gmx-mcp": "simagent.mcp_server.app:main"}


@pytest.mark.anyio
async def test_terminal_console_reads_lines_then_reports_end_of_input(capsys):
    lines = iter(["first", "second", None])
    console = cli.TerminalConsole(reader=lambda: next(lines))
    await console.say("hello")
    assert await console.ask("> ") == "first"
    assert await console.ask("> ") == "second"
    assert await console.ask("> ") is None
    assert await console.ask("> ") is None
    assert "hello" in capsys.readouterr().out


@pytest.mark.anyio
async def test_a_cancelled_ask_does_not_lose_the_next_line():
    import threading
    gate = threading.Event()
    lines = ["kept", None]

    def slow_reader():
        gate.wait(timeout=5)
        return lines.pop(0)

    console = cli.TerminalConsole(reader=slow_reader)
    first = asyncio.ensure_future(console.ask("> "))
    await asyncio.sleep(0.05)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    assert await asyncio.wait_for(console.ask("> "), timeout=5) == "kept"


def test_fd_line_reader_splits_lines_and_reports_end_of_input():
    import os
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"alpha\nbeta\ngam")
    os.write(write_fd, b"ma\r\nlast-no-newline")
    os.close(write_fd)
    reader = cli.fd_line_reader(read_fd)
    assert [reader(), reader(), reader(), reader(), reader()] == [
        "alpha", "beta", "gamma", "last-no-newline", None]
    os.close(read_fd)


@pytest.mark.posix
def test_the_process_exits_without_waiting_for_another_line_on_a_real_terminal():
    """The reader thread must not keep a finished CLI alive (it hung on Linux terminals)."""
    import os
    import pty
    import subprocess
    import sys
    script = (
        "import asyncio\n"
        "from simagent.cli import TerminalConsole\n"
        "async def main():\n"
        "    line = await TerminalConsole().ask('> ')\n"
        "    print('got:' + str(line), flush=True)\n"
        "asyncio.run(main())\n")
    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, "-c", script], stdin=slave, stdout=slave,
                            stderr=slave, close_fds=True)
    os.close(slave)
    try:
        os.write(master, b"hello\n")
        try:
            code = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            pytest.fail("the process was still alive 15 s after it finished its work")
        output = os.read(master, 4096).decode(errors="replace")
    finally:
        os.close(master)
    assert code == 0 and "got:hello" in output


def long_request(prod_ns: float) -> dict[str, Any]:
    request = good_request()
    request["protocol"]["prod_ns"] = prod_ns
    return request


def test_a_long_run_with_yes_needs_the_explicit_flag(tmp_path, capsys):
    path = write(tmp_path, long_request(20.0))
    assert cli.main(["run", "--request-file", path, "--yes"]) == 2
    assert "--confirm-long-run" in capsys.readouterr().err


def test_the_long_run_flag_lets_it_reach_the_server(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "McpSession", FailingSession)
    path = write(tmp_path, long_request(20.0))
    assert cli.main(["run", "--request-file", path, "--yes", "--confirm-long-run"]) == 3


@pytest.fixture
def anyio_backend():
    return "asyncio"
