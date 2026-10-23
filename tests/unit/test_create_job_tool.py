from dataclasses import replace
from pathlib import Path

from simagent.config import GmxConfig, load_config
from simagent.events import EventLog, Redactor
from simagent.mcp_server.registry import ServerContext
from simagent.mcp_server.tools_job import CreateJobArgs, create_job

REQUEST = {
    "system": {"kind": "water", "n_molecules": 1000, "water_model": "tip4p"},
    "force_field": "oplsaa",
    "thermo": {"temperature_K": 300.0, "pressure_bar": 1.0},
    "protocol": {"prod_ns": 1.0},
    "seed": None,
    "analyses": ["rdf_OO"],
}


def make_ctx(tmp_path: Path, binary: Path):
    cfg = load_config(env={})
    cfg = replace(cfg, gmx=replace(GmxConfig(), bin=str(binary)),
                  paths=replace(cfg.paths, runs_dir=tmp_path / "runs",
                                sim_runs_dir=tmp_path / "runs_simulated"))
    ctx = ServerContext(cfg=cfg, events=EventLog(tmp_path / "ev.jsonl", Redactor([])))
    calls = []

    def fake_check(c):
        calls.append(1)
        return {"sha256": "x", "execution_mode": "real", "env_allow": {"PATH": "p"},
                "version": "2025.3", "probe": {"ok": True}}

    ctx.extras["check_environment"] = fake_check
    return ctx, calls


def test_create_job_returns_ids_and_environment(tmp_path):
    binary = tmp_path / "gmx"
    binary.write_bytes(b"\x7fELF1")
    ctx, _ = make_ctx(tmp_path, binary)
    out = create_job(ctx, CreateJobArgs(request=REQUEST))
    assert out["job_id"].startswith("water-1000-tip4p-300K-")
    assert Path(out["job_dir"]).is_dir()
    assert out["environment"]["execution_mode"] == "real"


def test_environment_check_is_cached_until_binary_changes(tmp_path):
    binary = tmp_path / "gmx"
    binary.write_bytes(b"\x7fELF1")
    ctx, calls = make_ctx(tmp_path, binary)
    create_job(ctx, CreateJobArgs(request=REQUEST))
    create_job(ctx, CreateJobArgs(request=REQUEST))
    assert len(calls) == 1
    binary.write_bytes(b"\x7fELF2-changed")
    create_job(ctx, CreateJobArgs(request=REQUEST))
    assert len(calls) == 2


def test_simulated_environment_creates_job_under_sim_dir(tmp_path):
    binary = tmp_path / "gmx"
    binary.write_bytes(b"\x7fELF1")
    ctx, _ = make_ctx(tmp_path, binary)
    ctx.extras["check_environment"] = lambda c: {
        "sha256": "x", "execution_mode": "simulated", "env_allow": {}, "version": "FAKE-0.0",
        "probe": "skipped (simulated)"}
    out = create_job(ctx, CreateJobArgs(request=REQUEST))
    assert Path(out["job_dir"]).parent.name == "runs_simulated"


def test_create_job_rejects_out_of_range_request_before_any_work(tmp_path):
    import copy

    import pytest

    from simagent.errors import ErrorCode, SimAgentError

    binary = tmp_path / "gmx"
    binary.write_bytes(b"\x7fELF1")
    ctx, calls = make_ctx(tmp_path, binary)
    bad = copy.deepcopy(REQUEST)
    bad["thermo"]["temperature_K"] = 199
    with pytest.raises(SimAgentError) as exc:
        create_job(ctx, CreateJobArgs(request=bad))
    assert exc.value.code == ErrorCode.E_PARAM_OUT_OF_RANGE
    assert calls == []
    assert not (tmp_path / "runs").exists() or not list((tmp_path / "runs").iterdir())


def test_environment_records_binary_stat_for_continuity_checks(tmp_path):
    binary = tmp_path / "gmx"
    binary.write_bytes(b"\x7fELF1")
    ctx, _ = make_ctx(tmp_path, binary)
    out = create_job(ctx, CreateJobArgs(request=REQUEST))
    stat = binary.stat()
    assert out["environment"]["bin_stat"] == {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    stored = ctx.extras["store"].get(out["job_id"]).read()["environment"]
    assert stored["bin_stat"]["size"] == stat.st_size
