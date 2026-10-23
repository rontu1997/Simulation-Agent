"""Job directories, ``job.json``, and the stage state machine (design 3.3 and 3.4).

Only the server writes ``job.json``, always under a file lock and atomically.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock

from simagent.config import Config
from simagent.core.paths import validate_job_id
from simagent.core.provenance import Ledger
from simagent.errors import ErrorCode, SimAgentError

STAGES: tuple[str, ...] = ("build", "em", "nvt", "npt", "prod", "analysis")
STAGE_DIRS: dict[str, str] = {
    "build": "01_build", "em": "02_em", "nvt": "03_nvt", "npt": "04_npt", "prod": "05_prod",
    "analysis": "06_analysis",
}
PROCESS_STAGES: tuple[str, ...] = ("em", "nvt", "npt", "prod")

_LEGAL = {
    ("PENDING", "READY"), ("READY", "RUNNING"), ("RUNNING", "GATING"), ("RUNNING", "FAILED"),
    ("RUNNING", "CANCELLED"), ("GATING", "PASSED"), ("GATING", "FAILED"),
    ("FAILED", "RUNNING"), ("CANCELLED", "RUNNING"),
}
_SYNC_STAGES = {"build", "analysis"}  # no external process: PENDING goes straight to RUNNING


def _now_iso(clock: Callable[[], datetime]) -> str:
    return clock().isoformat()


class JobHandle:
    def __init__(self, job_id: str, directory: Path) -> None:
        self.job_id = job_id
        self.dir = Path(directory)
        self.ledger = Ledger(self.dir / "provenance.jsonl")
        self._path = self.dir / "job.json"
        self._lock = FileLock(str(self.dir / "job.json.lock"))

    def read(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self._path.read_text(encoding="utf-8"))
        return data

    def update(self, fn: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        with self._lock:
            data = self.read()
            fn(data)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._path)
            return data

    def stage_dir(self, stage: str) -> Path:
        if stage not in STAGE_DIRS:
            raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE, f"unknown stage: {stage}",
                                hint=f"stage must be one of {', '.join(STAGES)}")
        return self.dir / STAGE_DIRS[stage]

    def status(self, stage: str) -> str:
        self.stage_dir(stage)
        return str(self.read()["stage_status"][stage])

    def set_status(self, stage: str, new: str) -> None:
        self.stage_dir(stage)

        def fn(data: dict[str, Any]) -> None:
            old = data["stage_status"][stage]
            legal = (old, new) in _LEGAL or (
                stage in _SYNC_STAGES and (old, new) == ("PENDING", "RUNNING"))
            if not legal:
                raise SimAgentError(
                    ErrorCode.E_STAGE_STATE,
                    f"stage {stage} cannot go from {old} to {new}",
                    hint="see the stage transition table in design 3.4")
            data["stage_status"][stage] = new

        self.update(fn)

    def invalidate(self, stage: str) -> list[str]:
        """Send ``stage`` and every later stage to PENDING and move their files aside."""
        self.stage_dir(stage)
        affected = list(STAGES[STAGES.index(stage):])

        def fn(data: dict[str, Any]) -> None:
            if any(data["stage_status"][s] == "RUNNING" for s in PROCESS_STAGES):
                raise SimAgentError(ErrorCode.E_MDRUN_RUNNING,
                                    "a stage process is running; stages cannot be reset now",
                                    hint="cancel the run first")
            root = self.dir / "superseded"
            n = len(list(root.iterdir())) + 1 if root.exists() else 1
            for name in affected:
                directory = self.stage_dir(name)
                if any(directory.iterdir()):
                    dest = root / str(n) / STAGE_DIRS[name]
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(directory), str(dest))
                    directory.mkdir()
                data["stage_status"][name] = "PENDING"
                data["mdp_expected_sha256"].pop(name, None)

        self.update(fn)
        return affected

    def job_status(self) -> str:
        statuses = list(self.read()["stage_status"].values())
        if any(s in ("RUNNING", "GATING") for s in statuses):
            return "running"
        if "FAILED" in statuses:
            return "failed"
        if "CANCELLED" in statuses:
            return "cancelled"
        if all(s == "PASSED" for s in statuses):
            return "passed"
        return "planned"


def _num(value: Any) -> str:
    return f"{float(value):g}" if isinstance(value, (int, float)) else "?"


def _title(request: dict[str, Any]) -> str:
    system = request.get("system", {})
    thermo = request.get("thermo", {})
    prod_ns = request.get("protocol", {}).get("prod_ns")
    return (f"{system.get('n_molecules', '?')} {system.get('water_model', '?')} water, "
            f"{_num(thermo.get('temperature_K'))} K, {_num(thermo.get('pressure_bar'))} bar, "
            f"{_num(prod_ns)} ns")


class JobStore:
    def __init__(self, cfg: Config,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.cfg = cfg
        self._clock = clock

    def _roots(self) -> list[Path]:
        # absolute, so job paths mean the same thing to the server, the wrapper, and clients
        return [Path(self.cfg.paths.runs_dir).resolve(),
                Path(self.cfg.paths.sim_runs_dir).resolve()]

    def create(self, request: dict[str, Any], execution_mode: str,
               environment: dict[str, Any]) -> JobHandle:
        system = request.get("system", {})
        stem = (f"{system.get('kind', 'water')}-{system.get('n_molecules', 0)}-"
                f"{system.get('water_model', 'tip4p')}-"
                f"{_num(request.get('thermo', {}).get('temperature_K', 0))}K-"
                f"{self._clock().strftime('%Y%m%d')}")
        root = Path(self.cfg.paths.sim_runs_dir if execution_mode == "simulated"
                    else self.cfg.paths.runs_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        k = 1
        while True:
            job_id = f"{stem}-{k}"
            validate_job_id(job_id)
            if not any((r / job_id).exists() for r in self._roots()):
                try:
                    (root / job_id).mkdir()
                except FileExistsError:
                    k += 1
                    continue
                break
            k += 1
        directory = root / job_id
        for name in STAGES:
            (directory / STAGE_DIRS[name]).mkdir()
        data: dict[str, Any] = {
            "job_id": job_id, "created_at": _now_iso(self._clock), "request": request,
            "plan": None, "plan_sha256": None,
            "stage_status": {s: "PENDING" for s in STAGES},
            "environment": environment, "execution_mode": execution_mode,
            "approval": None, "pending_warnings": [], "acknowledged_warnings": [],
            "mdp_expected_sha256": {}, "failures": [], "retries": [], "report_sha256": None,
            "config_snapshot": {"config": repr(self.cfg)},
        }
        (directory / "job.json").write_text(
            json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        return JobHandle(job_id, directory)

    def get(self, job_id: str) -> JobHandle:
        validate_job_id(job_id)
        for root in self._roots():
            if (root / job_id / "job.json").is_file():
                return JobHandle(job_id, root / job_id)
        raise SimAgentError(ErrorCode.E_JOB_NOT_FOUND, f"no such job: {job_id}",
                            hint="list jobs to see the available job ids")

    def list(self, limit: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for root in self._roots():
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                if entry.name.startswith("_") or not (entry / "job.json").is_file():
                    continue
                job = JobHandle(entry.name, entry)
                data = job.read()
                job_status = job.job_status()
                if status is not None and job_status != status:
                    continue
                rows.append({
                    "job_id": entry.name, "created": data["created_at"],
                    "title": _title(data["request"]), "status": job_status,
                    "execution_mode": data["execution_mode"], "path": str(entry),
                })
        rows.sort(key=lambda r: (r["created"], r["job_id"]), reverse=True)
        return rows[:limit] if limit is not None else rows
