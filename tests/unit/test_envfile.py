import sys

import pytest

from simagent import cli
from simagent.envfile import EnvFileError, load_env_file, parse_env

SECRET = "sk-secret-0123456789"


def test_parse_handles_comments_blanks_export_quotes_and_spacing():
    text = (
        "# a comment\n"
        "\n"
        "LUNA_API_KEY=plain\n"
        "export LUNA_BASE_URL = https://host/v1  \n"
        'LUNA_MODEL="gpt-6-luna"\n'
        "GMX_BIN='/opt/gmx/bin/gmx'\n"
        "GMX_NTOMP=4   # four threads\n"
        'SIMAGENT_USER="has # hash inside"\n'
        "SIMAGENT_ALLOW_FAKE=\n")
    pairs, problems = parse_env(text)
    assert pairs == {
        "LUNA_API_KEY": "plain", "LUNA_BASE_URL": "https://host/v1", "LUNA_MODEL": "gpt-6-luna",
        "GMX_BIN": "/opt/gmx/bin/gmx", "GMX_NTOMP": "4", "SIMAGENT_USER": "has # hash inside",
        "SIMAGENT_ALLOW_FAKE": ""}
    assert problems == []


def test_parse_reports_bad_lines_by_number_without_their_content():
    text = f"LUNA_API_KEY={SECRET}\nthis is not an assignment {SECRET}\n9BAD=1\n'unclosed=1\n"
    pairs, problems = parse_env(text)
    assert pairs == {"LUNA_API_KEY": SECRET}
    assert [p.split(":")[0] for p in problems] == ["line 2", "line 3", "line 4"]
    assert SECRET not in " ".join(problems)


def test_the_real_environment_wins_over_the_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("LUNA_BASE_URL=https://from-file/v1\nLUNA_MODEL=from-file\n", encoding="utf-8")
    path.chmod(0o600)  # a world-readable file would add a warning
    environ = {"LUNA_BASE_URL": "https://from-shell/v1"}
    loaded, warnings = load_env_file(path, environ)
    assert environ == {"LUNA_BASE_URL": "https://from-shell/v1", "LUNA_MODEL": "from-file"}
    assert loaded == ["LUNA_MODEL"] and warnings == []


def test_only_project_keys_are_accepted_and_the_token_never_comes_from_a_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        f"PATH=/evil\nLD_PRELOAD=/evil.so\nHOME=/x\nSIMAGENT_APPROVAL_TOKEN={SECRET}\n"
        f"SIMAGENT_APPROVAL_TOKENX=1\nLUNA_API_KEY={SECRET}\nGMX_BIN=/opt/gmx\n",
        encoding="utf-8")
    environ: dict[str, str] = {}
    loaded, warnings = load_env_file(path, environ)
    assert environ == {"LUNA_API_KEY": SECRET, "GMX_BIN": "/opt/gmx"}
    assert loaded == ["LUNA_API_KEY", "GMX_BIN"]
    ignored = " ".join(warnings)
    for name in ("PATH", "LD_PRELOAD", "HOME", "SIMAGENT_APPROVAL_TOKEN"):
        assert name in ignored
    assert SECRET not in ignored and "/evil" not in ignored


def test_a_missing_or_unreadable_file_is_an_error(tmp_path):
    with pytest.raises(EnvFileError, match="cannot read"):
        load_env_file(tmp_path / "nope.env", {})
    with pytest.raises(EnvFileError, match="cannot read"):
        load_env_file(tmp_path, {})


def test_a_file_that_is_not_text_is_an_error(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(EnvFileError):
        load_env_file(path, {})


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_a_file_readable_by_others_gets_a_warning(tmp_path):
    path = tmp_path / ".env"
    path.write_text("LUNA_API_KEY=x\n", encoding="utf-8")
    path.chmod(0o644)
    _, warnings = load_env_file(path, {})
    assert any("readable by other users" in w and "chmod 600" in w for w in warnings)
    path.chmod(0o600)
    assert load_env_file(path, {})[1] == []


class StopAtServer:
    """Reaching the MCP server means startup got past the key checks."""

    launched = 0

    def __init__(self, params):
        StopAtServer.launched += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def call(self, name, args):
        return {"ok": False, "data": {}, "error": {"code": "E_GMX_NOT_FOUND",
                                                    "message": "stop here", "hint": ""}}

    async def list_tools(self):
        return []


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    for name in ("LUNA_API_KEY", "LUNA_BASE_URL", "LUNA_MODEL", "SIMAGENT_CONFIG", "GMX_BIN"):
        monkeypatch.setenv(name, "x")  # so the test's own loading is undone afterwards
        monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "McpSession", StopAtServer)
    StopAtServer.launched = 0
    return tmp_path


def test_cli_loads_dot_env_from_the_working_directory(clean_env, capsys):
    (clean_env / ".env").write_text(
        f"LUNA_API_KEY={SECRET}\nLUNA_BASE_URL=http://example.invalid/v1\n", encoding="utf-8")
    assert cli.main([]) == 3  # past the key checks, stopped by the fake server
    assert StopAtServer.launched == 1
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def test_cli_without_a_dot_env_still_asks_for_the_key(clean_env, capsys):
    assert cli.main([]) == 3
    assert "LUNA_API_KEY is not set" in capsys.readouterr().err and StopAtServer.launched == 0


def test_env_file_option_names_another_file(clean_env, capsys):
    other = clean_env / "prod.env"
    other.write_text(f"LUNA_API_KEY={SECRET}\nLUNA_BASE_URL=http://example.invalid/v1\n",
                     encoding="utf-8")
    assert cli.main(["--env-file", str(other)]) == 3
    assert StopAtServer.launched == 1


def test_env_file_option_with_a_missing_file_exits_2(clean_env, capsys):
    assert cli.main(["--env-file", str(clean_env / "missing.env")]) == 2
    assert "cannot read" in capsys.readouterr().err and StopAtServer.launched == 0


def test_cli_prints_warnings_without_values(clean_env, capsys):
    (clean_env / ".env").write_text(
        f"PATH=/evil\nLUNA_API_KEY={SECRET}\nLUNA_BASE_URL=http://example.invalid/v1\n",
        encoding="utf-8")
    cli.main([])
    err = capsys.readouterr().err
    assert "PATH" in err and "ignored" in err and "/evil" not in err and SECRET not in err


def test_the_option_works_before_a_subcommand(clean_env, capsys, tmp_path):
    other = clean_env / "x.env"
    other.write_text("GMX_BIN=/somewhere/gmx\n", encoding="utf-8")
    assert cli.main(["--env-file", str(other), "run", "--request-file",
                     str(clean_env / "missing.json"), "--yes"]) == 2  # request file, not env
    assert "request file" in capsys.readouterr().err


def test_a_file_saved_by_notepad_with_a_byte_order_mark_and_crlf_still_loads(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"\xef\xbb\xbfLUNA_API_KEY=abc\r\nLUNA_MODEL=gpt-6-luna\r\n")
    path.chmod(0o600)
    environ: dict[str, str] = {}
    loaded, warnings = load_env_file(path, environ)
    assert environ == {"LUNA_API_KEY": "abc", "LUNA_MODEL": "gpt-6-luna"} and warnings == []
