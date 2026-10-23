import tomllib
from pathlib import Path

import simagent
from simagent import cli

ROOT = Path(__file__).resolve().parents[2]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_version_is_0_1_0():
    assert simagent.__version__ == "0.1.0"


def test_cli_main_prints_version_and_returns_zero(capsys):
    assert cli.main(["--version"]) == 0
    assert "simagent 0.1.0" in capsys.readouterr().out


def test_ruff_blocks_shell_true():
    select = _pyproject()["tool"]["ruff"]["lint"]["select"]
    assert "S602" in select and "S605" in select


def test_markers_registered():
    markers = " ".join(_pyproject()["tool"]["pytest"]["ini_options"]["markers"])
    for name in ("posix", "real_gmx", "slow"):
        assert name in markers
