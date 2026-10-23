import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from test_build import StubRunner

from simagent.config import load_config
from simagent.core.approval import (
    plan_sha256,
    record_approval,
    require_run_approval,
    set_plan,
)
from simagent.core.build import build_water_box
from simagent.core.jobs import JobStore
from simagent.core.mdp import build_default_plan
from simagent.errors import ErrorCode, SimAgentError

TOKEN = "tok-secret-123456"
REQUEST = {
    "system": {"kind": "water", "n_molecules": 1000, "water_model": "tip4p"},
    "force_field": "oplsaa",
    "thermo": {"temperature_K": 300.0, "pressure_bar": 1.0},
    "protocol": {"emtol": 1000.0, "nvt_ps": 100, "npt_ps": 100, "prod_ns": 1.0, "dt_ps": 0.002,
                 "tau_t": 0.1, "tau_p": 2.0},
    "seed": 1904784231,
    "analyses": ["rdf_OO", "density"],
}


def make_cfg(tmp_path, env=None):
    cfg = load_config(env={"SIMAGENT_APPROVAL_TOKEN": TOKEN} if env is None else env)
    return replace(cfg, paths=replace(cfg.paths, runs_dir=tmp_path / "runs",
                                      sim_runs_dir=tmp_path / "sim"))


def make_built(tmp_path, env=None):
    cfg = make_cfg(tmp_path, env)
    job = JobStore(cfg, clock=lambda: datetime(2026, 10, 23, tzinfo=UTC)).create(
        REQUEST, "real", {"sha256": "a" * 64})
    info = build_water_box(job, StubRunner(), n_molecules=1000, water_model="tip4p")
    return cfg, job, info


def make_planned(tmp_path, env=None):
    cfg, job, info = make_built(tmp_path, env)
    plan = build_default_plan(copy.deepcopy(REQUEST), info)
    result = set_plan(job, cfg, copy.deepcopy(REQUEST), plan)
    return cfg, job, plan, result["plan_sha256"]


def code_of(fn, *args, **kwargs):
    with pytest.raises(SimAgentError) as exc:
        fn(*args, **kwargs)
    return exc.value.code


def test_plan_hash_ignores_why_and_estimates():
    plan = build_default_plan(copy.deepcopy(REQUEST), {"box_nm": 3.1, "build_sha256": "b" * 64})
    base = plan_sha256(REQUEST, plan)
    other = copy.deepcopy(plan)
    for stage in other["stages"]:
        stage["why"] = "reworded"
    other["estimates"] = {"wall_hours": 9, "disk_mb": 1}
    assert plan_sha256(REQUEST, other) == base
    assert len(base) == 64


def test_plan_hash_changes_with_any_parameter():
    plan = build_default_plan(copy.deepcopy(REQUEST), {"box_nm": 3.1, "build_sha256": "b" * 64})
    base = plan_sha256(REQUEST, plan)
    for mutate in (
        lambda p: p["stages"][2]["params"].update(tau_t=0.2),
        lambda p: p["stages"][4]["params"].update(nsteps=400000),
        lambda p: p["stages"][0]["params"].update(build_sha256="c" * 64),
        lambda p: p["automatic_retries_allowed"].append({"key": "x"}),
    ):
        changed = copy.deepcopy(plan)
        mutate(changed)
        assert plan_sha256(REQUEST, changed) != base
    other_request = copy.deepcopy(REQUEST)
    other_request["thermo"]["temperature_K"] = 310.0
    assert plan_sha256(other_request, plan) != base


