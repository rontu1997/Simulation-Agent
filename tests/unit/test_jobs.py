import json
import re
import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from simagent.config import load_config
from simagent.core.jobs import STAGE_DIRS, STAGES, JobStore
from simagent.errors import ErrorCode, SimAgentError

REQUEST = {
    "system": {"kind": "water", "n_molecules": 1000, "water_model": "tip4p"},
    "force_field": "oplsaa",
    "thermo": {"temperature_K": 300.0, "pressure_bar": 1.0},
    "protocol": {"prod_ns": 1.0},
    "seed": None,
    "analyses": ["rdf_OO"],
}
ENV = {"sha256": "a" * 64, "execution_mode": "real", "version": "2025.3"}
CLOCK = lambda: datetime(2026, 10, 23, 12, 0, tzinfo=UTC)  # noqa: E731


def make_store(tmp_path: Path) -> JobStore:
    cfg = load_config(env={})
    cfg = replace(cfg, paths=replace(cfg.paths, runs_dir=tmp_path / "runs",
                                     sim_runs_dir=tmp_path / "runs_simulated"))
    return JobStore(cfg, clock=CLOCK)


def test_create_makes_layout_and_job_json(tmp_path):
    job = make_store(tmp_path).create(REQUEST, "real", ENV)
    assert job.job_id == "water-1000-tip4p-300K-20261023-1"
    for stage in STAGES:
        assert (job.dir / STAGE_DIRS[stage]).is_dir()
    data = json.loads((job.dir / "job.json").read_text(encoding="utf-8"))
    for key in ("job_id", "created_at", "request", "plan", "plan_sha256", "stage_status",
                "environment", "execution_mode", "approval", "pending_warnings",
                "acknowledged_warnings", "mdp_expected_sha256", "failures", "retries",
                "report_sha256", "config_snapshot"):
        assert key in data
    assert all(v == "PENDING" for v in data["stage_status"].values())
    assert set(data["stage_status"]) == set(STAGES)
    assert data["environment"]["sha256"] == "a" * 64


def test_job_id_collision_increments_k_and_never_overwrites(tmp_path):
    store = make_store(tmp_path)
    first = store.create(REQUEST, "real", ENV)
    marker = first.dir / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    second = store.create(REQUEST, "real", ENV)
    assert second.job_id.endswith("-2")
    assert marker.read_text(encoding="utf-8") == "keep"


def test_simulated_job_goes_to_sim_runs_dir(tmp_path):
    store = make_store(tmp_path)
    job = store.create(REQUEST, "simulated", {**ENV, "execution_mode": "simulated"})
    assert job.dir.parent == (tmp_path / "runs_simulated")
    assert not (tmp_path / "runs").exists() or not list((tmp_path / "runs").iterdir())


def test_get_unknown_job(tmp_path):
    with pytest.raises(SimAgentError) as exc:
        make_store(tmp_path).get("nope-1")
    assert exc.value.code == ErrorCode.E_JOB_NOT_FOUND


def test_get_finds_simulated_and_real_jobs(tmp_path):
    store = make_store(tmp_path)
    real = store.create(REQUEST, "real", ENV)
    sim = store.create(REQUEST, "simulated", ENV)
    assert store.get(real.job_id).dir == real.dir
    assert store.get(sim.job_id).dir == sim.dir


@pytest.mark.parametrize("stage,old,new", [
    ("em", "PENDING", "READY"), ("em", "READY", "RUNNING"), ("em", "RUNNING", "GATING"),
    ("em", "RUNNING", "FAILED"), ("em", "RUNNING", "CANCELLED"), ("em", "GATING", "PASSED"),
    ("em", "GATING", "FAILED"), ("em", "FAILED", "RUNNING"), ("em", "CANCELLED", "RUNNING"),
    ("build", "PENDING", "RUNNING"), ("analysis", "PENDING", "RUNNING"),
])
def test_legal_transitions(tmp_path, stage, old, new):
    job = make_store(tmp_path).create(REQUEST, "real", ENV)
    job.update(lambda d: d["stage_status"].__setitem__(stage, old))
    job.set_status(stage, new)
    assert job.status(stage) == new


