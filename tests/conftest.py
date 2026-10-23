from __future__ import annotations

import os
import subprocess
import sys

import pytest


def _real_gmx_available() -> bool:
    binary = os.environ.get("GMX_BIN")
    if not binary:
        return False
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "GROMACS" in result.stdout


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip_posix = pytest.mark.skip(reason="needs POSIX features; runs on the Linux host or WSL")
    skip_gmx = pytest.mark.skip(reason="needs the real GROMACS named by GMX_BIN")
    real_ok = None
    for item in items:
        if "posix" in item.keywords and sys.platform == "win32":
            item.add_marker(skip_posix)
        if "real_gmx" in item.keywords:
            if real_ok is None:
                real_ok = _real_gmx_available()
            if not real_ok:
                item.add_marker(skip_gmx)
