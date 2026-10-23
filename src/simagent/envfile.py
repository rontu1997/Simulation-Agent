"""Read settings from a ``.env`` file into the process environment (CLI only).

The real environment always wins over the file. Only ``LUNA_*``, ``GMX_*``, and ``SIMAGENT_*``
settings are accepted, so a stray ``.env`` in the folder you run from cannot set ``PATH`` or
``LD_PRELOAD``, and the approval token (generated for each launch) can never come from a file.
Messages name keys and line numbers, never values.
"""

from __future__ import annotations

import re
import sys
from collections.abc import MutableMapping
from pathlib import Path

ALLOWED_PREFIXES = ("LUNA_", "GMX_", "SIMAGENT_")
_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_TRAILING_COMMENT = re.compile(r"\s+#.*$")


class EnvFileError(Exception):
    """The file could not be read."""


def parse_env(text: str) -> tuple[dict[str, str], list[str]]:
    """``KEY=value`` pairs and a list of problems (line numbers only)."""
    pairs: dict[str, str] = {}
    problems: list[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        match = _LINE.match(line)
        if match is None:
            problems.append(f"line {number}: not a KEY=value line")
            continue
        name, value = match.group(1), match.group(2).strip()
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            if end < 0:
                problems.append(f"line {number}: unclosed quote")
                continue
            value = value[1:end]
        else:
            value = _TRAILING_COMMENT.sub("", value).strip()
        pairs[name] = value
    return pairs, problems


def _refusal(name: str) -> str | None:
    if name.upper().startswith("SIMAGENT_APPROVAL"):
        return (f"ignored {name}: the approval token is generated for each launch and cannot "
                "be set from a file")
    if not name.startswith(ALLOWED_PREFIXES):
        return (f"ignored {name}: only LUNA_*, GMX_*, and SIMAGENT_* settings are read from "
                "an env file")
    return None


def load_env_file(path: Path, environ: MutableMapping[str, str]) -> tuple[list[str], list[str]]:
    """Set the accepted keys that ``environ`` does not already have. Returns the names that were
    set and the warnings to show."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")  # Windows editors may add a byte-order mark
    except (OSError, UnicodeDecodeError) as exc:
        raise EnvFileError(f"cannot read the env file {path}: {type(exc).__name__}") from exc
    pairs, problems = parse_env(text)
    warnings = [f"{path}, {problem}" for problem in problems]
    if sys.platform != "win32" and path.stat().st_mode & 0o077:
        warnings.append(f"{path} is readable by other users; run: chmod 600 {path}")
    loaded: list[str] = []
    for name, value in pairs.items():
        refusal = _refusal(name)
        if refusal is not None:
            warnings.append(refusal)
        elif name not in environ:
            environ[name] = value
            loaded.append(name)
    return loaded, warnings