@pytest.mark.parametrize("stage,old,new", [
    ("em", "PENDING", "RUNNING"), ("em", "PASSED", "RUNNING"), ("em", "READY", "PASSED"),
    ("em", "PENDING", "PASSED"), ("nvt", "GATING", "RUNNING"), ("em", "PASSED", "READY"),
])
def test_illegal_transitions(tmp_path, stage, old, new):
    job = make_store(tmp_path).create(REQUEST, "real", ENV)
    job.update(lambda d: d["stage_status"].__setitem__(stage, old))
    with pytest.raises(SimAgentError) as exc:
        job.set_status(stage, new)
    assert exc.value.code == ErrorCode.E_STAGE_STATE
    assert job.status(stage) == old


def test_invalidate_resets_later_stages_and_moves_files(tmp_path):
    job = make_store(tmp_path).create(REQUEST, "real", ENV)
    for stage in STAGES:
        (job.stage_dir(stage) / f"{stage}.out").write_text(stage, encoding="utf-8")
    job.update(lambda d: d["stage_status"].update(
        {"build": "PASSED", "em": "PASSED", "nvt": "PASSED", "npt": "PENDING"}))
    reset = job.invalidate("em")
    assert reset == ["em", "nvt", "npt", "prod", "analysis"]
    assert job.status("build") == "PASSED"
    for stage in reset:
        assert job.status(stage) == "PENDING"
        assert list(job.stage_dir(stage).iterdir()) == []
    moved = job.dir / "superseded" / "1" / STAGE_DIRS["nvt"] / "nvt.out"
    assert moved.read_text(encoding="utf-8") == "nvt"
    assert (job.stage_dir("build") / "build.out").exists()
    (job.stage_dir("npt") / "again.txt").write_text("y", encoding="utf-8")
    job.invalidate("em")  # new files move to the next superseded index
    assert (job.dir / "superseded" / "2" / STAGE_DIRS["npt"] / "again.txt").exists()


def test_invalidate_refused_while_running(tmp_path):
    job = make_store(tmp_path).create(REQUEST, "real", ENV)
    (job.stage_dir("nvt") / "keep.txt").write_text("x", encoding="utf-8")
    job.update(lambda d: d["stage_status"].update({"prod": "RUNNING", "nvt": "PASSED"}))
    with pytest.raises(SimAgentError) as exc:
        job.invalidate("em")
    assert exc.value.code == ErrorCode.E_MDRUN_RUNNING
    assert (job.stage_dir("nvt") / "keep.txt").exists()
    assert job.status("nvt") == "PASSED"


def test_job_status_derivation(tmp_path):
    job = make_store(tmp_path).create(REQUEST, "real", ENV)
    assert job.job_status() == "planned"

    def set_all(**kw):
        job.update(lambda d: d["stage_status"].update(kw))

    set_all(build="PASSED", em="PASSED", nvt="PASSED", npt="PASSED", prod="PASSED",
            analysis="PASSED")
    assert job.job_status() == "passed"
    set_all(analysis="CANCELLED")
    assert job.job_status() == "cancelled"
    set_all(prod="FAILED")
    assert job.job_status() == "failed"
    set_all(npt="GATING")
    assert job.job_status() == "running"


def test_concurrent_updates_do_not_lose_writes(tmp_path):
    job = make_store(tmp_path).create(REQUEST, "real", ENV)
    job.update(lambda d: d.update(counter=0))

    def worker():
        for _ in range(25):
            job.update(lambda d: d.update(counter=d["counter"] + 1))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert job.read()["counter"] == 200


def test_list_jobs_filters_and_skips_internal_dirs(tmp_path):
    store = make_store(tmp_path)
    a = store.create(REQUEST, "real", ENV)
    store.create(REQUEST, "real", ENV)
    (tmp_path / "runs" / "_probe").mkdir()
    a.update(lambda d: d["stage_status"].update({"em": "RUNNING"}))
    rows = store.list()
    assert len(rows) == 2
    assert {"job_id", "created", "title", "status", "execution_mode", "path"} <= set(rows[0])
    assert [r["job_id"] for r in store.list(status="running")] == [a.job_id]
    assert len(store.list(limit=1)) == 1
    assert "tip4p" in rows[0]["title"] and "300" in rows[0]["title"]


def test_job_dir_path_inside_runs(tmp_path):
    job = make_store(tmp_path).create(REQUEST, "real", ENV)
    assert re.match(r"^water-1000-tip4p-300K-\d{8}-1$", job.job_id)
