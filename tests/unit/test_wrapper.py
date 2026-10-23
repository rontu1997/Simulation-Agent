import json
import os
import sys
import time
from pathlib import Path

import pytest

from simagent.core.mdrun_wrapper import launch_wrapper, run_wrapper
from simagent.core.provenance import Ledger


def setup(tmp_path):
    stage_dir = tmp_path / "05_prod"
    stage_dir.mkdir()
    return stage_dir, tmp_path / "provenance.jsonl"


def wrapper(tmp_path, code, **kwargs):
    stage_dir, ledger_path = setup(tmp_path)
    exit_code = run_wrapper(stage_dir, [sys.executable, "-c", code], dict(os.environ),
                            ledger_path=ledger_path, stage="prod",
                            max_wall_hours=kwargs.pop("max_wall_hours", 1.0), **kwargs)
    return stage_dir, Ledger(ledger_path), exit_code


def test_wrapper_runs_child_and_writes_files(tmp_path):
    stage_dir, ledger, code = wrapper(tmp_path, "print('hello from gmx')")
    assert code == 0
    assert (stage_dir / "run.exit").read_text(encoding="utf-8").strip() == "0"
    assert "hello from gmx" in (stage_dir / "mdrun.out").read_text(encoding="utf-8")
    info = json.loads((stage_dir / "run.json").read_text(encoding="utf-8"))
    assert info["state"] == "running" and info["pid"] == os.getpid() and info["gmx_pid"]
    records = ledger.records()
    assert [r["event"] for r in records] == ["start", "end"]
    assert records[1]["exit_code"] == 0 and records[1]["stage"] == "prod"


def test_end_record_is_written_before_run_exit(tmp_path):
    seen = {}
    stage_dir, ledger_path = setup(tmp_path)
    ledger = Ledger(ledger_path)

    def before_exit_written() -> None:
        seen["exit_exists"] = (stage_dir / "run.exit").exists()
        seen["events"] = [r["event"] for r in ledger.records()]

    run_wrapper(stage_dir, [sys.executable, "-c", "pass"], dict(os.environ),
                ledger_path=ledger_path, stage="prod", max_wall_hours=1.0,
                before_exit_written=before_exit_written)
    assert seen == {"exit_exists": False, "events": ["start", "end"]}


def test_end_record_lists_output_receipts(tmp_path):
    stage_dir, ledger_path = setup(tmp_path)
    code = "open('prod.xtc','w').write('x'); open('prod.log','w').write('Finished mdrun')"
    run_wrapper(stage_dir, [sys.executable, "-c", code], dict(os.environ),
                ledger_path=ledger_path, stage="prod", max_wall_hours=1.0)
    outputs = {o["path"] for o in Ledger(ledger_path).records()[-1]["outputs"]}
    assert {"05_prod/prod.xtc", "05_prod/prod.log"} <= outputs
    assert not any(p.endswith(("run.exit", "run.json")) for p in outputs)


def test_nonzero_exit_is_recorded(tmp_path):
    stage_dir, ledger, code = wrapper(tmp_path, "import sys; sys.exit(3)")
    assert code == 3
    assert (stage_dir / "run.exit").read_text(encoding="utf-8").strip() == "3"
    assert ledger.records()[-1]["exit_code"] == 3


def test_timeout_stops_the_child_and_writes_run_timeout(tmp_path):
    started = time.time()
    stage_dir, ledger, code = wrapper(tmp_path, "import time; time.sleep(60)",
                                      max_wall_hours=0.0003)
    assert time.time() - started < 30
    assert (stage_dir / "run.timeout").exists()
    assert code != 0
    assert int((stage_dir / "run.exit").read_text(encoding="utf-8")) != 0
    assert ledger.records()[-1]["event"] == "end"


@pytest.mark.posix
def test_launch_survives_and_finishes_independently(tmp_path):
    stage_dir, ledger_path = setup(tmp_path)
    pid = launch_wrapper(stage_dir, [sys.executable, "-c", "print('bg')"], dict(os.environ),
                         ledger_path=ledger_path, stage="prod", max_wall_hours=1.0)
    assert pid > 0
    assert json.loads((stage_dir / "run.json").read_text(encoding="utf-8"))["state"] in (
        "launching", "running")
    for _ in range(100):
        if (stage_dir / "run.exit").exists():
            break
        time.sleep(0.1)
    assert (stage_dir / "run.exit").read_text(encoding="utf-8").strip() == "0"


def test_launch_accepts_relative_paths(tmp_path, monkeypatch):
    """The wrapper starts inside the stage directory, so relative paths must be made absolute
    first (the default runs_dir is relative)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "runs" / "job" / "05_prod").mkdir(parents=True)
    stage_dir = Path("runs") / "job" / "05_prod"
    launch_wrapper(stage_dir, [sys.executable, "-c", "print('rel')"], dict(os.environ),
                   ledger_path=Path("runs") / "job" / "provenance.jsonl", stage="prod",
                   max_wall_hours=1.0)
    absolute = tmp_path / "runs" / "job" / "05_prod"
    for _ in range(150):
        if (absolute / "run.exit").exists():
            break
        time.sleep(0.1)
    assert (absolute / "run.exit").read_text(encoding="utf-8").strip() == "0", (
        (absolute / "wrapper.err").read_text(encoding="utf-8"))
    assert [r["event"] for r in Ledger(tmp_path / "runs" / "job" / "provenance.jsonl").records()
            ] == ["start", "end"]
