"""The mdrun wrapper (design 4.5): survives the server, captures the exit code, writes receipts.

Order of the final writes matters: the provenance ``end`` record first, ``run.exit`` last, so a
poller that sees ``run.exit`` always finds the receipts.

Run as ``python -m simagent.core.mdrun_wrapper <stage_dir> <argv-json> --ledger P --stage S
--max-wall-hours H``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any

from simagent.core.provenance import Ledger, file_entry
from simagent.core.runs import read_proc_snapshot, update_run_json

_SKIP_OUTPUTS = ("run.", "mdrun.lock")


def _snapshot(stage_dir: Path) -> dict[str, tuple[int, int]]:
    return {p.name: (p.stat().st_mtime_ns, p.stat().st_size)
            for p in stage_dir.iterdir() if p.is_file()}


def _outputs(stage_dir: Path, before: dict[str, tuple[int, int]]) -> list[dict[str, Any]]:
    base = stage_dir.parent
    entries = []
    for path in sorted(p for p in stage_dir.iterdir() if p.is_file()):
        if path.name.startswith(_SKIP_OUTPUTS) or path.name.endswith((".lock", ".tmp")):
            continue
        stat = path.stat()
        if before.get(path.name) == (stat.st_mtime_ns, stat.st_size):
            continue  # unchanged since the run began: not an output of this run
        entries.append(file_entry(base, f"{stage_dir.name}/{path.name}"))
    return entries


def _stop_child(child: subprocess.Popen[bytes]) -> None:
    child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def run_wrapper(stage_dir: Path, argv: list[str], env: Mapping[str, str], *, ledger_path: Path,
                stage: str, max_wall_hours: float,
                before_exit_written: Callable[[], None] | None = None,
                now: Callable[[], float] = time.time) -> int:
    stage_dir = Path(stage_dir)
    ledger = Ledger(ledger_path)
    base = stage_dir.parent
    before = _snapshot(stage_dir)
    record: dict[str, Any] = {"kind": "gmx", "stage": stage, "tool": "run_mdrun", "argv": argv,
                              "cwd": stage_dir.name, "env_allow": dict(env)}
    ledger.append({**record, "event": "start", "inputs": [
        file_entry(base, f"{stage_dir.name}/{name}") for name in (f"{stage}.tpr", f"{stage}.cpt")
        if (stage_dir / name).is_file()]})

    stopping = {"signal": False}
    previous = None
    child: subprocess.Popen[bytes] | None = None

    def on_term(_signum: int, _frame: FrameType | None) -> None:
        stopping["signal"] = True  # the loop below forwards it to the child

    if sys.platform != "win32":
        try:
            previous = signal.signal(signal.SIGTERM, on_term)
        except ValueError:  # not the main thread
            previous = None

    timed_out = False
    started = now()
    try:
        with open(stage_dir / "mdrun.out", "wb") as out, open(stage_dir / "mdrun.err", "wb") as err:
            try:
                child = subprocess.Popen(  # noqa: S603 - argument list, shell=False
                    argv, cwd=str(stage_dir), env=dict(env), stdin=subprocess.DEVNULL,
                    stdout=out, stderr=err, shell=False)
            except OSError as exc:
                err.write(f"cannot execute {argv[0]}: {exc}\n".encode())
                exit_code = 127
            else:
                gmx_pid = child.pid
                update_run_json(stage_dir, lambda d: d.update(
                    state="running", pid=os.getpid(), gmx_pid=gmx_pid, argv=argv,
                    start_time_ticks=read_proc_snapshot(os.getpid()).start_ticks,
                    started_at=datetime.now(UTC).isoformat()))
                limit_s = max_wall_hours * 3600
                while child.poll() is None:
                    if stopping["signal"] or (stage_dir / "run.cancelled").exists():
                        _stop_child(child)
                        break
                    if now() - started > limit_s:
                        (stage_dir / "run.timeout").write_text("wall time exceeded\n",
                                                               encoding="utf-8")
                        timed_out = True
                        _stop_child(child)
                        break
                    try:
                        child.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        pass
                exit_code = child.returncode
                if timed_out and exit_code == 0:
                    exit_code = -signal.SIGTERM.value if sys.platform != "win32" else 1
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)

    ledger.append({**record, "event": "end", "exit_code": exit_code, "timeout": timed_out,
                   "outputs": _outputs(stage_dir, before)})
    if before_exit_written is not None:
        before_exit_written()
    tmp = stage_dir / "run.exit.tmp"
    tmp.write_text(f"{exit_code}\n", encoding="utf-8")
    os.replace(tmp, stage_dir / "run.exit")
    return exit_code


def launch_wrapper(stage_dir: Path, argv: list[str], env: Mapping[str, str], *, ledger_path: Path,
                   stage: str, max_wall_hours: float) -> int:
    """Start the wrapper detached from this process; return its pid.

    A preliminary ``run.json`` (``launching``) is written first so a status call never sees a
    half-started run.
    """
    stage_dir = Path(stage_dir)
    update_run_json(stage_dir, lambda d: d.update(
        state="launching", launched_at=time.time(), argv=argv, samples=[]))
    command = [sys.executable, "-m", "simagent.core.mdrun_wrapper", str(stage_dir),
               json.dumps(argv), "--ledger", str(ledger_path), "--stage", stage,
               "--max-wall-hours", repr(max_wall_hours)]
    options: dict[str, Any] = {"cwd": str(stage_dir), "env": dict(env), "close_fds": True,
                               "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                               "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        options["creationflags"] = (subprocess.DETACHED_PROCESS
                                    | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        options["start_new_session"] = True
    proc = subprocess.Popen(command, shell=False, **options)  # noqa: S603
    update_run_json(stage_dir, lambda d: d.update(pid=proc.pid))
    return proc.pid


def main(args: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="simagent.core.mdrun_wrapper")
    parser.add_argument("stage_dir")
    parser.add_argument("argv_json")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--max-wall-hours", type=float, required=True)
    ns = parser.parse_args(args)
    return run_wrapper(Path(ns.stage_dir), json.loads(ns.argv_json), dict(os.environ),
                       ledger_path=Path(ns.ledger), stage=ns.stage,
                       max_wall_hours=ns.max_wall_hours)


if __name__ == "__main__":
    sys.exit(main())
