"""Playbook diagnostician (design 7): regexes pick the entry, the entry names the next step.

Matching is deterministic and only reads text; the model is never involved in choosing a retry.
Texts are cut to their last ``MAX_TEXT_CHARS`` characters so a huge log cannot stall a match.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from importlib import resources
from typing import Any

MAX_TEXT_CHARS = 20_000
_NO_CHECKPOINT_HINT = ("No checkpoint exists: reset the stage with write_mdp(reset=true) and "
                       "run it again from grompp.")


@dataclass(frozen=True)
class RetryStep:
    key: str
    value: float
    target: str


@dataclass(frozen=True)
class PlaybookEntry:
    id: str
    pattern: str | None
    source: str | None
    checks: tuple[str, ...]
    causes: tuple[str, ...]
    cause: str
    action: str
    auto: bool
    steps: tuple[RetryStep, ...]
    hint: str
    verified: bool


@dataclass
class Evidence:
    stage: str
    texts: dict[str, str] = field(default_factory=dict)
    failed_checks: list[str] = field(default_factory=list)
    causes: list[str] = field(default_factory=list)
    has_checkpoint: bool = False


@dataclass(frozen=True)
class Diagnosis:
    entry: PlaybookEntry
    matched: str | None
    step: RetryStep | None
    exhausted: bool


def load_playbook() -> list[PlaybookEntry]:
    raw = resources.files("simagent.agent").joinpath("playbook_default.json").read_text(
        encoding="utf-8")
    entries = []
    for item in json.loads(raw)["entries"]:
        entries.append(PlaybookEntry(
            id=item["id"], pattern=item.get("pattern"), source=item.get("source"),
            checks=tuple(item.get("checks", ())), causes=tuple(item.get("causes", ())),
            cause=item["cause"], action=item["action"], auto=bool(item["auto"]),
            steps=tuple(RetryStep(s["key"], float(s["value"]), s["target"])
                        for s in item.get("steps", ())),
            hint=item["hint"], verified=bool(item["verified"])))
    return entries


class Diagnostician:
    def __init__(self, entries: Sequence[PlaybookEntry] | None = None) -> None:
        self.entries = list(entries) if entries is not None else load_playbook()
        self._compiled = {e.id: re.compile(e.pattern, re.MULTILINE)
                          for e in self.entries if e.pattern}

    def _match(self, entry: PlaybookEntry, evidence: Evidence) -> tuple[bool, str | None]:
        if set(entry.checks) & set(evidence.failed_checks):
            return True, next(c for c in evidence.failed_checks if c in entry.checks)
        if set(entry.causes) & set(evidence.causes):
            return True, next(c for c in evidence.causes if c in entry.causes)
        if entry.pattern and entry.source:
            text = evidence.texts.get(entry.source, "")[-MAX_TEXT_CHARS:]
            found = self._compiled[entry.id].search(text)
            if found:
                return True, found.group(0)[:200]
        if not entry.pattern and not entry.checks and not entry.causes:
            return True, None  # the catch-all row
        return False, None

    def diagnose(self, evidence: Evidence, applied: Sequence[dict[str, Any]]) -> Diagnosis:
        for entry in self.entries:
            hit, matched = self._match(entry, evidence)
            if not hit:
                continue
            if entry.action == "resume" and not evidence.has_checkpoint:
                entry = replace(entry, action="escalate", hint=_NO_CHECKPOINT_HINT)
            return self._with_step(entry, matched, applied)
        raise LookupError("the playbook has no catch-all entry")

    @staticmethod
    def _with_step(entry: PlaybookEntry, matched: str | None,
                   applied: Sequence[dict[str, Any]]) -> Diagnosis:
        if entry.action != "retry":
            return Diagnosis(entry, matched, None, False)
        done = {(r["key"], float(r["value"])) for r in applied}
        step = next((s for s in entry.steps if (s.key, s.value) not in done), None)
        return Diagnosis(entry, matched, step, step is None)
