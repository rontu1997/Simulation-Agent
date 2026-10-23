"""The request draft and the clarification policy (design 3.1 and 9.2).

Pure code: no model, no tools. The model only fills a draft through ``submit_request``; this
module decides what is still missing, what to ask, and what a finished request looks like.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

SCIENCE_CRITICAL = ("n_molecules", "water_model", "force_field", "temperature_K", "pressure_bar",
                    "prod_ns", "analyses")
_SCALARS = SCIENCE_CRITICAL[:-1]
TECHNICAL = ("emtol", "nvt_ps", "npt_ps", "dt_ps", "tau_t", "tau_p")
ALLOWED_FIELDS = (*SCIENCE_CRITICAL, *TECHNICAL, "seed")
ANALYSIS_NAMES = ("temperature", "pressure", "density", "energy", "rdf_OO", "msd")
BASELINE = ANALYSIS_NAMES[:4]
DEFAULT_ANALYSES = ("rdf_OO", "msd")
TECHNICAL_DEFAULTS: dict[str, float] = {
    "emtol": 1000.0, "nvt_ps": 100, "npt_ps": 100, "dt_ps": 0.002, "tau_t": 0.1, "tau_p": 2.0}
_INTS = ("n_molecules", "seed")
_STRINGS = ("water_model", "force_field")
_ORIGINS = ("user", "inferred", "default", "memory")


@dataclass
class Question:
    field: str
    text: str
    proposal: str


@dataclass
class RequestDraft:
    values: dict[str, Any] = field(default_factory=dict)
    origins: dict[str, str] = field(default_factory=dict)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _coerce(name: str, value: Any, path: str | None = None) -> Any:
    """Check the type of one field and normalise numbers; ``ValueError`` names the field."""
    label = path or name
    if name == "analyses":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{label} must be a list of analysis names")
        bad = [v for v in value if v not in ANALYSIS_NAMES]
        if bad:
            raise ValueError(f"{label}: unknown analysis {bad[0]} "
                             f"(allowed: {', '.join(ANALYSIS_NAMES)})")
        return list(dict.fromkeys(value))
    if name in _INTS:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{label} must be a whole number")
        return value
    if name in _STRINGS:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be text")
        return value.strip().lower() if name == "water_model" else value.strip()
    if not _is_number(value):
        raise ValueError(f"{label} must be a number")
    return value if name in ("nvt_ps", "npt_ps") else float(value)


def _parse_text(name: str, text: str) -> Any:
    """Turn a proposal or an answer into a typed value."""
    text = text.strip()
    if name == "analyses":
        return _coerce(name, [t.strip() for t in text.split(",") if t.strip()])
    if name in _STRINGS:
        return _coerce(name, text)
    try:
        number: Any = int(text) if name in _INTS else float(text)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    return _coerce(name, number)


def merge_submission(draft: RequestDraft, submitted: dict[str, Any]) -> RequestDraft:
    """Fold a ``submit_request`` call into the draft. Untagged values count as inferred, and an
    inferred value never replaces something the researcher stated."""
    values, origins = dict(draft.values), dict(draft.origins)
    tags = submitted.get("origins") or {}
    if not isinstance(tags, dict):
        raise ValueError("origins must be an object")
    for name, raw in submitted.items():
        if name == "origins":
            continue
        if name not in ALLOWED_FIELDS:
            raise ValueError(f"unknown field: {name}")
        value = _coerce(name, raw)
        origin = tags.get(name, "inferred")
        if origin not in ("user", "inferred"):
            raise ValueError(f"origins.{name} must be user or inferred")
        if origin == "inferred" and origins.get(name) == "user":
            continue
        values[name], origins[name] = value, origin
    return RequestDraft(values, origins)


_QUESTIONS: dict[str, tuple[str, str]] = {
    "n_molecules": ("How many water molecules should the box hold?", "1000"),
    "water_model": ("Which water model? TIP4P (the original, shipped with OPLS-AA) is "
                    "proposed; TIP4P/2005 depends on the topology files installed.", "tip4p"),
    "force_field": ("Which force field?", "oplsaa"),
    "temperature_K": ("What temperature (K)?", "300"),
    "pressure_bar": ("What pressure (bar)?", "1"),
    "prod_ns": ("How long should the production run be (ns)?", "1"),
    "analyses": ("Which analyses do you want besides temperature, pressure, density, and "
                 "energy (always included)?", ", ".join(DEFAULT_ANALYSES)),
}


def _show(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def missing_questions(draft: RequestDraft, limit: int = 3) -> list[Question]:
    """What to ask next: absent science-critical fields and unconfirmed inferred ones, in order
    of impact. Anything the researcher stated is never asked again."""
    questions: list[Question] = []
    for name in SCIENCE_CRITICAL:
        if len(questions) >= limit:
            break
        text, proposal = _QUESTIONS[name]
        origin = draft.origins.get(name)
        if name not in draft.values:
            questions.append(Question(name, text, proposal))
        elif origin in ("inferred", "memory"):
            shown = _show(draft.values[name])
            questions.append(Question(
                name, f"I took {name} = {shown} from your description. Is that right?", shown))
    return questions


def accept(draft: RequestDraft, questions: Sequence[Question]) -> RequestDraft:
    """The researcher said yes: each proposal becomes the value, origin ``user``."""
    values, origins = dict(draft.values), dict(draft.origins)
    for question in questions:
        values[question.field] = _parse_text(question.field, question.proposal)
        origins[question.field] = "user"
    return RequestDraft(values, origins)


def _missing(draft: RequestDraft) -> list[str]:
    return [name for name in _SCALARS if name not in draft.values]


def non_interactive_errors(draft: RequestDraft) -> list[str]:
    return [f"missing science-critical field: {name}" for name in _missing(draft)]


def finalize(draft: RequestDraft) -> dict[str, Any]:
    """The nested ``SimulationRequest`` of design 3.1, with an origin for every field."""
    missing = _missing(draft)
    if missing:
        raise ValueError("missing science-critical fields: " + ", ".join(missing))
    v, origin = draft.values, draft.origins
    protocol: dict[str, Any] = {"prod_ns": v["prod_ns"]}
    provenance: dict[str, str] = {name: origin.get(name, "user") for name in _SCALARS}
    for name in TECHNICAL:
        protocol[name] = v.get(name, TECHNICAL_DEFAULTS[name])
        provenance[name] = origin[name] if name in v else "default"
    named = list(v.get("analyses", []))
    analyses = [*BASELINE, *[a for a in named if a not in BASELINE]]
    for name in analyses:
        provenance[f"analyses.{name}"] = (
            origin.get("analyses", "user") if name in named else "default")
    request: dict[str, Any] = {
        "system": {"kind": "water", "n_molecules": v["n_molecules"],
                   "water_model": v["water_model"]},
        "force_field": v["force_field"],
        "thermo": {"temperature_K": v["temperature_K"], "pressure_bar": v["pressure_bar"]},
        "protocol": {k: protocol[k] for k in ("emtol", "nvt_ps", "npt_ps", "prod_ns", "dt_ps",
                                              "tau_t", "tau_p")},
        "seed": v.get("seed"),
        "analyses": analyses,
        "hardware": v.get("hardware", {"ntomp": None, "ntmpi": None}),
        "provenance_of_fields": provenance,
    }
    if "seed" in v:
        provenance["seed"] = origin.get("seed", "user")
    return request


def _object(data: Any, path: str, allowed: Sequence[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be an object")
    for key in data:
        if key not in allowed:
            raise ValueError(f"unknown key: {path}.{key}" if path != "request" else
                             f"unknown key: {key}")
    return data


def draft_from_request_file(data: dict[str, Any]) -> RequestDraft:
    """A complete request from a file (design 3.1 shape). Every value counts as stated."""
    top = _object(data, "request", ("system", "force_field", "thermo", "protocol", "seed",
                                    "analyses", "hardware", "provenance_of_fields"))
    values: dict[str, Any] = {}
    if "system" in top:
        system = _object(top["system"], "system", ("kind", "n_molecules", "water_model"))
        if system.get("kind", "water") != "water":
            raise ValueError("system.kind must be water")
        for name in ("n_molecules", "water_model"):
            if name in system:
                values[name] = _coerce(name, system[name], f"system.{name}")
    if "force_field" in top:
        values["force_field"] = _coerce("force_field", top["force_field"])
    if "thermo" in top:
        thermo = _object(top["thermo"], "thermo", ("temperature_K", "pressure_bar"))
        for name, raw in thermo.items():
            values[name] = _coerce(name, raw, f"thermo.{name}")
    if "protocol" in top:
        protocol = _object(top["protocol"], "protocol", ("prod_ns", *TECHNICAL))
        for name, raw in protocol.items():
            values[name] = _coerce(name, raw, f"protocol.{name}")
    if top.get("seed") is not None:
        values["seed"] = _coerce("seed", top["seed"])
    if "analyses" in top:
        values["analyses"] = _coerce("analyses", top["analyses"])
    if "hardware" in top:
        hardware = _object(top["hardware"], "hardware", ("ntomp", "ntmpi"))
        values["hardware"] = {"ntomp": hardware.get("ntomp"), "ntmpi": hardware.get("ntmpi")}
    return RequestDraft(values, {name: "user" for name in values if name != "hardware"})


def _property(kind: str, description: str) -> dict[str, Any]:
    return {"type": kind, "description": description}


SUBMIT_REQUEST_FUNCTION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_request",
        "description": (
            "Submit the simulation fields you extracted from the researcher's words. Include "
            "only fields they stated or that you can infer, and tag each one in origins as "
            "user (they said it) or inferred (you guessed it). Never invent a value."),
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "n_molecules": _property("integer", "number of water molecules"),
                "water_model": _property("string", "water model, for example tip4p"),
                "force_field": _property("string", "force field, for example oplsaa"),
                "temperature_K": _property("number", "temperature in kelvin"),
                "pressure_bar": _property("number", "pressure in bar"),
                "prod_ns": _property("number", "production length in nanoseconds"),
                "analyses": {"type": "array", "items": {"type": "string",
                                                        "enum": list(ANALYSIS_NAMES)}},
                "emtol": _property("number", "EM force tolerance, kJ/mol/nm"),
                "nvt_ps": _property("number", "NVT equilibration, ps"),
                "npt_ps": _property("number", "NPT equilibration, ps"),
                "dt_ps": _property("number", "time step, ps"),
                "tau_t": _property("number", "thermostat time constant, ps"),
                "tau_p": _property("number", "barostat time constant, ps"),
                "seed": _property("integer", "random seed"),
                "origins": {"type": "object",
                            "additionalProperties": {"type": "string",
                                                     "enum": ["user", "inferred"]}},
            },
        },
    },
}

ASK_RESEARCHER_FUNCTION: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "ask_researcher",
        "description": ("Ask the researcher up to three short questions, each with a proposed "
                        "answer they can accept with yes. Never ask about a field they stated."),
        "parameters": {
            "type": "object", "additionalProperties": False, "required": ["questions"],
            "properties": {"questions": {
                "type": "array", "maxItems": 3,
                "items": {"type": "object", "additionalProperties": False,
                          "required": ["field", "text", "proposal"],
                          "properties": {"field": {"type": "string"}, "text": {"type": "string"},
                                         "proposal": {"type": "string"}}}}},
        },
    },
}
