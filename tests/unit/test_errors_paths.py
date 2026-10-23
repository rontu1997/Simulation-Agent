import os

import pytest

from simagent.core.paths import confine, sha256_file, sha256_text, validate_job_id
from simagent.errors import ErrorCode, SimAgentError, error_envelope, fail, ok

DESIGN_CODES = {
    "E_GMX_NOT_FOUND", "E_GMX_NOT_ELF", "E_GMX_VERSION", "E_DATA_MISSING", "E_FAKE_BINARY",
    "E_PROBE_FAILED", "E_PARAM_OUT_OF_RANGE", "E_PATH_ESCAPE", "E_JOB_NOT_FOUND",
    "E_STAGE_ORDER", "E_STAGE_STATE", "E_RETRY_NOT_ALLOWED", "E_RETRY_LIMIT", "E_NOT_APPROVED",
    "E_APPROVAL_UNAVAILABLE", "E_BAD_TOKEN", "E_PLAN_MISMATCH", "E_INPUT_MODIFIED",
    "E_BINARY_CHANGED", "E_BUILD_COUNT", "E_GROMPP_FAILED", "E_MDRUN_FAILED", "E_MDRUN_RUNNING",
    "E_NO_RECEIPT", "E_GATE_FAILED", "E_ANALYSIS_INPUT", "E_CANCELLED", "E_TIMEOUT",
    "E_DISK_FULL", "E_INTERNAL",
}


def test_error_codes_match_design():
    assert {c.value for c in ErrorCode} == DESIGN_CODES
    assert all(c.value == c.name for c in ErrorCode)


def test_envelope_shapes():
    assert ok({"a": 1}) == {"ok": True, "data": {"a": 1}, "error": None}
    assert ok() == {"ok": True, "data": {}, "error": None}
    env = fail(ErrorCode.E_STAGE_ORDER, "m", "h")
    assert env["ok"] is False
    assert env["error"] == {"code": "E_STAGE_ORDER", "message": "m", "hint": "h"}


def test_error_envelope_carries_data():
    exc = SimAgentError(ErrorCode.E_GROMPP_FAILED, "bad", hint="see log", data={"warnings": [1]})
    env = error_envelope(exc)
    assert env["ok"] is False and env["data"] == {"warnings": [1]}
    assert env["error"]["code"] == "E_GROMPP_FAILED"


@pytest.mark.parametrize("bad", ["../x", "a/b", "a\\b", ".hidden", "a..b", "/abs", "../../etc"])
def test_job_id_path_escape(bad):
    with pytest.raises(SimAgentError) as exc:
        validate_job_id(bad)
    assert exc.value.code == ErrorCode.E_PATH_ESCAPE


@pytest.mark.parametrize("bad", ["", "x" * 81, "a b", "é", "-lead"])
def test_job_id_malformed(bad):
    with pytest.raises(SimAgentError) as exc:
        validate_job_id(bad)
    assert exc.value.code == ErrorCode.E_PARAM_OUT_OF_RANGE


def test_job_id_valid():
    jid = "water-1000-tip4p-300K-20261023-1"
    assert validate_job_id(jid) == jid


def test_confine_rejects_escape(tmp_path):
    base = tmp_path / "job"
    base.mkdir()
    for bad in ("../x", "../../x", str(tmp_path / "other")):
        with pytest.raises(SimAgentError) as exc:
            confine(base, bad)
        assert exc.value.code == ErrorCode.E_PATH_ESCAPE


def test_confine_rejects_symlink_escape(tmp_path):
    base = tmp_path / "job"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    try:
        os.symlink(outside, base / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks cannot be created on this host")
    with pytest.raises(SimAgentError) as exc:
        confine(base, "link/file.txt")
    assert exc.value.code == ErrorCode.E_PATH_ESCAPE


def test_confine_nested_ok(tmp_path):
    base = tmp_path / "job"
    base.mkdir()
    assert confine(base, "02_em/em.mdp") == (base / "02_em" / "em.mdp").resolve()


def test_sha256_known_digest(tmp_path):
    expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert sha256_text("abc") == expected
    f = tmp_path / "f.txt"
    f.write_bytes(b"abc")
    assert sha256_file(f) == expected
