import copy

import pytest

from simagent.core.mdp import (
    EXAMPLE_JOB_ID,
    build_default_plan,
    check_retry,
    derive_seed,
    render_mdp,
    rescale_for_dt,
    validate_plan,
    validate_request,
)
from simagent.core.paths import sha256_text
from simagent.errors import ErrorCode, SimAgentError

SEED = 1904784231
REQUEST = {
    "system": {"kind": "water", "n_molecules": 1000, "water_model": "tip4p"},
    "force_field": "oplsaa",
    "thermo": {"temperature_K": 300.0, "pressure_bar": 1.0},
    "protocol": {"emtol": 1000.0, "nvt_ps": 100, "npt_ps": 100, "prod_ns": 1.0, "dt_ps": 0.002,
                 "tau_t": 0.1, "tau_p": 2.0},
    "seed": SEED,
    "analyses": ["rdf_OO", "density"],
}
BUILD = {"box_nm": 3.107, "build_sha256": "b" * 64, "target_density_g_cm3": 0.997,
         "n_molecules": 1000, "water_model": "tip4p"}


def plan():
    return build_default_plan(copy.deepcopy(REQUEST), BUILD)


def code_of(fn, *args, **kwargs):
    with pytest.raises(SimAgentError) as exc:
        fn(*args, **kwargs)
    return exc.value.code


def test_seed_derivation_matches_design_example():
    assert derive_seed(EXAMPLE_JOB_ID) == SEED
    assert EXAMPLE_JOB_ID == "water-1000-tip4p-300K-20261023-1"


def test_seed_differs_between_jobs():
    assert derive_seed("water-1000-tip4p-300K-20261023-2") != SEED


def test_render_prod_contains_spec_values():
    text = render_mdp("prod", plan(), REQUEST)
    for line in ("integrator            = md", "nsteps                = 500000",
                 "dt                    = 0.002", "pcoupl                = C-rescale",
                 "tau-p                 = 2.0", "ref-t                 = 300.0",
                 "nstxout-compressed    = 250", "continuation          = yes"):
        assert line in text


def test_render_em_values():
    text = render_mdp("em", plan(), REQUEST)
    assert "integrator            = steep" in text
    assert "emtol                 = 1000.0" in text
    assert "nstxout-compressed    = 0" in text
    assert "nsteps                = 50000" in text
    assert "tcoupl" not in text


def test_render_nvt_gen_vel_and_seed():
    text = render_mdp("nvt", plan(), REQUEST)
    assert "gen-vel               = yes" in text
    assert f"gen-seed              = {SEED}" in text
    assert "pcoupl                = no" in text
    assert "tau-t                 = 0.1" in text


def test_render_is_deterministic():
    a, b = render_mdp("npt", plan(), REQUEST), render_mdp("npt", plan(), REQUEST)
    assert a == b and sha256_text(a) == sha256_text(b)


def test_render_dry_is_plan_independent():
    other = copy.deepcopy(REQUEST)
    other["thermo"]["temperature_K"] = 350.0
    assert render_mdp("dry", plan(), REQUEST) == render_mdp(
        "dry", build_default_plan(other, BUILD), other)
    assert "nsteps                = 1" in render_mdp("dry", plan(), REQUEST)


