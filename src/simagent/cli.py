"""Command-line entry point (the agent CLI arrives in a later milestone)."""

from __future__ import annotations

from collections.abc import Sequence

from simagent import __version__


def main(argv: Sequence[str] | None = None) -> int:
    print(f"simagent {__version__}")
    return 0
