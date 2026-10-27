"""The agent session (design 10, 14.2, 14.5): intake, clarification, plan, approval, execution.

Code sequences everything; the model only turns the researcher's words into ``submit_request``
fields. All input and output go through a ``Console``, so a whole session runs in tests with
scripted input. While a run is in progress one reader task owns the console: it handles
commands (``/cancel`` acts at once) and hands answers to any prompt the controller raises.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from mcp.types import Tool

from simagent.agent.controller import POLL_FLOOR_S, Controller, Outcome, ToolCaller
from simagent.agent.conversation import ChatClient, Conversation
from simagent.agent.planning import (
    PlanError,
    Prepared,
    prepare_job,
    record_plan_approval,
    render_plan,
    replan,
)
from simagent.agent.request import (
    SCIENCE_CRITICAL,
    Question,
    RequestDraft,
    accept,
    explicit_clarification_answers,
    finalize,
    merge_submission,
    missing_questions,
    parse_value,
)
from simagent.config import Config
from simagent.core.provenance import Ledger

MAX_STALLED_CLARIFY_ROUNDS = 4
STAGE_IDS = {"em": "S2", "nvt": "S3", "npt": "S4", "prod": "S5"}
STAGE_ORDER = ("build", "em", "nvt", "npt", "prod", "analysis")
YES = {"y", "yes"}
NO = {"n", "no"}
COMMANDS = "/status /plan /cancel /resume <job_id> /files /provenance /jobs /quit"


class Console(Protocol):
    async def say(self, text: str) -> None: ...

    async def ask(self, prompt: str) -> str | None: ...


def _failure_text(envelope: dict[str, Any]) -> str:
    error = envelope.get("error") or {}
    text = f"{error.get('code', 'E_UNKNOWN')}: {error.get('message', '')}"
    return f"{text}. {error['hint']}" if error.get("hint") else text


class SessionDecisions:
    """The researcher's decisions during a run. Only this class (through the CLI's token) records
    a warning acknowledgement; the controller and the model never do."""

    def __init__(self, tools: ToolCaller, console: Console, token: str) -> None:
        self.tools = tools
        self.console = console
        self.token = token

    async def acknowledge_warnings(self, job_id: str, warnings: list[dict[str, Any]]) -> list[str]:
        accepted: list[str] = []
        for warning in warnings:
            reply = await self.console.ask(
                f"grompp warning {warning['id']}: {warning['text']} "
                f"({warning.get('source', '')}). Accept it and continue? [y/n] ")
            if reply is not None and reply.strip().lower() in YES:
                accepted.append(warning["id"])
        if not accepted:
            return []
        result = await self.tools.call("record_approval", {
            "job_id": job_id, "approval_token": self.token, "kind": "warnings",
            "warning_ids": accepted})
        if not result["ok"]:
            await self.console.say(_failure_text(result))
            return []
        return accepted

    async def on_interrupted(self, job_id: str, stage: str, cause: str,
                             can_resume: bool) -> str:
        options = ["resume", "restart", "stop"] if can_resume else ["restart", "stop"]
        prompt = f"{stage} was interrupted ({cause}). [{'/'.join(options)}] "
        for _ in range(3):
            reply = await self.console.ask(prompt)
            if reply is None:
                return "stop"
            if reply.strip().lower() in options:
                return reply.strip().lower()
        return "stop"


_LABELS = {
    "done": "all four stages passed their gates",
    "escalate": "stopped and needs your decision",
    "cancelled": "cancelled",
    "failed": "failed",
    "limit": "stopped at a limit",
    "detached": "left running in the background",
}


def final_message(outcome: Outcome, job: dict[str, Any]) -> str:
    """The closing message, from the outcome and the server's job data only (RULE-C-06): the
    model's own words never decide what is reported."""
    statuses = job.get("stage_status", {})
    lines = [f"Run finished: {_LABELS.get(outcome.status, outcome.status)} "
             f"(execution mode: {job.get('execution_mode', 'unknown')})",
             "Stages: " + ", ".join(f"{s} {statuses[s]}" for s in STAGE_ORDER[1:5]
                                    if s in statuses),
             f"Warnings acknowledged: {len(job.get('acknowledged_warnings', []))}"]
    retries = job.get("retries", [])
    if retries:
        lines.append("Automatic retries: " + "; ".join(
            f"{r['key']}={r['value']:g} on {r['stage']} (after a failure in "
            f"{r['failing_stage']})" for r in retries))
    else:
        lines.append("Automatic retries: none")
    extra = [d for d in outcome.disclosures if not d.startswith("Retried")]
    if extra:
        lines.append("During this session: " + "; ".join(extra))
    for failure in job.get("failures", []):
        lines.append(f"Failure recorded: {failure['stage']}: {failure['cause']}")
    if outcome.status != "done":
        lines.append(f"What stopped the run: {outcome.message}")
    not_verified = ["Analysis and the report were skipped: they are not part of this run"]
    if job.get("execution_mode") == "simulated":
        not_verified.append("physical gate checks were skipped (simulated mode: no real "
                            "physics ran)")
    lines.append("Not verified: " + "; ".join(not_verified) + ".")
    return "\n".join(lines)


