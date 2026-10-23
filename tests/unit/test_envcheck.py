from dataclasses import replace
from pathlib import Path

import pytest

from simagent.config import GmxConfig, load_config
from simagent.core.envcheck import check_environment, run_probe
from simagent.core.gmx import RunResult
from simagent.errors import ErrorCode, SimAgentError

REAL_VERSION = (
    "GROMACS version:    2025.3\nPrecision:          mixed\nGPU support:        disabled\n")
FAKE_VERSION = "GROMACS version:    FAKE-0.0\n"


def make_prefix(tmp_path: Path, *, data: bool = True, content: bytes = b"\x7fELF\x02\x01\x01\x00"):
    prefix = tmp_path / "gromacs"
    (prefix / "bin").mkdir(parents=True)
    binary = prefix / "bin" / "gmx"
    binary.write_bytes(content)
    binary.chmod(0o755)  # the check requires an executable file; Windows ignores the bit
    if data:
        top = prefix / "share" / "gromacs" / "top"
        (top / "oplsaa.ff").mkdir(parents=True)
        (top / "oplsaa.ff" / "forcefield.itp").write_text("; ff\n", encoding="utf-8")
        (top / "oplsaa.ff" / "tip4p.itp").write_text("; tip4p\n", encoding="utf-8")
        (top / "tip4p.gro").write_text("tip4p\n", encoding="utf-8")
    return binary


def cfg_for(binary: Path, **overrides):
    gmx = replace(GmxConfig(), bin=str(binary), **overrides.pop("gmx", {}))
    return replace(load_config(env={}), gmx=gmx, **overrides)


def ok_probe():
    return {"ok": True, "wall_s": 0.5}


def test_missing_binary(tmp_path):
    cfg = cfg_for(tmp_path / "nope")
    with pytest.raises(SimAgentError) as exc:
        check_environment(cfg, version_fn=lambda: REAL_VERSION, probe_fn=ok_probe)
    assert exc.value.code == ErrorCode.E_GMX_NOT_FOUND
    assert "GMX_BIN" in exc.value.hint


def test_directory_not_file(tmp_path):
    with pytest.raises(SimAgentError) as exc:
        check_environment(cfg_for(tmp_path), version_fn=lambda: REAL_VERSION, probe_fn=ok_probe)
    assert exc.value.code == ErrorCode.E_GMX_NOT_FOUND


def test_text_file_not_elf_not_script(tmp_path):
    binary = make_prefix(tmp_path, content=b"hello world\n")
    with pytest.raises(SimAgentError) as exc:
        check_environment(cfg_for(binary), version_fn=lambda: REAL_VERSION, probe_fn=ok_probe)
    assert exc.value.code == ErrorCode.E_GMX_NOT_ELF


def test_script_without_flag_is_fake(tmp_path):
    binary = make_prefix(tmp_path, content=b"#!/bin/sh\necho hi\n")
    with pytest.raises(SimAgentError) as exc:
        check_environment(cfg_for(binary), version_fn=lambda: FAKE_VERSION, probe_fn=ok_probe)
    assert exc.value.code == ErrorCode.E_FAKE_BINARY


def test_script_with_flag_and_FAKE_output_is_simulated(tmp_path):
    binary = make_prefix(tmp_path, data=False, content=b"#!/bin/sh\necho hi\n")
    result = check_environment(cfg_for(binary, allow_fake=True),
                               version_fn=lambda: FAKE_VERSION, probe_fn=ok_probe)
    assert result["execution_mode"] == "simulated"
    assert result["probe"] == "skipped (simulated)"
    assert result["is_elf"] is False


def test_script_with_flag_but_no_FAKE_output(tmp_path):
    binary = make_prefix(tmp_path, content=b"#!/bin/sh\necho hi\n")
    with pytest.raises(SimAgentError) as exc:
        check_environment(cfg_for(binary, allow_fake=True),
                          version_fn=lambda: REAL_VERSION, probe_fn=ok_probe)
    assert exc.value.code == ErrorCode.E_FAKE_BINARY


def test_real_elf_ignores_allow_fake(tmp_path):
    binary = make_prefix(tmp_path)
    result = check_environment(cfg_for(binary, allow_fake=True),
                               version_fn=lambda: REAL_VERSION, probe_fn=ok_probe)
    assert result["execution_mode"] == "real"
    assert any("SIMAGENT_ALLOW_FAKE" in w for w in result["warnings"])