@pytest.mark.parametrize("path,value,ok", [
    (("thermo", "temperature_K"), 199, False), (("thermo", "temperature_K"), 200, True),
    (("thermo", "temperature_K"), 450, True), (("thermo", "temperature_K"), 451, False),
    (("thermo", "pressure_bar"), 0.09, False), (("thermo", "pressure_bar"), 0.1, True),
    (("thermo", "pressure_bar"), 5001, False),
    (("protocol", "dt_ps"), 0.0021, False), (("protocol", "dt_ps"), 0.002, True),
    (("protocol", "dt_ps"), 0.0005, True), (("protocol", "dt_ps"), 0.0004, False),
    (("system", "n_molecules"), 308, False), (("system", "n_molecules"), 309, True),
    (("system", "n_molecules"), 20000, True), (("system", "n_molecules"), 20001, False),
    (("protocol", "prod_ns"), 0.009, False), (("protocol", "prod_ns"), 0.01, True),
    (("protocol", "prod_ns"), 101, False),
    (("protocol", "tau_t"), 0.04, False), (("protocol", "tau_p"), 0.9, False),
    (("protocol", "emtol"), 9, False), (("protocol", "emtol"), 5001, False),
    (("protocol", "nvt_ps"), 9, False), (("protocol", "npt_ps"), 10001, False),
    (("seed",), -1, False), (("seed",), 0, True),
])
def test_range_boundaries(path, value, ok):
    request = copy.deepcopy(REQUEST)
    target = request
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    if ok:
        validate_request(request)
    else:
        assert code_of(validate_request, request) == ErrorCode.E_PARAM_OUT_OF_RANGE


def test_model_and_force_field_combinations():
    request = copy.deepcopy(REQUEST)
    request["system"]["water_model"] = "spc"
    assert code_of(validate_request, request) == ErrorCode.E_PARAM_OUT_OF_RANGE
    request["system"]["water_model"] = "tip4pew"
    assert code_of(validate_request, request) == ErrorCode.E_PARAM_OUT_OF_RANGE
    request["force_field"] = "amber99sb-ildn"
    validate_request(request)


def test_warning_bands_do_not_reject():
    request = copy.deepcopy(REQUEST)
    request["thermo"]["temperature_K"] = 400.0
    warnings = validate_request(request)
    assert any("temperature" in w for w in warnings)


def test_missing_optional_protocol_fields_use_defaults():
    minimal = {"system": REQUEST["system"], "force_field": "oplsaa",
               "thermo": REQUEST["thermo"], "protocol": {"prod_ns": 1.0}, "seed": None}
    validate_request(minimal)


def test_default_plan_shape():
    p = plan()
    assert [s["id"] for s in p["stages"]] == ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]
    assert [s["name"] for s in p["stages"]] == [
        "build", "em", "nvt", "npt", "prod", "analysis", "report"]
    by = {s["name"]: s["params"] for s in p["stages"]}
    assert by["build"]["build_sha256"] == "b" * 64 and by["build"]["box_nm"] == 3.107
    assert by["em"] == {"emtol": 1000.0, "nsteps": 50000}
    assert by["nvt"]["nsteps"] == 50000 and by["nvt"]["seed"] == SEED
    assert by["npt"]["nsteps"] == 50000 and by["npt"]["tau_p"] == 2.0
    assert by["prod"]["nsteps"] == 500000
    assert set(by["analysis"]["analyses"]) >= {"temperature", "pressure", "density", "energy",
                                                "rdf_OO"}
    assert p["automatic_retries_allowed"] == [
        {"key": "emtol", "min": 100, "max": 1000.0}, {"key": "dt", "allowed": [0.001]},
        {"key": "tau_p", "allowed": [5.0]}, {"key": "em_nsteps", "allowed": [100000, 200000]}]
    validate_plan(p, REQUEST)


def test_validate_plan_rejects_inconsistent_values():
    p = plan()
    p["stages"][2]["params"]["ref_t"] = 400.0  # differs from the request
    assert code_of(validate_plan, p, REQUEST) == ErrorCode.E_PARAM_OUT_OF_RANGE
    p = plan()
    p["stages"][4]["params"]["dt"] = 0.003
    assert code_of(validate_plan, p, REQUEST) == ErrorCode.E_PARAM_OUT_OF_RANGE
    p = plan()
    p["automatic_retries_allowed"][1]["allowed"] = [0.0015]  # 0.002 / 0.0015 is not an integer
    assert code_of(validate_plan, p, REQUEST) == ErrorCode.E_PARAM_OUT_OF_RANGE


