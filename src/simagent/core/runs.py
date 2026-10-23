"""Process-state logic for mdrun stages (design 4.5): run files, process snapshots, progress.

The state of a run is decided from files (``run.json``, ``run.exit``, ``run.cancelled``,
``run.timeout``) and, while no exit file exists, from the wrapper process itself. Linux is the
contract; on other hosts the process snapshot degrades to "alive or not".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock

LAUNCH_GRACE_S = 30.0
WRAPPER_MODULE = "simagent.core.mdrun_wrapper"
RUN_FILES = ("run.json", "run.exit", "run.cancelled", "run.timeout")


@dataclass
class RunFiles:
    run_json: dict[str, Any] | None
    has_exit: bool
    exit_code: int | None
    cancelled: bool
    timeout: bool


@dataclass
class ProcSnapshot:
    alive: bool
    start_ticks: int | None
    cmdline: str | None


def read_run_files(stage_dir: Path) -> RunFiles:
    stage_dir = Path(stage_dir)
    run_json = read_run_json(stage_dir)
    exit_file = stage_dir / "run.exit"
    exit_code: int | None = None
    has_exit = exit_file.is_file()
    if has_exit:
        try:
            exit_code = int(exit_file.read_text(encoding="utf-8").strip())
        except ValueError:
            exit_code = None
    return RunFiles(run_json=run_json, has_exit=has_exit, exit_code=exit_code,
                    cancelled=(stage_dir / "run.cancelled").exists(),
                    timeout=(stage_dir / "run.timeout").exists())


def decide_process_state(files: RunFiles, proc: ProcSnapshot | None, now: float) -> str:
    """The table of design 4.5: not_started, running, cancelled, finished, or died."""
    run = files.run_json
    if run is None:
        return "not_started"
    if run.get("state") == "launching" and not files.has_exit \
            and now - float(run.get("launched_at", 0.0)) < LAUNCH_GRACE_S:
        return "running"
    if files.cancelled:
        return "cancelled"
    if files.has_exit:
        if files.exit_code == 0 and not files.timeout:
            return "finished"
        return "died"
    if files.timeout or proc is None or not proc.alive:
        return "died"
    ticks = run.get("start_time_ticks")
    if ticks is not None and proc.start_ticks != ticks:
        return "died"  # the pid now belongs to another process
    if proc.cmdline is not None and WRAPPER_MODULE not in proc.cmdline:
        return "died"
    return "running"


def read_proc_snapshot(pid: int) -> ProcSnapshot:
    """Liveness, start time ticks, and command line of ``pid``."""
    stat = Path(f"/proc/{pid}/stat")
    if stat.is_file():  # Linux
        try:
            text = stat.read_text(encoding="utf-8", errors="replace")
            fields = text[text.rindex(")") + 2:].split()
            ticks = int(fields[19])  # field 22 of the stat line
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except (OSError, ValueError, IndexError):
            return ProcSnapshot(False, None, None)
        return ProcSnapshot(True, ticks, raw.replace(b"\0", b" ").decode("utf-8", "replace"))
    return ProcSnapshot(_pid_alive(pid), None, None)


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        # os.kill would terminate the process on Windows, so ask the kernel instead
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                        and code.value == 259)  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def estimate_rate(samples: Sequence[tuple[float, float]],
                  remaining_ps: float) -> tuple[float | None, float | None]:
    """ns/day and ETA seconds from ``(wall_s, time_ps)`` samples (design 4.5)."""
    if len(samples) < 2:
        return None, None
    latest_wall, latest_ps = samples[-1]
    earlier = next((s for s in reversed(samples[:-1]) if latest_wall - s[0] >= 30.0), None)
    if earlier is None:
        return None, None
    per_s = (latest_ps - earlier[1]) / (latest_wall - earlier[0])
    if per_s <= 0:
        return None, None
    return per_s * 86.4, remaining_ps / per_s


_STEP_BLOCK = re.compile(r"^\s*Step\s+Time\s*\n\s*(\d+)\s+(\S+)", re.MULTILINE)
_PERFORMANCE = re.compile(r"^Performance:\s+(\S+)", re.MULTILINE)


def parse_last_step(log_text: str) -> tuple[int, float] | None:
    """Step and time (ps) of the last ``Step Time`` block of an mdrun log [verify TASK-034]."""
    last = None
    for match in _STEP_BLOCK.finditer(log_text):
        last = match
    if last is None:
        return None
    try:
        return int(last.group(1)), float(last.group(2))
    except ValueError:
        return None


def parse_performance(log_text: str) -> float | None:
    match = _PERFORMANCE.search(log_text)
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def read_run_json(stage_dir: Path) -> dict[str, Any] | None:
    path = Path(stage_dir) / "run.json"
    if not path.is_file():
        return None
    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data


def update_run_json(stage_dir: Path, fn: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Rewrite ``run.json`` atomically under a file lock (the server and the wrapper share it)."""
    stage_dir = Path(stage_dir)
    with FileLock(str(stage_dir / "run.json.lock")):
        data = read_run_json(stage_dir) or {}
        fn(data)
        tmp = stage_dir / "run.json.tmp"
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, stage_dir / "run.json")
        return data


def archive_run_files(stage_dir: Path) -> Path | None:
    """Move the run files into ``run_history/<n>/`` so a new run is never mixed with the old."""
    stage_dir = Path(stage_dir)
    present = [name for name in RUN_FILES if (stage_dir / name).exists()]
    if not present:
        return None
    history = stage_dir / "run_history"
    n = len(list(history.iterdir())) + 1 if history.exists() else 1
    dest = history / str(n)
    dest.mkdir(parents=True)
    for name in present:
        shutil.move(str(stage_dir / name), str(dest / name))
    return dest
