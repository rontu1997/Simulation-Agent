import copy
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import simagent.core.grompp as grompp_module
from simagent.config import GmxConfig, load_config
from simagent.core.gmx import RunResult
from simagent.core.grompp import IN_GRO, parse_warnings, run_grompp
from simagent.core.jobs import JobStore
from simagent.core.mdp import build_default_plan, render_mdp
from simagent.core.paths import sha256_file, sha256_text
from simagent.core.provenance import record_internal
from simagent.errors import ErrorCode, SimAgentError

REQUEST = {
    "system": {"kind": "water", "n_molecules": 1000, "water_model": "tip4p"},
    "force_field": "oplsaa",
    "thermo": {"temperature_K": 300.0, "pressure_bar": 1.0},
    "protocol": {"emtol": 1000.0, "nvt_ps": 100, "npt_ps": 100, "prod_ns": 1.0, "dt_ps": 0.002,
                 "tau_t": 0.1, "tau_p": 2.0},
    "seed": 1904784231,
    "analyses": ["rdf_OO"],
}
WARNING_1 = "WARNING 1 [file topol.top, line 12]:\n  first problem\n  continues here\n\n"
WARNING_2 = "WARNING 2 [file em.mdp, line 3]:\n  second problem\n\n"
ORDER = ["build", "em", "nvt", "npt", "prod"]


class Scripted:
    """Returns scripted results in order and writes the output tpr on success."""

    def __init__(self, *results: RunResult) -> None:
        self.results = list(results) or [RunResult(0, "", "")]
        self.calls: list[list[str]] = []

    def run(self, args, *, cwd, **kwargs):
        self.calls.append(list(args))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if result.exit_code == 0:
            (Path(cwd) / args[args.index("-o") + 1]).write_text("tpr", encoding="utf-8")
        return result


class Env:
    pass


def make_env(tmp_path, target="em"):
    binary = tmp_path / "gmx"
    binary.write_bytes(b"\x7fELF-original")
    cfg = load_config(env={})
    cfg = replace(cfg, gmx=replace(GmxConfig(), bin=str(binary)),
                  paths=replace(cfg.paths, runs_dir=tmp_path / "runs", sim_runs_dir=tmp_path / "s"))
    st = binary.stat()
    environment = {"sha256": sha256_file(binary),
                   "bin_stat": {"size": st.st_size, "mtime_ns": st.st_mtime_ns}}
    job = JobStore(cfg, clock=lambda: datetime(2026, 10, 23, tzinfo=UTC)).create(
        REQUEST, "real", environment)
    build_dir = job.stage_dir("build")
    (build_dir / "water.gro").write_text("gro", encoding="utf-8")
    (build_dir / "topol.top").write_text("top", encoding="utf-8")
    build_sha = sha256_text(sha256_file(build_dir / "water.gro")
                            + sha256_file(build_dir / "topol.top"))
    plan = build_default_plan(copy.deepcopy(REQUEST), {"box_nm": 3.107, "build_sha256": build_sha})
    job.update(lambda d: d.update(plan=plan))
    record_internal(job.ledger, stage="build", tool="build_water_box", base=job.dir,
                    outputs=["01_build/water.gro", "01_build/topol.top"])
    # earlier stages passed, with the files the target stage consumes
    for earlier in ORDER[:ORDER.index(target)]:
        job.update(lambda d, e=earlier: d["stage_status"].__setitem__(e, "PASSED"))
    for stage, rel in IN_GRO.items():
        if stage == target:
            path = job.dir / rel.replace("../", "")
            if not path.exists():
                path.write_text("gro", encoding="utf-8")
                record_internal(job.ledger, stage="prev", tool="mdrun", base=job.dir,
                                outputs=[rel.replace("../", "")])
    if target in ("npt", "prod"):
        cpt = {"npt": "03_nvt/nvt.cpt", "prod": "04_npt/npt.cpt"}[target]
        (job.dir / cpt).write_text("cpt", encoding="utf-8")
        record_internal(job.ledger, stage="prev", tool="mdrun", base=job.dir, outputs=[cpt])
    mdp = render_mdp(target, plan, REQUEST, [])
    (job.stage_dir(target) / f"{target}.mdp").write_text(mdp, encoding="utf-8")
    job.update(lambda d: d["mdp_expected_sha256"].__setitem__(target, sha256_text(mdp)))
    return cfg, job, binary


