"""Parser for GROMACS .xvg files."""

from __future__ import annotations

import re

_LEGEND = re.compile(r'@\s*s\d+\s+legend\s+"(.*)"')


def parse_xvg(text: str) -> tuple[list[str], list[list[float]]]:
    """Return (legends, rows). Comment lines (#) and other @ directives are skipped."""
    legends: list[str] = []
    rows: list[list[float]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("@"):
            match = _LEGEND.match(stripped)
            if match:
                legends.append(match.group(1))
            continue
        try:
            rows.append([float(x) for x in stripped.split()])
        except ValueError:
            continue
    return legends, rows