def _production_ns(job: dict[str, Any]) -> float:
    """Planned production length in ns, from a ``get_job`` data dict."""
    for stage in job["plan"]["stages"]:
        if stage["name"] == "prod":
            params = stage["params"]
            return float(params["nsteps"] * params["dt"] / 1000)
    return 0.0


def _clock(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


class Session:
    def __init__(self, tools: ToolCaller, llm: ChatClient | None, cfg: Config, console: Console,
                 token: str, *, interactive: bool = True, read_tools: Sequence[Tool] = (),
                 system_prompt: str = "",
                 sleep: Callable[[float], Awaitable[None]] | None = None,
                 poll_interval_s: float = POLL_FLOOR_S) -> None:
        self.tools = tools
        self.cfg = cfg
        self.console = console
        self.token = token
        self.interactive = interactive
        self._sleep = sleep
        self._poll_interval = poll_interval_s
        self.job_id: str | None = None
        self.draft = RequestDraft()
        self.quit = False
        self._controller: Controller | None = None
        self._pump_active = False
        self._answer: asyncio.Future[str | None] | None = None
        self._totals: dict[str, float] = {}
        self._says: list[asyncio.Task[None]] = []
        self.convo = Conversation(
            llm, tools, cfg, system_prompt=system_prompt, read_tools=read_tools,
            job_summary=self._job_summary) if llm is not None else None

    # -- input and output ---------------------------------------------------------------------

    async def _say(self, text: str) -> None:
        await self.console.say(text)

    async def _ask(self, prompt: str) -> str | None:
        """Ask the researcher. During a run the reader task owns the console, so the answer
        arrives through it."""
        if not self._pump_active:
            return await self.console.ask(prompt)
        await self.console.say(prompt)
        self._answer = asyncio.get_running_loop().create_future()
        try:
            return await self._answer
        finally:
            self._answer = None

    def _job_summary(self) -> str:
        return f"job {self.job_id}" if self.job_id else "no job yet"

    # -- start ------------------------------------------------------------------------------

    async def start(self) -> bool:
        env = await self.tools.call("gmx_check_environment", {})
        if not env["ok"]:
            await self._say(_failure_text(env))
            return False
        data = env["data"]
        raw = data.get("probe")  # a dict for a real binary, text such as "skipped (simulated)"
        probe = raw if isinstance(raw, str) else ("ok" if (raw or {}).get("ok") else "failed")
        await self._say(f"SimAgent. GROMACS {data.get('version', '?')} verified "
                        f"({data.get('execution_mode', '?')} mode, probe run {probe}). "
                        "Describe your simulation.")
        return True

    # -- chat: intake, clarification, plan ----------------------------------------------------

    async def chat(self) -> None:
        while not self.quit:
            line = await self.console.ask("> ")
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                if not await self.handle_command(line):
                    break
                continue
            await self._intake(line)

    async def _intake(self, text: str) -> None:
        if self.convo is None:
            await self._say("No model is configured: set LUNA_API_KEY and LUNA_BASE_URL, or "
                            "use `simagent run --request-file`.")
            return
        initial = explicit_clarification_answers(
            text, missing_questions(self.draft, limit=len(SCIENCE_CRITICAL)))
        if initial:
            self.draft = merge_submission(self.draft, initial)
        message: str | None = text
        stalled_rounds = 0
        rejected_submissions = 0
        while True:
            extra: list[Question] = []
            if message is not None:
                turn = await self.convo.turn(message)
                message = None
                if turn.stop:
                    await self._say(turn.text or "")
                    return
                if turn.text:
                    await self._say(turn.text)
                extra = turn.questions
                if turn.submission:
                    try:
                        previous = self.draft
                        self.draft = merge_submission(self.draft, turn.submission)
                    except ValueError as exc:
                        message = f"submit_request was rejected: {exc}. Submit it again."
                        rejected_submissions += 1
                        if rejected_submissions > MAX_STALLED_CLARIFY_ROUNDS:
                            await self._say("I could not understand the request fields.")
                            return
                        continue
                    rejected_submissions = 0
                    if self.draft != previous:
                        stalled_rounds = 0
            questions = self._questions(extra)
            if not questions:
                break
            if stalled_rounds >= MAX_STALLED_CLARIFY_ROUNDS:
                await self._say("I could not complete the request after several rounds of "
                                "questions. Please restate it with the missing values, or use "
                                "`simagent run --request-file`.")
                return
            reply = await self._ask_questions(questions)
            if reply is None:
                return
            previous = self.draft
            tokens = reply.lower().split()
            if tokens and all(t in YES for t in tokens) and len(tokens) in (1, len(questions)):
                self.draft = accept(self.draft, questions)
            elif reply.strip():
                explicit = explicit_clarification_answers(reply, questions)
                if explicit:
                    self.draft = merge_submission(self.draft, explicit)
                if ("n_molecules" in {q.field for q in questions}
                        and "n_molecules" not in self.draft.values
                        and "maximum" in reply.lower()):
                    await self._say("I need a whole-number molecule count. SimAgent chooses the "
                                    "cubic box edge from that count, so it cannot enforce an "
                                    "exact box size.")
                message = reply
            else:
                await self._say("I need an answer to continue (yes accepts my proposals).")
            stalled_rounds = 0 if self.draft != previous else stalled_rounds + 1
        request = finalize(self.draft)
        self.draft = RequestDraft()
        await self._plan_and_run(request)

    def _questions(self, extra: Sequence[Question]) -> list[Question]:
        questions = missing_questions(self.draft)
        known = {q.field for q in questions}
        for question in extra:
            stated = self.draft.origins.get(question.field) == "user"
            if len(questions) < 3 and question.field not in known and not stated:
                questions.append(question)
                known.add(question.field)
        return questions[:3]

    async def _ask_questions(self, questions: Sequence[Question]) -> str | None:
        lines = ["Details I need (my proposal in brackets):"]
        lines += [f" {i}. {q.text} [{q.proposal}]" for i, q in enumerate(questions, 1)]
        block = "\n".join(lines)
        await self._say(block)
        if self.convo is not None:
            self.convo.note_assistant(block)
        return await self._ask("> ")

    async def _plan_and_run(self, request: dict[str, Any]) -> Outcome | None:
        try:
            prepared = await prepare_job(self.tools, request)
        except PlanError as exc:
            await self._say(str(exc))
            return None
        self.job_id = prepared.job_id
        for warning in prepared.warnings:
            await self._say(f"Note: {warning}")
        return await self._approve_and_run(prepared, auto=False)

    async def run_request(self, request: dict[str, Any], *, auto_approve: bool,
                          long_run_confirmed: bool = False) -> Outcome:
        """Plan, approve, and run a finished request (the non-interactive path)."""
        try:
            prepared = await prepare_job(self.tools, request)
        except PlanError as exc:
            await self._say(str(exc))
            return Outcome("failed", None, str(exc))
        self.job_id = prepared.job_id
        for warning in prepared.warnings:
            await self._say(f"Note: {warning}")
        outcome = await self._approve_and_run(prepared, auto=auto_approve,
                                              long_run_confirmed=long_run_confirmed)
        return outcome or Outcome("cancelled", None, "the plan was not approved")

    async def _approve_and_run(self, prepared: Prepared, *, auto: bool,
                               long_run_confirmed: bool = False) -> Outcome | None:
        while True:
            job = await self.tools.call("get_job", {"job_id": prepared.job_id})
            if not job["ok"]:
                await self._say(_failure_text(job))
                return None
            await self._say(render_plan(job["data"]))
            long_ns = _production_ns(job["data"])
            limit = self.cfg.workflow.max_production_ns_without_confirm
            is_long = long_ns > limit
            confirmed = long_run_confirmed
            mode = "auto"
            if is_long and auto and not confirmed:
                message = (f"The planned production run is {long_ns:g} ns, above the {limit:g} "
                           "ns limit. Pass --confirm-long-run to approve it automatically.")
                await self._say(message)
                return Outcome("failed", None, message)
            if not auto:
                answer = await self._ask("Approve? [y/n/edit] ")
                reply = (answer or "").strip().lower()
                if answer is None or reply in NO or reply == "":
                    await self._say(f"Not approved. Job {prepared.job_id} stays planned; "
                                    f"/resume {prepared.job_id} reopens it.")
                    return None
                if reply == "edit":
                    prepared = await self._edit(prepared)
                    continue
                if reply not in YES:
                    await self._say("Please answer y, n, or edit.")
                    continue
                mode = "interactive"
                if is_long and not confirmed:
                    sure = await self._ask(
                        f"This is a {long_ns:g} ns production run (above the {limit:g} ns "
                        "limit). Confirm the long run? [y/n] ")
                    if sure is None or sure.strip().lower() not in YES:
                        await self._say(f"Not approved. Job {prepared.job_id} stays planned.")
                        return None
                    confirmed = True
            try:
                await record_plan_approval(self.tools, self.token, prepared.job_id, mode,
                                           shown=job["data"], long_run_confirmed=confirmed)
            except PlanError as exc:
                await self._say(str(exc))
                return None
            return await self.execute(prepared.job_id)

    async def _edit(self, prepared: Prepared) -> Prepared:
        reply = await self._ask("Change (for example prod_ns=2, temperature_K=310): ")
        if reply is None:
            return prepared
        changes: dict[str, Any] = {}
        try:
            for item in reply.replace(",", " ").split():
                name, _, text = item.partition("=")
                if not text:
                    raise ValueError(f"{item!r} is not name=value")
                changes[name] = parse_value(name, text) if name in SCIENCE_CRITICAL or name in (
                    "emtol", "nvt_ps", "npt_ps", "dt_ps", "tau_t", "tau_p", "seed") else text
            return await replan(self.tools, prepared, changes)
        except (ValueError, PlanError) as exc:
            await self._say(f"Could not apply that change: {exc}")
            return prepared

    # -- execution ----------------------------------------------------------------------------

    def _event(self, event: dict[str, Any]) -> None:
        line = self._event_line(event)
        if line:
            self._says.append(asyncio.ensure_future(self.console.say(line)))

    def _event_line(self, event: dict[str, Any]) -> str | None:
        stage = event.get("stage")
        label = f"[{STAGE_IDS.get(str(stage), '')} {stage}]"
        state = event["state"]
        if state == "PROGRESS":
            run = event["run"]
            time_ps = run.get("time_ps")
            if time_ps is None:
                return f"{label} {run.get('state', 'running')}"
            total = self._totals.get(str(stage))
            parts = [label + (f" {int(time_ps / total * 100)}%" if total else ""),
                     f"{time_ps:.1f} ps"]
            if run.get("ns_per_day") is not None:
                parts.append(f"{run['ns_per_day']:g} ns/day")
            if run.get("eta_s") is not None:
                parts.append(f"ETA {_clock(run['eta_s'])}")
            return " | ".join(parts)
        if state == "DIAGNOSE":
            return f"{label} failed; reading the logs"
        if state == "RETRY":
            retry = event.get("retry", {})
            return f"{label} retrying with {retry.get('key')}={retry.get('value')}"
        return None

    async def execute(self, job_id: str) -> Outcome:
        """Run the stored, approved plan through the controller, reading commands meanwhile."""
        self.job_id = job_id
        job = await self.tools.call("get_job", {"job_id": job_id})
        self._totals = {}
        if job["ok"] and job["data"].get("plan"):
            for stage in job["data"]["plan"]["stages"]:
                params = stage["params"]
                if stage["name"] in STAGE_IDS and "dt" in params:
                    self._totals[stage["name"]] = params["nsteps"] * params["dt"]
        controller = Controller(
            self.tools, self.cfg, SessionDecisions(self.tools, _Routed(self), self.token),
            sleep=self._sleep, poll_interval_s=self._poll_interval, on_event=self._event)
        self._controller = controller
        task = asyncio.ensure_future(controller.run(job_id))
        pump = asyncio.ensure_future(self._pump()) if self.interactive else None
        self._pump_active = pump is not None
        try:
            waiting: set[asyncio.Future[Any]] = {task} if pump is None else {task, pump}
            await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            if task.done():
                outcome = task.result()
            else:  # the researcher quit or the input closed: the run keeps going without us
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                await self._say(f"Leaving job {job_id} running in the background. "
                                f"Start simagent again and use /resume {job_id} to follow it.")
                self.quit = True
                return Outcome("detached", None, "the session ended while the run continued")
        finally:
            self._pump_active = False
            if pump is not None:
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump
            self._controller = None
            if self._says:
                await asyncio.gather(*self._says, return_exceptions=True)
                self._says = []
        final = await self.tools.call("get_job", {"job_id": job_id})
        await self._say(final_message(outcome, final["data"] if final["ok"] else {}))
        return outcome

    async def _pump(self) -> None:
        """The one reader while a run is in progress: answers go to a pending prompt, anything
        else is a command."""
        while True:
            line = await self.console.ask("")
            if line is None:
                if self._answer is not None and not self._answer.done():
                    self._answer.set_result(None)
                return
            stripped = line.strip()
            if self._answer is not None and not self._answer.done():
                if stripped not in ("/quit", "/cancel"):
                    self._answer.set_result(line)
                    continue
                if stripped == "/cancel":
                    self._answer.set_result(None)  # the prompt gets no answer
                if not await self.handle_command(stripped):
                    return
            elif line.strip().startswith("/"):
                if not await self.handle_command(line.strip()):
                    return
            elif line.strip():
                await self._say(f"A run is in progress. Commands: {COMMANDS}")

    # -- commands -----------------------------------------------------------------------------

    async def handle_command(self, line: str) -> bool:
        try:
            return await self._dispatch(line)
        except Exception as exc:  # one failing command must not end the session
            await self._say(f"Command failed: {type(exc).__name__}: {exc}")
            return True

    async def _dispatch(self, line: str) -> bool:
        name, _, argument = line.partition(" ")
        argument = argument.strip()
        if name == "/quit":
            self.quit = True
            return False
        if name == "/jobs":
            await self._jobs()
        elif name == "/resume":
            await self._resume(argument)
        elif name == "/cancel":
            await self._cancel()
        elif name in ("/status", "/plan", "/files", "/provenance"):
            if self.job_id is None:
                await self._say("No job yet. Describe a simulation first, or /resume <job_id>.")
            else:
                await getattr(self, f"_{name[1:]}")(self.job_id)
        else:
            await self._say(f"Unknown command {name}. Commands: {COMMANDS}")
        return True

    async def _job(self, job_id: str) -> dict[str, Any] | None:
        job = await self.tools.call("get_job", {"job_id": job_id})
        if not job["ok"]:
            await self._say(_failure_text(job))
            return None
        data: dict[str, Any] = job["data"]
        return data

    async def _status(self, job_id: str) -> None:
        job = await self._job(job_id)
        if job is not None:
            await self._say(f"Job {job_id} ({job.get('execution_mode')}): " + ", ".join(
                f"{s} {job['stage_status'][s]}" for s in STAGE_ORDER if s in job["stage_status"]))

    async def _plan(self, job_id: str) -> None:
        job = await self._job(job_id)
        if job is not None:
            await self._say(render_plan(job))

    async def _files(self, job_id: str) -> None:
        listing = await self.tools.call("list_artifacts", {"job_id": job_id})
        if not listing["ok"]:
            await self._say(_failure_text(listing))
            return
        await self._say("\n".join(
            f"{f['path']}  {f['bytes']} bytes  {'receipt' if f['has_receipt'] else 'no receipt'}"
            for f in listing["data"]["files"]) or "No files yet.")

    async def _jobs(self) -> None:
        listing = await self.tools.call("list_jobs", {})
        if not listing["ok"]:
            await self._say(_failure_text(listing))
            return
        await self._say("\n".join(
            f"{j['job_id']}  {j['status']}  {j['title']}  ({j['execution_mode']})"
            for j in listing["data"]["jobs"]) or "No jobs yet.")

    async def _provenance(self, job_id: str) -> None:
        listing = await self.tools.call("list_jobs", {})
        path = next((j["path"] for j in listing["data"]["jobs"] if j["job_id"] == job_id), None) \
            if listing["ok"] else None
        if path is None:
            await self._say(f"Cannot find the directory of job {job_id}.")
            return
        rows = [f"#{r['seq']} {' '.join(r['argv'])}  exit {r.get('exit_code')}"
                for r in Ledger(Path(path) / "provenance.jsonl").records()
                if r.get("event") == "end" and r.get("kind") == "gmx" and r.get("argv")]
        await self._say("\n".join(rows) or "No gmx commands recorded yet.")

    async def _cancel(self) -> None:
        if self._controller is None or self.job_id is None:
            await self._say("Nothing is running.")
            return
        result = await self._controller.cancel(self.job_id)
        await self._say("Cancelling the running stage." if result.get("ok")
                        else _failure_text(result))

    async def _resume(self, job_id: str) -> None:
        if not job_id:
            await self._say("Usage: /resume <job_id>")
            return
        if self._controller is not None:
            await self._say("A run is in progress. Cancel it or wait for it to finish, then "
                            "/resume.")
            return
        job = await self._job(job_id)
        if job is None:
            return
        self.job_id = job_id
        if not job.get("plan"):
            await self._say(f"Job {job_id} has no stored plan (planning did not finish). "
                            "Describe the simulation again to start over.")
            return
        prepared = Prepared(job_id, job["request"], str(job["plan_sha256"]), [],
                            str(job.get("execution_mode", "real")))
        if (job.get("approval") or {}).get("matches_plan"):
            await self.execute(job_id)
        else:
            await self._approve_and_run(prepared, auto=False)


class _Routed:
    """A console whose ``ask`` goes through the session, so a prompt raised during a run is
    answered by the reader task."""

    def __init__(self, session: Session) -> None:
        self._session = session

    async def say(self, text: str) -> None:
        await self._session.console.say(text)

    async def ask(self, prompt: str) -> str | None:
        return await self._session._ask(prompt)
