"""Error codes, the exception type, and the tool result envelope (design 4.1 and 4.2)."""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    E_GMX_NOT_FOUND = "E_GMX_NOT_FOUND"
    E_GMX_NOT_ELF = "E_GMX_NOT_ELF"
    E_GMX_VERSION = "E_GMX_VERSION"
    E_DATA_MISSING = "E_DATA_MISSING"
    E_FAKE_BINARY = "E_FAKE_BINARY"
    E_PROBE_FAILED = "E_PROBE_FAILED"
    E_PARAM_OUT_OF_RANGE = "E_PARAM_OUT_OF_RANGE"
    E_PATH_ESCAPE = "E_PATH_ESCAPE"
    E_JOB_NOT_FOUND = "E_JOB_NOT_FOUND"
    E_STAGE_ORDER = "E_STAGE_ORDER"
    E_STAGE_STATE = "E_STAGE_STATE"
    E_RETRY_NOT_ALLOWED = "E_RETRY_NOT_ALLOWED"
    E_RETRY_LIMIT = "E_RETRY_LIMIT"
    E_NOT_APPROVED = "E_NOT_APPROVED"
    E_APPROVAL_UNAVAILABLE = "E_APPROVAL_UNAVAILABLE"
    E_BAD_TOKEN = "E_BAD_TOKEN"
    E_PLAN_MISMATCH = "E_PLAN_MISMATCH"
    E_INPUT_MODIFIED = "E_INPUT_MODIFIED"
    E_BINARY_CHANGED = "E_BINARY_CHANGED"
    E_BUILD_COUNT = "E_BUILD_COUNT"
    E_GROMPP_FAILED = "E_GROMPP_FAILED"
    E_MDRUN_FAILED = "E_MDRUN_FAILED"
    E_MDRUN_RUNNING = "E_MDRUN_RUNNING"
    E_NO_RECEIPT = "E_NO_RECEIPT"
    E_GATE_FAILED = "E_GATE_FAILED"
    E_ANALYSIS_INPUT = "E_ANALYSIS_INPUT"
    E_CANCELLED = "E_CANCELLED"
    E_TIMEOUT = "E_TIMEOUT"
    E_DISK_FULL = "E_DISK_FULL"


class SimAgentError(Exception):
    """A failure that maps to a tool error envelope."""

    def __init__(self, code: ErrorCode, message: str, hint: str = "",
                 data: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message
        self.hint = hint
        self.data = data


def ok(data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": True, "data": data if data is not None else {}, "error": None}


def fail(code: ErrorCode, message: str, hint: str = "",
         data: dict[str, Any] | None = None) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "ok": False,
        "error": {"code": code.value, "message": message, "hint": hint},
    }
    envelope["data"] = data if data is not None else {}
    return envelope


def error_envelope(exc: SimAgentError) -> dict[str, Any]:
    return fail(exc.code, exc.message, exc.hint, exc.data)