def test_dt_retry_rescales_steps_and_output_intervals():
    params = {"nsteps": 500000, "dt": 0.002}
    scaled = rescale_for_dt(params, 0.001)
    assert scaled["dt"] == 0.001 and scaled["nsteps"] == 1000000
    retries = [{"stage": "prod", "key": "dt", "value": 0.001, "failing_stage": "prod"}]
    text = render_mdp("prod", plan(), REQUEST, retries)
    assert "dt                    = 0.001" in text
    assert "nsteps                = 1000000" in text
    assert "nstxout-compressed    = 500" in text
    # a dt retry on nvt also applies to the later stages
    retries = [{"stage": "nvt", "key": "dt", "value": 0.001, "failing_stage": "nvt"}]
    assert "dt                    = 0.001" in render_mdp("prod", plan(), REQUEST, retries)
    assert render_mdp("em", plan(), REQUEST, retries) == render_mdp("em", plan(), REQUEST)
    assert "dt                    = 0.002" in render_mdp("nvt", plan(), REQUEST, [
        {"stage": "npt", "key": "dt", "value": 0.001, "failing_stage": "npt"}])


def test_emtol_and_em_nsteps_retries_apply_to_em_only():
    retries = [{"stage": "em", "key": "emtol", "value": 100.0, "failing_stage": "nvt"},
               {"stage": "em", "key": "em_nsteps", "value": 100000, "failing_stage": "nvt"}]
    em = render_mdp("em", plan(), REQUEST, retries)
    assert "emtol                 = 100.0" in em and "nsteps                = 100000" in em
    assert "nsteps                = 50000" in render_mdp("nvt", plan(), REQUEST, retries)


FAILURE = {"stage": "nvt", "cause": "settle"}


def state(failures=(), retries=()):
    return {"failures": list(failures), "retries": list(retries)}


def test_retry_bounds():
    p = plan()
    ok_state = state([FAILURE])
    assert check_retry(p, ok_state, "emtol", 100.0, None, target_stage="em") == "nvt"
    assert code_of(check_retry, p, ok_state, "emtol", 5000.0, None) == \
        ErrorCode.E_RETRY_NOT_ALLOWED  # looser than the plan's max
    assert code_of(check_retry, p, ok_state, "emtol", 50.0, None) == \
        ErrorCode.E_RETRY_NOT_ALLOWED  # below the plan's min
    assert code_of(check_retry, p, ok_state, "dt", 0.0015, None) == ErrorCode.E_RETRY_NOT_ALLOWED
    assert code_of(check_retry, p, ok_state, "seed", 1, None) == ErrorCode.E_RETRY_NOT_ALLOWED
    assert code_of(check_retry, p, state(), "emtol", 100.0, None) == \
        ErrorCode.E_RETRY_NOT_ALLOWED  # no failure recorded
    assert code_of(check_retry, p, ok_state, "emtol", 100.0, None, target_stage="prod") == \
        ErrorCode.E_RETRY_NOT_ALLOWED  # a retry may not target a stage after the failing one


def test_retry_limit_and_attribution():
    p = plan()
    used = [{"stage": "em", "key": "emtol", "value": 100.0, "failing_stage": "nvt"},
            {"stage": "nvt", "key": "dt", "value": 0.001, "failing_stage": "nvt"}]
    assert code_of(check_retry, p, state([FAILURE], used), "tau_p", 5.0, "nvt") == \
        ErrorCode.E_RETRY_LIMIT
    other = {"stage": "npt", "cause": "pressure"}
    # the most recent failure with retries left is chosen when failing_stage is not given
    assert check_retry(p, state([FAILURE, other], used), "tau_p", 5.0, None) == "npt"
    # naming a stage without a recorded failure is not allowed
    assert code_of(check_retry, p, state([FAILURE]), "tau_p", 5.0, "prod") == \
        ErrorCode.E_RETRY_NOT_ALLOWED