def test_wrong_version_prefix(tmp_path):
    binary = make_prefix(tmp_path)
    with pytest.raises(SimAgentError) as exc:
        check_environment(cfg_for(binary), probe_fn=ok_probe,
                          version_fn=lambda: "GROMACS version:    2024.1\n")
    assert exc.value.code == ErrorCode.E_GMX_VERSION


def test_sha256_pin_mismatch(tmp_path):
    binary = make_prefix(tmp_path)
    cfg = cfg_for(binary, gmx={"expected_sha256": "0" * 64})
    with pytest.raises(SimAgentError) as exc:
        check_environment(cfg, version_fn=lambda: REAL_VERSION, probe_fn=ok_probe)
    assert exc.value.code == ErrorCode.E_GMX_VERSION


def test_missing_oplsaa_data(tmp_path):
    binary = make_prefix(tmp_path, data=False)
    with pytest.raises(SimAgentError) as exc:
        check_environment(cfg_for(binary), version_fn=lambda: REAL_VERSION, probe_fn=ok_probe)
    assert exc.value.code == ErrorCode.E_DATA_MISSING


def test_probe_failure_propagates(tmp_path):
    binary = make_prefix(tmp_path)

    def bad_probe():
        raise SimAgentError(ErrorCode.E_PROBE_FAILED, "grompp failed")

    with pytest.raises(SimAgentError) as exc:
        check_environment(cfg_for(binary), version_fn=lambda: REAL_VERSION, probe_fn=bad_probe)
    assert exc.value.code == ErrorCode.E_PROBE_FAILED


def test_success_shape(tmp_path):
    binary = make_prefix(tmp_path)
    result = check_environment(cfg_for(binary), version_fn=lambda: REAL_VERSION,
                               probe_fn=ok_probe)
    for key in ("bin_path", "sha256", "is_elf", "version", "has_oplsaa", "has_tip4p_itp",
                "has_tip4p_gro", "ncpu", "gpu_detected", "env_allow", "probe",
                "execution_mode", "warnings"):
        assert key in result
    assert result["execution_mode"] == "real" and result["is_elf"] is True
    assert result["version"] == "2025.3" and result["gpu_detected"] is False
    assert result["has_oplsaa"] and result["has_tip4p_itp"] and result["has_tip4p_gro"]
    assert result["probe"] == {"ok": True, "wall_s": 0.5}


class ScriptedRunner:
    """Stands in for GmxRunner: records argv and writes the files gmx would write."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def run(self, args, *, cwd, **kwargs):
        self.calls.append(list(args))
        cwd = Path(cwd)
        sub = args[0]
        if sub == "solvate":
            n = 112
            lines = ["water", str(4 * n)] + ["    1SOL     OW    1   0.0   0.0   0.0"] * (4 * n)
            (cwd / args[args.index("-o") + 1]).write_text("\n".join(lines) + "\n   1.5 1.5 1.5\n")
        if sub == "mdrun":
            (cwd / "probe.log").write_text("Finished mdrun on rank 0\n")
            (cwd / "probe.edr").write_text("x")
        if self.fail_on == sub:
            return RunResult(1, "", f"fatal error in {sub}")
        return RunResult(0, "", "")


def test_probe_commands_and_cleanup(tmp_path):
    cfg = replace(load_config(env={}), paths=replace(load_config(env={}).paths,
                                                     runs_dir=tmp_path / "runs"))
    runner = ScriptedRunner()
    result = run_probe(cfg, runner)
    assert result["ok"] is True
    subs = [c[0] for c in runner.calls]
    assert subs == ["solvate", "grompp", "mdrun", "check"]
    assert runner.calls[0][runner.calls[0].index("-box") + 1:runner.calls[0].index("-box") + 4] \
        == ["1.5", "1.5", "1.5"]
    assert not list((tmp_path / "runs" / "_probe").glob("*"))  # removed on success


def test_probe_failure_keeps_directory_and_names_command(tmp_path):
    cfg = replace(load_config(env={}), paths=replace(load_config(env={}).paths,
                                                     runs_dir=tmp_path / "runs"))
    with pytest.raises(SimAgentError) as exc:
        run_probe(cfg, ScriptedRunner(fail_on="grompp"))
    assert exc.value.code == ErrorCode.E_PROBE_FAILED
    assert "grompp" in exc.value.message and "fatal error in grompp" in exc.value.message
    assert len(list((tmp_path / "runs" / "_probe").glob("*"))) == 1
