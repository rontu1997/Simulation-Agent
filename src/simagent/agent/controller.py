"""Workflow controller (design 14.2): deterministic sequencing of the approved plan over MCP tools.

The loop is state-driven. Each round reads ``get_job`` and does whatever the first stage that is
not ``PASSED`` calls for, so a fresh run, a retry, a resume, and a reattach after a restart are
the same code path. Researcher decisions go through ``Decisions``; this module never calls
``record_approval`` and never builds a tool argument from log text.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from simagent.agent.diagnostician import Diagnosis, Diagnostician, Evidence
from simagent.config import Config

POLL_FLOOR_S = 10.0
PROCESS_STAGES = ("em", "nvt", "npt", "prod")
_ENDED = ("finished", "died", "cancelled", "not_started")


class ToolCaller(Protocol):
    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]: ...


class Decisions(Protocol):
    async def acknowledge_warnings(self, job_id: str,
                                   warnings: list[dict[str, Any]]) -> list[str]: ...

    async def on_interrupted(self, job_id: str, stage: str, cause: str,
                             can_resume: bool) -> str: ...


class NoDecisions:
    """The safe default: acknowledge nothing and stop when a run was interrupted."""

    async def acknowledge_warnings(self, job_id: str, warnings: list[dict[str, Any]]) -> list[str]:
        return []

    async def on_interrupted(self, job_id: str, stage: str, cause: str, can_resume: bool) -> str:
        return "stop"


@dataclass
class Outcome:
    status: str  # done, escalate, cancelled, failed, limit
    stage: str | None
    message: str
    diagnosis: Diagnosis | None = None
    disclosures: list[str] = field(default_factory=list)


class _Stop(Exception):
    def __init__(self, outcome: Outcome) -> None:
        super().__init__(outcome.message)
        self.outcome = outcome


class _Restart(Exception):
    """A retry was applied; re-read the job and start the stage scan again."""


def _describe(envelope: dict[str, Any]) -> str:
    error = envelope.get("error") or {}
    text = f"{error.get('code', 'E_UNKNOWN')}: {error.get('message', 'the tool failed')}"
    return f"{text}. {error['hint']}" if error.get("hint") else text


class Controller:
    def __init__(self, tools: ToolCaller, cfg: Config, decisions: Decisions | None = None, *,
                 sleep: Callable[[float], Awaitable[None]] | None = None,
                 poll_interval_s: float = POLL_FLOOR_S, max_tool_calls: int = 500,
                 max_rounds: int = 40, on_event: Callable[[dict[str, Any]], None] | None = None,
                 diagnostician: Diagnostician | None = None) -> None:
        self.tools = tools
        self.cfg = cfg
        self.decisions: Decisions = decisions if decisions is not None else NoDecisions()
        self._sleep = sleep
        self._interval = max(poll_interval_s, POLL_FLOOR_S)
        self._max_calls = max_tool_calls
        self._max_rounds = max_rounds
        self._on_event = on_event
        self.diagnostician = diagnostician or Diagnostician()
        self._calls = 0
        self._cancelled = False
        self._wake = asyncio.Event()
        self.disclosures: list[str] = []

    # -- plumbing ---------------------------------------------------------------------------

    def _emit(self, state: str, job_id: str, stage: str | None = None, **extra: Any) -> None:
        if self._on_event is not None:
            self._on_event({"state": state, "job_id": job_id, "stage": stage, **extra})

    async def _call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if self._calls >= self._max_calls:
            raise _Stop(Outcome("limit", None, f"stopped after {self._calls} tool calls "
                                "(the controller's call limit)"))
        self._calls += 1
        try:
            envelope = await self.tools.call(name, args)
        except Exception as exc:  # the server went away or the transport broke
            raise _Stop(Outcome("failed", None, f"tool {name} failed: "
                                f"{type(exc).__name__}: {exc}")) from exc
        if not isinstance(envelope, dict) or "ok" not in envelope:
            raise _Stop(Outcome("failed", None, f"tool {name} failed: it returned no result "
                                "envelope"))
        return envelope

    async def _wait(self, seconds: float) -> None:
        if self._sleep is not None:
            await self._sleep(seconds)
            return
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def _outcome(self, status: str, stage: str | None, message: str,
                 diagnosis: Diagnosis | None = None) -> Outcome:
        return Outcome(status, stage, message, diagnosis, list(self.disclosures))

    # -- public -----------------------------------------------------------------------------

    async def run(self, job_id: str) -> Outcome:
        self._cancelled = False
        self._wake.clear()
        self.disclosures = []
        try:
            outcome = await self._run(job_id)
        except _Stop as stop:
            outcome = stop.outcome
            outcome.disclosures = list(self.disclosures)
        state = {"done": "DONE", "escalate": "ESCALATE", "cancelled": "CANCELLED"}.get(
            outcome.status, "FAILED")
        self._emit(state, job_id, outcome.stage, message=outcome.message)
        return outcome

    async def cancel(self, job_id: str) -> dict[str, Any]:
        """Stop the running stage now: wake the poll sleep, then call ``cancel_run``."""
        self._cancelled = True
        self._wake.set()
        job = await self._call("get_job", {"job_id": job_id})
        running = next((s for s in PROCESS_STAGES
                        if job["ok"] and job["data"]["stage_status"].get(s) == "RUNNING"), None)
        if running is None:
            return {"ok": True, "data": {"cancelled": False, "signal_sent": None}, "error": None}
        return await self._call("cancel_run", {"job_id": job_id, "stage": running})

    # -- the loop ---------------------------------------------------------------------------

    async def _run(self, job_id: str) -> Outcome:
        env = await self._call("gmx_check_environment", {})
        if not env["ok"]:
            return self._outcome("failed", None, _describe(env))
        rounds = 0
        while True:
            job = await self._call("get_job", {"job_id": job_id})
            if not job["ok"]:
                return self._outcome("failed", None, _describe(job))
            statuses = job["data"]["stage_status"]
            stage = next((s for s in PROCESS_STAGES if statuses[s] != "PASSED"), None)
            if stage is None:
                return self._outcome("done", "prod", "em, nvt, npt, and prod passed their gates")
            rounds += 1
            if rounds > self._max_rounds:
                return self._outcome("limit", stage, f"stopped after {self._max_rounds} rounds "
                                     "without finishing")
            try:
                outcome = await self._advance(job_id, stage, job["data"])
            except _Restart:
                continue
            if outcome is not None:
                return outcome

    async def _advance(self, job_id: str, stage: str, job: dict[str, Any]) -> Outcome | None:
        """Do what ``stage`` needs next. ``None`` means progress was made; re-read the job."""
        status = job["stage_status"][stage]
        args = {"job_id": job_id, "stage": stage}
        self._emit("EXECUTE", job_id, stage)
        if status == "PENDING":
            stopped = await self._prepare(job_id, stage)
            if stopped is not None:
                return stopped
            status = "READY"
        if status == "READY":
            started = await self._call("run_mdrun", {**args, "resume": False})
            if not started["ok"]:
                return self._outcome("escalate", stage, _describe(started))
        elif status in ("FAILED", "CANCELLED"):
            return await self._stopped_stage(job_id, stage, job, status)
        elif status != "RUNNING":
            return self._outcome("escalate", stage, f"{stage} is {status} and needs a decision")
        await self._poll(job_id, stage)
        return await self._validate(job_id, stage)

    async def _prepare(self, job_id: str, stage: str) -> Outcome | None:
        args = {"job_id": job_id, "stage": stage}
        written = await self._call("write_mdp", args)
        if not written["ok"]:
            return self._outcome("escalate", stage, _describe(written))
        for attempt in range(2):
            grompp = await self._call("run_grompp", args)
            if grompp["ok"]:
                return None
            warnings = (grompp.get("data") or {}).get("warnings") or []
            unacknowledged = [w for w in warnings if not w.get("acknowledged")]
            code = (grompp.get("error") or {}).get("code")
            if code == "E_GROMPP_FAILED" and unacknowledged and attempt == 0:
                accepted = await self.decisions.acknowledge_warnings(job_id, unacknowledged)
                if {w["id"] for w in unacknowledged} <= set(accepted):
                    continue
                texts = "; ".join(f"{w['id']}: {w['text']}" for w in unacknowledged)
                return self._outcome("escalate", stage,
                                     f"grompp stopped on warnings that were not acknowledged: "
                                     f"{texts}")
            if code == "E_GROMPP_FAILED":
                return await self._diagnose(job_id, stage, [])
            return self._outcome("escalate", stage, _describe(grompp))
        return None  # unreachable: the loop returns or continues

    async def _poll(self, job_id: str, stage: str) -> str:
        while True:
            status = await self._call("get_run_status", {"job_id": job_id, "stage": stage})
            if not status["ok"]:
                raise _Stop(self._outcome("escalate", stage, _describe(status)))
            state = str(status["data"]["state"])
            if state in _ENDED:
                return state
            await self._wait(self._interval)
            if self._cancelled:
                raise _Stop(self._outcome("cancelled", stage, f"{stage} was cancelled"))

    async def _validate(self, job_id: str, stage: str) -> Outcome | None:
        result = await self._call("validate_stage", {"job_id": job_id, "stage": stage})
        if not result["ok"]:
            code = (result.get("error") or {}).get("code")
            status = "cancelled" if code == "E_CANCELLED" else "escalate"
            return self._outcome(status, stage, _describe(result))
        if result["data"]["passed"]:
            return None
        failed = [c["name"] for c in result["data"]["checks"]
                  if c["status"] in ("fail", "no_receipt", "not_run")]
        return await self._diagnose(job_id, stage, failed)

    async def _stopped_stage(self, job_id: str, stage: str, job: dict[str, Any],
                             status: str) -> Outcome:
        """A stage found FAILED or CANCELLED (a reattach, or a stage left by an earlier run)."""
        causes = [f["cause"] for f in job.get("failures", []) if f["stage"] == stage]
        last = causes[-1] if causes else ""
        if status == "CANCELLED":
            return await self._interrupted(job_id, stage, "cancelled")
        if last in ("process_died", "timeout"):
            return await self._interrupted(job_id, stage, last)
        prefix = "gate failed: "  # a failed gate after a finished run is never resumed
        names = last[len(prefix):] if last.startswith(prefix) else ""
        failed = [n.strip() for n in names.split(",") if n.strip()]
        return await self._diagnose(job_id, stage, failed)

    async def _has_checkpoint(self, job_id: str, stage: str) -> bool:
        listing = await self._call("list_artifacts", {"job_id": job_id, "stage": stage})
        return bool(listing["ok"] and any(
            str(f["path"]).endswith(f"{stage}.cpt") for f in listing["data"]["files"]))

    async def _interrupted(self, job_id: str, stage: str, cause: str,
                           has_checkpoint: bool | None = None,
                           diagnosis: Diagnosis | None = None) -> Outcome:
        """The process died, timed out, or was cancelled: ask the researcher, then resume from
        the checkpoint or restart from grompp (always raising ``_Restart``), or stop."""
        if has_checkpoint is None:
            has_checkpoint = await self._has_checkpoint(job_id, stage)
        choice = await self.decisions.on_interrupted(job_id, stage, cause, has_checkpoint)
        if choice == "resume" and has_checkpoint:
            started = await self._call("run_mdrun", {
                "job_id": job_id, "stage": stage, "resume": True})
            if not started["ok"]:
                return self._outcome("escalate", stage, _describe(started))
            self.disclosures.append(f"Resumed {stage} from its checkpoint after it {cause}")
            self._emit("EXECUTE", job_id, stage, resumed=True)
            raise _Restart
        if choice in ("resume", "restart"):
            reset = await self._call("write_mdp", {"job_id": job_id, "stage": stage,
                                                    "reset": True})
            if not reset["ok"]:
                return self._outcome("escalate", stage, _describe(reset))
            self.disclosures.append(f"Restarted {stage} from grompp after it {cause}")
            raise _Restart
        status = "cancelled" if cause == "cancelled" else "escalate"
        return self._outcome(status, stage, f"{stage} was interrupted ({cause}) and the run "
                             "was stopped at your request", diagnosis)

    async def _diagnose(self, job_id: str, stage: str, failed_checks: list[str]) -> Outcome:
        """Match the playbook and apply the next allowed retry (raising ``_Restart``), or
        return the escalation for the researcher."""
        self._emit("DIAGNOSE", job_id, stage)
        job = await self._call("get_job", {"job_id": job_id})
        data = job["data"] if job["ok"] else {}
        causes = [f["cause"] for f in data.get("failures", []) if f["stage"] == stage][-1:]
        died = bool(causes) and causes[0] in ("process_died", "timeout")
        has_checkpoint = await self._has_checkpoint(job_id, stage) if died else False
        texts: dict[str, str] = {}
        for which in ("log", "grompp", "stderr"):
            excerpt = await self._call("get_log_excerpt", {
                "job_id": job_id, "stage": stage, "which": which, "lines": 40})
            if excerpt["ok"]:
                texts[which] = "\n".join(excerpt["data"]["lines"])
        diagnosis = self.diagnostician.diagnose(
            Evidence(stage, texts, failed_checks, causes, has_checkpoint), data.get("retries", []))
        entry = diagnosis.entry
        if entry.id == "process_died":
            return await self._interrupted(job_id, stage, causes[0], has_checkpoint, diagnosis)
        if entry.action == "retry" and diagnosis.step is not None:
            step = diagnosis.step
            applied = await self._call("write_mdp", {
                "job_id": job_id, "stage": step.target,
                "retry": {"key": step.key, "value": step.value}, "failing_stage": stage})
            if not applied["ok"]:
                return self._outcome(
                    "escalate", stage, f"{entry.cause}. The server refused the automatic retry: "
                    f"{_describe(applied)}", diagnosis)
            used = sum(1 for r in data.get("retries", []) if r.get("failing_stage") == stage)
            self.disclosures.append(
                f"Retried {step.target} with {step.key}={step.value:g} after the {entry.id} "
                f"failure in {stage} (automatic, {used + 1} of 2)")
            self._emit("RETRY", job_id, stage, retry={"key": step.key, "value": step.value})
            raise _Restart
        matched = f" (matched: {diagnosis.matched})" if diagnosis.matched else ""
        tail = " Automatic retries are used up." if diagnosis.exhausted else ""
        return self._outcome("escalate", stage, f"{entry.cause}{matched}.{tail} {entry.hint}",
                             diagnosis)
