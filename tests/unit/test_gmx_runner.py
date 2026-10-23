import errno
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from simagent.config import GmxConfig, load_config
from simagent.core.gmx import GmxRunner, build_gmx_env
from simagent.core.paths import sha256_text
from simagent.core.provenance import Ledger
from simagent.errors import ErrorCode, SimAgentError

CANARY = "sk-canary-123456"


def _cfg(binary: str):
    cfg = load_config(env={})
    return replace(cfg, gmx=replace(GmxConfig(), bin=binary))


def test_env_allow_list_only():
    parent = {"FOO": "1", "PATH": "/usr/bin", "LUNA_API_KEY": CANARY,
              "SIMAGENT_APPROVAL_TOKEN": "tok-123456"}
    env = build_gmx_env(GmxConfig(), parent)
    assert env["PATH"] == "/usr/bin"
    for name in ("FOO", "LUNA_API_KEY", "SIMAGENT_APPROVAL_TOKEN"):
        assert name not in env


def test_secret_names_dropped_even_if_configured():
    gmx = replace(GmxConfig(), env_passthrough=("PATH", "LUNA_API_KEY"))
    env = build_gmx_env(gmx, {"PATH": "/p", "LUNA_API_KEY": CANARY})
    assert "LUNA_API_KEY" not in env


def test_env_derived_from_prefix(tmp_path):
    prefix = tmp_path / "gromacs"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "share" / "gromacs").mkdir(parents=True)
    (prefix / "lib64").mkdir()
    gmx = replace(GmxConfig(), bin=str(prefix / "bin" / "gmx"))
    env = build_gmx_env(gmx, {"LD_LIBRARY_PATH": "/existing"})
    assert Path(env["GMXDATA"]) == prefix / "share" / "gromacs"
    assert Path(env["GMXBIN"]) == prefix / "bin"
    parts = env["LD_LIBRARY_PATH"].split(__import__("os").pathsep)
    assert parts[0] == "/existing"
    assert str(prefix / "lib64") in parts
    assert str(prefix / "lib") not in parts


def test_run_records_start_and_end(tmp_path):
    ledger = Ledger(tmp_path / "provenance.jsonl")
    out = tmp_path / "out.txt"
    runner = GmxRunner(_cfg(sys.executable), ledger)
    result = runner.run(["-c", "print('hi')"], cwd=tmp_path, stage="em", tool="run_grompp",
                        base=tmp_path)
    out.write_text("x", encoding="utf-8")
    assert result.exit_code == 0 and result.stdout.strip() == "hi"
    records = ledger.records()
    assert [r["event"] for r in records] == ["start", "end"]
    end = records[1]
    assert end["argv"][0] == sys.executable and end["exit_code"] == 0
    assert end["stdout_sha256"] == sha256_text(result.stdout)
    assert end["stage"] == "em" and end["tool"] == "run_grompp" and end["kind"] == "gmx"
    assert result.seq_start == 1 and result.seq_end == 2


def test_end_record_lists_output_receipts(tmp_path):
    ledger = Ledger(tmp_path / "provenance.jsonl")
    runner = GmxRunner(_cfg(sys.executable), ledger)
    script = "open('made.txt','w').write('data')"
    runner.run(["-c", script], cwd=tmp_path, stage="em", tool="t", base=tmp_path,
               outputs=[tmp_path / "made.txt"])
    end = ledger.records()[-1]
    assert end["outputs"][0]["path"] == "made.txt"
    assert ledger.has_receipt("made.txt", end["outputs"][0]["sha256"])


def test_semicolon_argument_is_literal(tmp_path):
    runner = GmxRunner(_cfg(sys.executable), None)
    result = runner.run(["-c", "import sys; print(sys.argv[1])", "a;b"], cwd=tmp_path)
    assert result.stdout.strip() == "a;b"


def test_timeout_maps_to_E_TIMEOUT(tmp_path):
    runner = GmxRunner(_cfg(sys.executable), None)
    with pytest.raises(SimAgentError) as exc:
        runner.run(["-c", "import time; time.sleep(5)"], cwd=tmp_path, timeout=0.3)
    assert exc.value.code == ErrorCode.E_TIMEOUT


def test_enospc_maps_to_E_DISK_FULL(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(subprocess, "run", boom)
    runner = GmxRunner(_cfg(sys.executable), None)
    with pytest.raises(SimAgentError) as exc:
        runner.run(["-c", "pass"], cwd=tmp_path)
    assert exc.value.code == ErrorCode.E_DISK_FULL


def test_missing_binary_maps_to_E_GMX_NOT_FOUND(tmp_path):
    runner = GmxRunner(_cfg(str(tmp_path / "nope")), None)
    with pytest.raises(SimAgentError) as exc:
        runner.run(["--version"], cwd=tmp_path)
    assert exc.value.code == ErrorCode.E_GMX_NOT_FOUND


def test_no_secret_in_ledger(tmp_path):
    ledger = Ledger(tmp_path / "provenance.jsonl")
    runner = GmxRunner(_cfg(sys.executable), ledger,
                       parent_env={"LUNA_API_KEY": CANARY, "PATH": "/p"})
    runner.run(["-c", "pass"], cwd=tmp_path, base=tmp_path)
    assert CANARY not in (tmp_path / "provenance.jsonl").read_text(encoding="utf-8")