def code_of(fn, *args, **kwargs):
    with pytest.raises(SimAgentError) as exc:
        fn(*args, **kwargs)
    return exc.value


def test_parse_warnings_ids_stable_and_distinct():
    found = parse_warnings(WARNING_1 + WARNING_2)
    assert len(found) == 2 and found[0]["id"] != found[1]["id"]
    assert found[0]["source"] == "topol.top:12"
    assert found[0]["text"] == "first problem continues here"
    assert parse_warnings(WARNING_1)[0]["id"] == found[0]["id"]
    assert len(found[0]["id"]) == 12


def test_no_warnings_single_pass_no_maxwarn(tmp_path):
    cfg, job, _ = make_env(tmp_path)
    runner = Scripted(RunResult(0, "ok", ""))
    out = run_grompp(job, "em", runner, cfg)
    assert len(runner.calls) == 1 and "-maxwarn" not in runner.calls[0]
    assert job.status("em") == "READY"
    assert out["tpr"].endswith("em.tpr") and out["warnings"] == []
    assert (job.stage_dir("em") / "grompp.out").exists()
    assert job.ledger.has_receipt("02_em/em.mdp", sha256_file(job.stage_dir("em") / "em.mdp"))


def test_unacknowledged_warning_fails_without_maxwarn(tmp_path):
    cfg, job, _ = make_env(tmp_path)
    runner = Scripted(RunResult(1, "", WARNING_1))
    err = code_of(run_grompp, job, "em", runner, cfg)
    assert err.code == ErrorCode.E_GROMPP_FAILED
    ids = [w["id"] for w in err.data["warnings"]]
    assert ids == [parse_warnings(WARNING_1)[0]["id"]]
    assert err.data["warnings"][0]["acknowledged"] is False
    assert len(runner.calls) == 1 and all("-maxwarn" not in c for c in runner.calls)
    assert job.status("em") == "PENDING"
    data = job.read()
    assert data["failures"][-1]["stage"] == "em"
    assert [w["id"] for w in data["pending_warnings"]] == ids


def test_acknowledged_warning_second_pass_with_exact_maxwarn(tmp_path):
    cfg, job, _ = make_env(tmp_path)
    wid = parse_warnings(WARNING_1)[0]["id"]
    job.update(lambda d: d["acknowledged_warnings"].append(
        {"id": wid, "text": "x", "time": "t", "note": "n"}))
    runner = Scripted(RunResult(1, "", WARNING_1), RunResult(0, "", WARNING_1))
    out = run_grompp(job, "em", runner, cfg)
    assert len(runner.calls) == 2
    assert "-maxwarn" not in runner.calls[0]
    assert runner.calls[1][-2:] == ["-maxwarn", "1"]
    assert out["warnings"][0]["acknowledged"] is True
    assert job.status("em") == "READY"


def test_partially_acknowledged_warnings_still_fail(tmp_path):
    cfg, job, _ = make_env(tmp_path)
    wid = parse_warnings(WARNING_1)[0]["id"]
    job.update(lambda d: d["acknowledged_warnings"].append({"id": wid}))
    runner = Scripted(RunResult(1, "", WARNING_1 + WARNING_2))
    err = code_of(run_grompp, job, "em", runner, cfg)
    assert err.code == ErrorCode.E_GROMPP_FAILED and len(runner.calls) == 1
    assert [w["acknowledged"] for w in err.data["warnings"]] == [True, False]


def test_failure_without_warnings_is_recorded(tmp_path):
    cfg, job, _ = make_env(tmp_path)
    err = code_of(run_grompp, job, "em", Scripted(RunResult(1, "", "Fatal error:\nbad atomtype")),
                  cfg)
    assert err.code == ErrorCode.E_GROMPP_FAILED and "bad atomtype" in err.message
    assert job.read()["failures"][-1]["stage"] == "em"