def test_plan_hash_is_canonical_json_sha256():
    plan = build_default_plan(copy.deepcopy(REQUEST), {"box_nm": 3.1, "build_sha256": "b" * 64})
    payload = {
        "request": REQUEST, "analyses": REQUEST["analyses"],
        "stages": [{"id": s["id"], "name": s["name"], "params": s["params"]}
                   for s in plan["stages"]],
        "automatic_retries_allowed": plan["automatic_retries_allowed"],
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert plan_sha256(REQUEST, plan) == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_set_plan_validates_and_stores(tmp_path):
    cfg, job, info = make_built(tmp_path)
    bad_request = copy.deepcopy(REQUEST)
    bad_request["thermo"]["temperature_K"] = 199
    plan = build_default_plan(copy.deepcopy(REQUEST), info)
    assert code_of(set_plan, job, cfg, bad_request, plan) == ErrorCode.E_PARAM_OUT_OF_RANGE
    assert job.read()["plan"] is None
    new_request = copy.deepcopy(REQUEST)
    new_request["analyses"] = ["msd"]
    plan2 = build_default_plan(copy.deepcopy(new_request), info)
    result = set_plan(job, cfg, new_request, plan2)
    data = job.read()
    assert data["plan_sha256"] == result["plan_sha256"] == plan_sha256(new_request, plan2)
    assert data["request"]["analyses"] == ["msd"]  # replaces the request stored by create_job
    assert job.status("build") == "PASSED"


def test_set_plan_build_count_mismatch_is_out_of_range(tmp_path):
    cfg, job, info = make_built(tmp_path)
    build_json = job.stage_dir("build") / "build.json"
    data = json.loads(build_json.read_text(encoding="utf-8"))
    data["n_molecules"] = 900
    build_json.write_text(json.dumps(data), encoding="utf-8")
    plan = build_default_plan(copy.deepcopy(REQUEST), info)
    assert code_of(set_plan, job, cfg, copy.deepcopy(REQUEST), plan) == \
        ErrorCode.E_PARAM_OUT_OF_RANGE


def test_set_plan_build_hash_mismatch_fails_the_gate(tmp_path):
    cfg, job, info = make_built(tmp_path)
    (job.stage_dir("build") / "topol.top").write_text("tampered SOL  1000\n", encoding="utf-8")
    plan = build_default_plan(copy.deepcopy(REQUEST), info)
    assert code_of(set_plan, job, cfg, copy.deepcopy(REQUEST), plan) == ErrorCode.E_GATE_FAILED
    assert job.read()["plan"] is None


def test_set_plan_before_a_build_exists(tmp_path):
    cfg = make_cfg(tmp_path)
    job = JobStore(cfg).create(REQUEST, "real", {"sha256": "a" * 64})
    plan = build_default_plan(copy.deepcopy(REQUEST), {"box_nm": 3.1, "build_sha256": "b" * 64})
    assert code_of(set_plan, job, cfg, copy.deepcopy(REQUEST), plan) == ErrorCode.E_STAGE_ORDER


def test_set_plan_refused_while_running_leaves_statuses_unchanged(tmp_path):
    cfg, job, plan, _ = make_planned(tmp_path)
    job.update(lambda d: d["stage_status"].update({"nvt": "RUNNING", "em": "PASSED"}))
    before = job.read()["stage_status"]
    changed = copy.deepcopy(plan)
    changed["stages"][2]["params"]["tau_t"] = 0.2
    assert code_of(set_plan, job, cfg, copy.deepcopy(REQUEST), changed) == \
        ErrorCode.E_MDRUN_RUNNING
    assert job.read()["stage_status"] == before


def test_set_plan_clears_approval_and_invalidates_changed_stages(tmp_path):
    cfg, job, plan, digest = make_planned(tmp_path)
    record_approval(job, cfg, token=TOKEN, kind="plan", plan_sha256=digest)
    job.update(lambda d: (d["stage_status"].update({"em": "PASSED", "nvt": "PASSED"}),
                          d["acknowledged_warnings"].append({"id": "x"})))
    (job.stage_dir("nvt") / "nvt.out").write_text("x", encoding="utf-8")
    changed = copy.deepcopy(plan)
    changed["stages"][2]["params"]["tau_t"] = 0.2  # nvt
    result = set_plan(job, cfg, copy.deepcopy(REQUEST), changed)
    data = job.read()
    assert data["approval"] is None and data["acknowledged_warnings"] == []
    assert data["stage_status"]["em"] == "PASSED"
    assert all(data["stage_status"][s] == "PENDING" for s in ("nvt", "npt", "prod", "analysis"))
    assert result["invalidated"] == ["nvt", "npt", "prod", "analysis"]
    assert result["plan_sha256"] != digest
    assert not (job.stage_dir("nvt") / "nvt.out").exists()


def test_resubmitting_an_identical_plan_invalidates_nothing(tmp_path):
    cfg, job, plan, digest = make_planned(tmp_path)
    job.update(lambda d: d["stage_status"].update({"em": "PASSED"}))
    result = set_plan(job, cfg, copy.deepcopy(REQUEST), copy.deepcopy(plan))
    assert result["plan_sha256"] == digest and result["invalidated"] == []
    assert job.status("em") == "PASSED"


def test_record_approval_checks(tmp_path):
    cfg, job, plan, digest = make_planned(tmp_path)
    kw = dict(kind="plan", plan_sha256=digest)
    assert code_of(record_approval, job, cfg, token="nope", **kw) == ErrorCode.E_BAD_TOKEN
    assert code_of(record_approval, job, cfg, token=TOKEN, kind="plan",
                   plan_sha256="0" * 64) == ErrorCode.E_PLAN_MISMATCH
    assert code_of(record_approval, job, make_cfg(tmp_path, {}), token=TOKEN, **kw) == \
        ErrorCode.E_APPROVAL_UNAVAILABLE
    assert code_of(record_approval, job, cfg, token=TOKEN, kind="warnings",
                   warning_ids=["unknown-id"]) == ErrorCode.E_PARAM_OUT_OF_RANGE
    out = record_approval(job, cfg, token=TOKEN, mode="interactive", long_run_confirmed=True, **kw)
    approval = job.read()["approval"]
    assert out["approved_at"] and approval["plan_sha256"] == digest
    assert approval["mode"] == "interactive" and approval["long_run_confirmed"] is True


def test_record_approval_for_warnings_stores_ack_and_ledger_record(tmp_path):
    cfg, job, plan, digest = make_planned(tmp_path)
    job.update(lambda d: d["pending_warnings"].append(
        {"id": "w1", "text": "careful", "source": "topol.top:3"}))
    record_approval(job, cfg, token=TOKEN, kind="warnings", warning_ids=["w1"], note="checked")
    ack = job.read()["acknowledged_warnings"]
    assert [a["id"] for a in ack] == ["w1"] and ack[0]["note"] == "checked"
    assert ack[0]["text"] == "careful"
    assert any(r.get("event") == "ack" and r["warning_id"] == "w1" for r in job.ledger.records())


def test_require_run_approval_matrix(tmp_path):
    cfg, job, plan, digest = make_planned(tmp_path)
    assert code_of(require_run_approval, job, cfg, 1.0) == ErrorCode.E_NOT_APPROVED
    record_approval(job, cfg, token=TOKEN, kind="plan", plan_sha256=digest)
    require_run_approval(job, cfg, 1.0)
    assert code_of(require_run_approval, job, cfg, 12.0) == ErrorCode.E_NOT_APPROVED
    record_approval(job, cfg, token=TOKEN, kind="plan", plan_sha256=digest,
                    long_run_confirmed=True)
    require_run_approval(job, cfg, 12.0)
    job.update(lambda d: d["approval"].update(plan_sha256="0" * 64))  # stale approval
    assert code_of(require_run_approval, job, cfg, 1.0) == ErrorCode.E_NOT_APPROVED


def test_server_without_token_is_unavailable(tmp_path):
    cfg, job, plan, digest = make_planned(tmp_path, env={})
    assert code_of(require_run_approval, job, cfg, 1.0) == ErrorCode.E_APPROVAL_UNAVAILABLE


def test_operator_override(tmp_path):
    cfg, job, plan, digest = make_planned(tmp_path, env={"SIMAGENT_ALLOW_UNAPPROVED": "1"})
    require_run_approval(job, cfg, 1.0)
    approval = job.read()["approval"]
    assert approval["mode"] == "operator_override" and approval["plan_sha256"] == digest
    assert approval["long_run_confirmed"] is False
    assert code_of(require_run_approval, job, cfg, 12.0) == ErrorCode.E_NOT_APPROVED
    # warning acknowledgement stays closed without the token
    assert code_of(record_approval, job, cfg, token="x", kind="warnings",
                   warning_ids=["w"]) == ErrorCode.E_APPROVAL_UNAVAILABLE


def test_operator_override_needs_a_stored_plan(tmp_path):
    cfg, job, info = make_built(tmp_path, {"SIMAGENT_ALLOW_UNAPPROVED": "1"})
    assert code_of(require_run_approval, job, cfg, 1.0) == ErrorCode.E_STAGE_ORDER
