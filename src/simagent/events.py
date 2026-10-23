"""Structured event log with secret redaction (design 12, RULE-SEC-01)."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MIN_SECRET_LEN = 6


class Redactor:
    """Replaces every occurrence of a known secret with ``***``."""

    def __init__(self, secrets: Iterable[str]) -> None:
        self._secrets = sorted(
            {s for s in secrets if s and len(s) >= MIN_SECRET_LEN}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def redact_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {self.redact(str(k)): self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.redact_obj(v) for v in obj]
        return obj


class EventLog:
    def __init__(self, path: Path, redactor: Redactor) -> None:
        self.path = Path(path)
        self._redactor = redactor

    def emit(self, level: str, component: str, event: str, job_id: str | None = None,
             stage: str | None = None, data: dict[str, Any] | None = None) -> None:
        line = {
            "ts": datetime.now(UTC).isoformat(), "level": level, "component": component,
            "event": event, "job_id": job_id, "stage": stage, "data": data or {},
        }
        text = self._redactor.redact(json.dumps(self._redactor.redact_obj(line), sort_keys=True))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")


class ToolTimer:
    def __init__(self) -> None:
        self._start = time.perf_counter()
        self._external = 0.0

    def add_external(self, seconds: float) -> None:
        self._external += seconds

    def tool_time_s(self) -> float:
        return max(0.0, time.perf_counter() - self._start - self._external)


@contextmanager
def timed_tool(log: EventLog, tool: str, job_id: str | None) -> Iterator[ToolTimer]:
    """Emit ``tool_done`` with ``tool_time_s``: time in server code, excluding gmx and the LLM."""
    timer = ToolTimer()
    try:
        yield timer
    finally:
        log.emit("info", "server", "tool_done", job_id=job_id,
                 data={"tool": tool, "tool_time_s": round(timer.tool_time_s(), 6)})