def test_modified_mdp_gives_input_modified(tmp_path):
    cfg, job, _ = make_env(tmp_path, "nvt")
    path = job.stage_dir("nvt") / "nvt.mdp"
    path.write_text(path.read_text(encoding="utf-8").replace("ref-t                 = 300.0",
                                                            "ref-t                 = 400.0"),
                    encoding="utf-8")
    runner = Scripted()
    assert code_of(run_grompp, job, "nvt", runner, cfg).code == ErrorCode.E_INPUT_MODIFIED
    assert runner.calls == []


def test_modified_topology_gives_input_modified(tmp_path):
    cfg, job, _ = make_env(tmp_path)
    (job.stage_dir("build") / "topol.top").write_text("tampered", encoding="utf-8")
    assert code_of(run_grompp, job, "em", Scripted(), cfg).code == ErrorCode.E_INPUT_MODIFIED


def test_binary_changed(tmp_path):
    cfg, job, binary = make_env(tmp_path)
    binary.write_bytes(b"\x7fELF-different-and-longer")
    assert code_of(run_grompp, job, "em", Scripted(), cfg).code == ErrorCode.E_BINARY_CHANGED


def test_binary_not_rehashed_when_size_and_mtime_unchanged(tmp_path, monkeypatch):
    cfg, job, binary = make_env(tmp_path)
    calls = []
    real = grompp_module.sha256_file
    monkeypatch.setattr(grompp_module, "sha256_file", lambda p: calls.append(p) or real(p))
    stat = binary.stat()
    # same size and mtime but different bytes: the cheap check trusts the stat record
    binary.write_bytes(b"\x7fELF-ORIGINAL")
    os.utime(binary, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    run_grompp(job, "em", Scripted(), cfg)
    assert not [c for c in calls if Path(c) == binary]


def test_stage_order_enforced(tmp_path):
    cfg, job, _ = make_env(tmp_path)
    job.update(lambda d: d["stage_status"].__setitem__("em", "PENDING"))
    assert code_of(run_grompp, job, "nvt", Scripted(), cfg).code == ErrorCode.E_STAGE_ORDER


def test_stage_state_must_be_pending(tmp_path):
    cfg, job, _ = make_env(tmp_path)
    job.update(lambda d: d["stage_status"].__setitem__("em", "READY"))
    assert code_of(run_grompp, job, "em", Scripted(), cfg).code == ErrorCode.E_STAGE_STATE


def test_running_process_blocks_grompp(tmp_path):
    cfg, job, _ = make_env(tmp_path)
    job.update(lambda d: d["stage_status"].__setitem__("prod", "RUNNING"))
    assert code_of(run_grompp, job, "em", Scripted(), cfg).code == ErrorCode.E_MDRUN_RUNNING


@pytest.mark.parametrize("stage,coords,checkpoint", [
    ("em", "../01_build/water.gro", None), ("nvt", "../02_em/em.gro", None),
    ("npt", "../03_nvt/nvt.gro", "../03_nvt/nvt.cpt"),
    ("prod", "../04_npt/npt.gro", "../04_npt/npt.cpt"),
])
def test_input_coordinates_and_checkpoint_per_stage(tmp_path, stage, coords, checkpoint):
    cfg, job, _ = make_env(tmp_path, stage)
    runner = Scripted()
    run_grompp(job, stage, runner, cfg)
    argv = runner.calls[0]
    assert argv[argv.index("-c") + 1] == coords
    assert argv[argv.index("-p") + 1] == "../01_build/topol.top"
    assert argv[argv.index("-f") + 1] == f"{stage}.mdp"
    assert argv[argv.index("-o") + 1] == f"{stage}.tpr"
    if checkpoint:
        assert argv[argv.index("-t") + 1] == checkpoint
    else:
        assert "-t" not in argv
