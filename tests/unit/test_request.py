import pytest

from simagent.agent.request import (
    ASK_RESEARCHER_FUNCTION,
    SCIENCE_CRITICAL,
    SUBMIT_REQUEST_FUNCTION,
    RequestDraft,
    accept,
    draft_from_request_file,
    finalize,
    merge_submission,
    missing_questions,
    non_interactive_errors,
)

STATED = {"n_molecules": 1000, "water_model": "tip4p", "force_field": "oplsaa",
          "temperature_K": 300.0, "pressure_bar": 1.0, "prod_ns": 1.0}


def empty() -> RequestDraft:
    return RequestDraft(values={}, origins={})


def stated(**over) -> RequestDraft:
    values = {**STATED, **over}
    return RequestDraft(values=values, origins={k: "user" for k in values})


def test_science_critical_order():
    assert SCIENCE_CRITICAL == ("n_molecules", "water_model", "force_field", "temperature_K",
                                "pressure_bar", "prod_ns", "analyses")


def test_missing_questions_orders_by_impact_and_caps_at_three():
    draft = empty()
    first = missing_questions(draft)
    assert [q.field for q in first] == ["n_molecules", "water_model", "force_field"]
    draft = accept(merge_submission(draft, {"water_model": "tip4p", "origins": {
        "water_model": "user"}}), [q for q in first if q.field != "water_model"])
    second = missing_questions(draft)
    assert [q.field for q in second] == ["temperature_K", "pressure_bar", "prod_ns"]
    proposals = {q.field: q.proposal for q in second}
    assert proposals == {"temperature_K": "300", "pressure_bar": "1", "prod_ns": "1"}
    water = missing_questions(RequestDraft({}, {}))[1]
    assert water.proposal == "tip4p"
    assert missing_questions(empty())[2].proposal == "oplsaa"
    assert len(missing_questions(empty(), limit=2)) == 2


def test_stated_fields_are_never_asked_and_inferred_ones_are_confirmed():
    draft = merge_submission(empty(), {
        "n_molecules": 1000, "temperature_K": 310.0, "water_model": "tip4p",
        "origins": {"n_molecules": "user", "temperature_K": "user", "water_model": "inferred"}})
    questions = missing_questions(draft, limit=10)
    fields = [q.field for q in questions]
    assert "n_molecules" not in fields and "temperature_K" not in fields
    confirm = next(q for q in questions if q.field == "water_model")
    assert "tip4p" in confirm.text and confirm.proposal == "tip4p"


def test_an_inferred_value_does_not_overwrite_a_stated_one_but_user_does():
    draft = merge_submission(empty(), {"temperature_K": 300.0,
                                       "origins": {"temperature_K": "user"}})
    kept = merge_submission(draft, {"temperature_K": 350.0,
                                    "origins": {"temperature_K": "inferred"}})
    assert kept.values["temperature_K"] == 300.0 and kept.origins["temperature_K"] == "user"
    changed = merge_submission(draft, {"temperature_K": 350.0,
                                       "origins": {"temperature_K": "user"}})
    assert changed.values["temperature_K"] == 350.0


def test_untagged_values_count_as_inferred():
    draft = merge_submission(empty(), {"n_molecules": 500})
    assert draft.origins["n_molecules"] == "inferred"


def test_yes_makes_a_proposal_origin_user_and_defaults_stay_default():
    draft = accept(empty(), missing_questions(empty()))
    draft = accept(draft, missing_questions(draft))
    draft = accept(draft, missing_questions(draft))
    request = finalize(draft)
    origin = request["provenance_of_fields"]
    assert all(origin[f] == "user" for f in STATED)
    assert request["system"] == {"kind": "water", "n_molecules": 1000, "water_model": "tip4p"}
    assert request["thermo"] == {"temperature_K": 300.0, "pressure_bar": 1.0}
    assert request["protocol"] == {"emtol": 1000.0, "nvt_ps": 100, "npt_ps": 100, "prod_ns": 1.0,
                                   "dt_ps": 0.002, "tau_t": 0.1, "tau_p": 2.0}
    for name in ("emtol", "nvt_ps", "npt_ps", "dt_ps", "tau_t", "tau_p"):
        assert origin[name] == "default"
    assert request["seed"] is None and request["hardware"] == {"ntomp": None, "ntmpi": None}


