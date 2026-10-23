"""Path confinement, job id validation, and hashing (RULE-SEC-02)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from simagent.errors import ErrorCode, SimAgentError

_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


def validate_job_id(job_id: str) -> str:
    """Return the job id if valid.

    A separator, "..", or a leading dot is a path escape attempt; any other mismatch is
    out of range.
    """
    if "/" in job_id or "\\" in job_id or ".." in job_id or job_id.startswith("."):
        raise SimAgentError(ErrorCode.E_PATH_ESCAPE, "job_id contains a path separator, '..', "
                            "or a leading dot", hint="use only letters, digits, '.', '_' and '-'")
    if not _JOB_ID.match(job_id):
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE, "job_id is malformed",
                            hint="1 to 80 characters: letters, digits, '.', '_', '-', "
                            "starting with a letter or digit")
    return job_id


def confine(base: Path, rel: str | Path) -> Path:
    """Resolve rel under base; refuse anything that lands outside it (including via symlinks)."""
    base_resolved = Path(base).resolve()
    target = (base_resolved / rel).resolve()
    if not target.is_relative_to(base_resolved):
        raise SimAgentError(ErrorCode.E_PATH_ESCAPE, "path escapes the job directory",
                            hint="give a path relative to the job directory without '..'")
    return target


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
