from dataclasses import replace
from datetime import UTC, datetime

import pytest

from simagent.config import load_config
from simagent.core.gates import GateCheck, apply_validation
from simagent.core.jobs import JobStore
from simagent.errors import ErrorCode, SimAgentError

REQUEST = {
    "system": {"kind": "water", "n_molecules": 1000, "water_model": "tip4p"},
    "thermo": {"temperature_K": 300.0, "pressure_bar": 1.0}, "protocol": {"prod_ns": 1.0},
}


def make_job(tmp_path):
    cfg = load_config(env={})
    cfg = replace(cfg, paths=replace(cfg.paths, runs_dir=tmp_path / "runs"))
    return JobStore(cfg, clock=lambda: datetime(2026, 10, 23, tzinfo=UTC)).create(
        REQUEST, "real", {"sha256": "x"})


def good():
    return [GateCheck("process_state", "pass"), GateCheck("mean_temperature", "pass")]


def bad():
    return [GateCheck("process_state", "pass"), GateCheck("mean_temperature", "fail", 303.0, 2.0)]


def test_running_stage_whose_process_died_goes_failed_without_physics(tmp_path):
    job = make_job(tmp_path)
    job.update(lambda d: d["stage_status"].__setitem__("nvt", "RUNNING"))
    out = apply_validation(job, "nvt", process_state="died", gate_fn=lambda: good())
    assert out["passed"] is False
    assert out["checks"][0]["name"] == "process_died" and out["checks"][0]["status"] == "fail"
    assert job.status("nvt") == "FAILED"
    assert job.read()["failures"][-1]["stage"] == "nvt"


def test_finished_process_is_gated_and_passes(tmp_path):
    job = make_job(tmp_path)
    job.update(lambda d: d["stage_status"].__setitem__("em", "RUNNING"))
    out = apply_validation(job, "em", process_state="finished", gate_fn=good)
    assert out["passed"] is True and job.status("em") == "PASSED"


def test_finished_process_failing_its_gate_goes_failed(tmp_path):
    job = make_job(tmp_path)
    job.update(lambda d: d["stage_status"].__setitem__("em", "RUNNING"))
    out = apply_validation(job, "em", process_state="finished", gate_fn=bad)
    assert out["passed"] is False and job.status("em") == "FAILED"
    assert job.read()["failures"][-1]["stage"] == "em"


def test_still_running_process_changes_nothing(tmp_path):
    job = make_job(tmp_path)
    job.update(lambda d: d["stage_status"].__setitem__("em", "RUNNING"))
    out = apply_validation(job, "em", process_state="running", gate_fn=good)
    assert out["passed"] is False and job.status("em") == "RUNNING"
    assert out["checks"][0]["status"] == "not_run"


def test_stage_that_never_ran_reports_not_run(tmp_path):
    out = apply_validation(make_job(tmp_path), "npt", process_state=None, gate_fn=good)
    assert out["passed"] is False and out["checks"][0]["status"] == "not_run"


def test_cancelled_stage_raises_E_CANCELLED(tmp_path):
    job = make_job(tmp_path)
    job.update(lambda d: d["stage_status"].__setitem__("prod", "CANCELLED"))
    with pytest.raises(SimAgentError) as exc:
        apply_validation(job, "prod", process_state="cancelled", gate_fn=good)
    assert exc.value.code == ErrorCode.E_CANCELLED
    assert job.status("prod") == "CANCELLED"


def test_build_stage_gating_goes_passed_or_failed(tmp_path):
    job = make_job(tmp_path)
    job.update(lambda d: d["stage_status"].__setitem__("build", "GATING"))
    assert apply_validation(job, "build", process_state=None, gate_fn=good)["passed"]
    assert job.status("build") == "PASSED"
    job2 = make_job(tmp_path)
    job2.update(lambda d: d["stage_status"].__setitem__("build", "GATING"))
    apply_validation(job2, "build", process_state=None, gate_fn=bad)
    assert job2.status("build") == "FAILED"


def test_passed_stage_is_reevaluated_without_changing_status(tmp_path):
    job = make_job(tmp_path)
    job.update(lambda d: d["stage_status"].__setitem__("em", "PASSED"))
    out = apply_validation(job, "em", process_state="finished", gate_fn=bad)
    assert out["passed"] is False and job.status("em") == "PASSED"


def test_warnings_listed(tmp_path):
    job = make_job(tmp_path)
    job.update(lambda d: d["stage_status"].__setitem__("em", "RUNNING"))
    out = apply_validation(job, "em", process_state="finished", gate_fn=lambda: [
        GateCheck("a", "pass"), GateCheck("epot_range", "warn", -50.0, (-48, -34))])
    assert out["passed"] is True and out["warnings"] == ["epot_range"]