def test_analyses_baseline_is_always_added():
    request = finalize(stated(analyses=["rdf_OO"]))
    assert request["analyses"] == ["temperature", "pressure", "density", "energy", "rdf_OO"]
    origin = request["provenance_of_fields"]
    assert origin["analyses.rdf_OO"] == "user" and origin["analyses.density"] == "default"


def test_no_analyses_named_asks_once_proposing_rdf_and_msd():
    draft = stated()
    question = next(q for q in missing_questions(draft) if q.field == "analyses")
    assert question.proposal == "rdf_OO, msd"
    done = accept(draft, [question])
    assert done.values["analyses"] == ["rdf_OO", "msd"] and done.origins["analyses"] == "user"
    assert [q.field for q in missing_questions(done)] == []
    assert finalize(stated())["analyses"] == ["temperature", "pressure", "density", "energy"]


def test_finalize_lists_every_missing_critical_field():
    with pytest.raises(ValueError) as exc:
        finalize(merge_submission(empty(), {"n_molecules": 1000,
                                            "origins": {"n_molecules": "user"}}))
    message = str(exc.value)
    for name in ("water_model", "force_field", "temperature_K", "pressure_bar", "prod_ns"):
        assert name in message
    assert "n_molecules" not in message


def test_non_interactive_errors_name_each_field():
    errors = non_interactive_errors(merge_submission(empty(), {
        "n_molecules": 1000, "origins": {"n_molecules": "user"}}))
    assert len(errors) == 5 and all("missing" in e for e in errors)
    assert non_interactive_errors(stated()) == []


def request_file():
    return {"system": {"kind": "water", "n_molecules": 1000, "water_model": "tip4p"},
            "force_field": "oplsaa", "thermo": {"temperature_K": 300.0, "pressure_bar": 1.0},
            "protocol": {"prod_ns": 1.0, "emtol": 500.0}, "analyses": ["rdf_OO"], "seed": 7}


def test_draft_from_request_file_round_trips_through_finalize():
    draft = draft_from_request_file(request_file())
    assert set(draft.origins.values()) == {"user"}
    request = finalize(draft)
    assert request["protocol"]["emtol"] == 500.0 and request["protocol"]["nvt_ps"] == 100
    assert request["seed"] == 7 and request["provenance_of_fields"]["emtol"] == "user"
    assert request["provenance_of_fields"]["nvt_ps"] == "default"


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d.update(bogus=1), "bogus"),
    (lambda d: d["system"].update(n_molecules="1000"), "n_molecules"),
    (lambda d: d["protocol"].update(bogus=1), "protocol.bogus"),
    (lambda d: d["thermo"].update(temperature_K=True), "temperature_K"),
    (lambda d: d.update(analyses=["rdf_OO", "bogus"]), "bogus"),
    (lambda d: d["system"].update(kind="protein"), "kind"),
    (lambda d: d.update(system="water"), "system"),
])
def test_draft_from_request_file_rejects_unknown_keys_and_wrong_types(mutate, needle):
    data = request_file()
    mutate(data)
    with pytest.raises(ValueError) as exc:
        draft_from_request_file(data)
    assert needle in str(exc.value)


def test_function_schemas_are_exactly_the_two_internal_functions():
    assert SUBMIT_REQUEST_FUNCTION["function"]["name"] == "submit_request"
    assert ASK_RESEARCHER_FUNCTION["function"]["name"] == "ask_researcher"
    questions = ASK_RESEARCHER_FUNCTION["function"]["parameters"]["properties"]["questions"]
    assert questions["maxItems"] == 3
    props = SUBMIT_REQUEST_FUNCTION["function"]["parameters"]["properties"]
    assert {"n_molecules", "water_model", "force_field", "temperature_K", "pressure_bar",
            "prod_ns", "analyses", "origins"} <= set(props)


def test_merge_rejects_unknown_fields_and_bad_types():
    with pytest.raises(ValueError, match="bogus"):
        merge_submission(empty(), {"bogus": 1})
    with pytest.raises(ValueError, match="n_molecules"):
        merge_submission(empty(), {"n_molecules": "many"})
    with pytest.raises(ValueError, match="analyses"):
        merge_submission(empty(), {"analyses": ["nonsense"]})
