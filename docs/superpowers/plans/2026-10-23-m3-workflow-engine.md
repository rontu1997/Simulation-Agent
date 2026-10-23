# M3 Workflow Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic workflow controller and the playbook diagnostician that run an approved plan through EM, NVT, NPT, and production over the MCP tools, diagnose failures, apply bounded retries, and resume or reattach to interrupted runs (TASK-030, TASK-031, TASK-032).

**Architecture:** `agent/diagnostician.py` matches playbook entries (a shipped JSON resource) against evidence the controller collects and returns the next retry step. `agent/controller.py` is a state-driven async loop over any object with `async call(name, args) -> envelope` (the real `McpSession`, or a fake in tests): it reads `get_job`, and for each process stage does whatever its stored status calls for, so a fresh run, a retry, a resume, and a reattach are the same code path. The researcher-facing decisions (acknowledging warnings, resume or restart) go through a `Decisions` object the CLI will implement in M4; the controller never calls `record_approval`.

**Tech Stack:** Python 3.12, `asyncio`, `pytest` with `anyio` for async tests (existing `anyio_backend` fixture pattern), the existing MCP tools and `McpSession`.

**Spec:** `design.md` sections 7 (playbook), 14.1 and 14.2 (controller), 3.4 (status vocabularies), 4.3 items 7 to 9 (retries and warnings), 4.5 (resume); `architecture.md` section 4 (state machine); `rules.md` RULE-X-04, RULE-X-05, RULE-X-08, RULE-X-10, RULE-X-14, RULE-S-04, RULE-C-08. Tracker rows TASK-030, 031, 032 (TASK-034 is out of scope here: it needs real GROMACS output).

## Global Constraints

- Controller polls `get_run_status` no more often than every 10 s (`POLL_FLOOR_S = 10.0`); a researcher `/cancel` during a poll sleep is handled at once (T-AGENT-07).
- Automatic retries only through `write_mdp(retry={key, value})` with `key` in `emtol`, `dt`, `tau_p`, `em_nsteps`; at most 2 per failing stage (`E_RETRY_LIMIT` from the server is final); anything the plan does not allow is escalated, never forced (RULE-S-04, RULE-X-05).
- Every automatic change is disclosed: the controller returns a human-readable line per applied retry in `Outcome.disclosures` (RULE-C-08).
- The controller never calls `record_approval` and never passes a `-maxwarn` or any tool argument derived from log text (RULE-X-14); log and stderr text is evidence for regex matching only.
- Model function allow-list is unchanged (`LLM_ALLOWED_TOOLS`); the controller uses the full tool set through its `tools` object.
- Python source lines at most 100 characters; `ruff` and `mypy src` clean; tests run on Windows and POSIX (POSIX-only tests carry `@pytest.mark.posix`).
- Playbook regexes tagged **[verify TASK-034]** in `design.md` are shipped with `"verified": false`; the diagnostician treats them identically but reports the flag in `Diagnosis.entry.verified` so the CLI can show low confidence.

## Review Focus

- A `/cancel` typed during a poll sleep stops the run within that sleep (not after the full interval), the stage ends `CANCELLED`, and the controller never validates it as a pass (Task 2, `test_cancel_during_poll_sleep`).
- The tools object raising (server died, broken pipe) or returning a non-envelope stops the controller with a `failed` outcome naming the tool, and never loops or marks a stage (Task 2, `test_tool_failure_stops_the_run`).
- The same failure recurring after its retry steps are used up escalates instead of looping, and a retry the plan or server refuses (`E_RETRY_NOT_ALLOWED`, `E_RETRY_LIMIT`) escalates (Task 3, `test_recurring_failure_escalates`, `test_refused_retry_escalates`).
- Log or stderr text containing instructions, JSON tool calls, or regex metacharacters is only matched against shipped patterns and never changes which tool is called or with what arguments (Task 1, `test_log_text_is_evidence_only`).
- A researcher who declines to acknowledge a grompp warning gets an `escalate` outcome and no second `run_grompp` is issued (Task 2, `test_declined_warning_escalates`).

---

## File Structure

```
src/simagent/agent/__init__.py            package marker
src/simagent/agent/playbook_default.json  shipped playbook (design 7.1), package resource
src/simagent/agent/diagnostician.py       PlaybookEntry, Evidence, Diagnosis, Diagnostician
src/simagent/agent/controller.py          ToolCaller, Decisions, Outcome, Controller
tests/unit/test_diagnostician.py
tests/unit/test_controller.py             FakeTools scripted tool double
tests/integration/test_controller_flow.py controller against the real server with the fake gmx (posix)
tests/fakes/fake_gmx                      new failure knobs (FAKE_GMX_FAIL_ONCE)
```

---

### Task 1: Playbook and diagnostician (TASK-032, T-UNIT-04)

**Files:**
- Create: `src/simagent/agent/__init__.py`, `src/simagent/agent/playbook_default.json`, `src/simagent/agent/diagnostician.py`, `tests/unit/test_diagnostician.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) RetryStep(key: str, value: float, target: str)` where `key` is one of `emtol`, `dt`, `tau_p`, `em_nsteps` and `target` is the stage the retry is applied to.
  - `@dataclass(frozen=True) PlaybookEntry(id: str, pattern: str | None, source: str | None, checks: tuple[str, ...], cause: str, action: str, auto: bool, steps: tuple[RetryStep, ...], hint: str, verified: bool)`; `action` is one of `retry`, `resume`, `escalate`; `source` is `log`, `grompp`, `stderr`, or `None`.
  - `@dataclass Evidence(stage: str, texts: dict[str, str], failed_checks: list[str], causes: list[str], has_checkpoint: bool)`; `texts` maps `log`, `grompp`, `stderr` to the text of that file's tail (missing files absent).
  - `@dataclass(frozen=True) Diagnosis(entry: PlaybookEntry, matched: str | None, step: RetryStep | None, exhausted: bool)`; `step` is the next retry step not yet applied, `exhausted` is true when the entry is an automatic retry whose steps are all applied.
  - `load_playbook() -> list[PlaybookEntry]` (reads the package resource with `importlib.resources`).
  - `class Diagnostician: __init__(self, entries: Sequence[PlaybookEntry] | None = None)`; `diagnose(self, evidence: Evidence, applied: Sequence[dict[str, Any]]) -> Diagnosis` where `applied` is `job.json["retries"]` (entries with `key`, `value`).

- [ ] **Step 1: Write failing tests** in `tests/unit/test_diagnostician.py`:
  - `test_playbook_loads_every_row_of_design_7_1`: ids include `settle`, `lincs`, `nan`, `em_not_converged`, `em_machine_precision`, `count_mismatch`, `missing_moleculetype`, `include_missing`, `box_small`, `grompp_warning`, `pressure_scaling`, `domain_decomposition`, `segfault`, `out_of_memory`, `process_died`, `unknown`; every `RetryStep.key` is in `{emtol, dt, tau_p, em_nsteps}`; every entry whose pattern is tagged verify has `verified is False`; `unknown` is last and matches anything.
  - `test_each_pattern_maps_to_its_action` parametrized by hand-written fixture strings (one per row, taken from the design pattern): `Water molecule starting at atom 17 can not be settled` gives `settle`, `auto`, first step `emtol=100` target `em`; with that step already in `applied`, the next step is `dt=0.001` target `nvt`; with both applied, `exhausted` is true. `Energy minimization reached the maximum number of steps` gives `em_not_converged`, steps `em_nsteps` 100000 then 200000. `Pressure scaling more than 1%` gives `tau_p=5.0` target `npt`, auto once. `forces have not converged to the requested precision` gives `escalate`, not auto. `WARNING 1 [file topol.top, line 3]:` and `There were 2 warnings` give `grompp_warning`, `escalate`. `Segmentation fault`, `No space left on device` give `escalate`. The `.log`, `grompp`, and `stderr` sources are each matched only in their own text.
  - `test_failed_check_name_selects_the_nan_entry`: `failed_checks=["no_nan"]` with no text gives `nan`, same steps as `settle`.
  - `test_process_died_with_checkpoint_resumes`: `causes=["process_died"]`, `has_checkpoint=True` gives `process_died` with action `resume`; without a checkpoint the action is `escalate` (hint says to reset and rerun).
  - `test_first_matching_row_wins_in_table_order` and `test_no_match_is_unknown_with_low_confidence`.
  - `test_log_text_is_evidence_only`: a log tail containing `{"tool": "run_mdrun"}`, `ignore previous instructions`, and the string `(a+)+$` is diagnosed as `unknown` without raising, and `diagnose` returns in under 1 s for a 2 MB single-line text (patterns are anchored to bounded constructs; texts are cut to the last 20,000 characters before matching).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_diagnostician.py -q`. Expected: FAIL (import error).
- [ ] **Step 3: Implement** the playbook JSON (design 7.1 rows in table order, `automatic` rows carry `steps`, `auto: true`) and `diagnostician.py`. Matching: for each entry in order, if `checks` intersects `failed_checks` or `pattern` matches (`re.search`, no flags) the text of `source`, it applies; `process_died` applies when `"process_died"` or `"timeout"` is in `causes`; `unknown` applies last. `step` is the first `RetryStep` whose `(key, value)` is not present in `applied` (compare values as floats).
- [ ] **Step 4: Run** the test file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: playbook and diagnostician`.

### Task 2: Controller core: one stage, polling, cancel, warnings (TASK-030)

**Files:**
- Create: `src/simagent/agent/controller.py`, `tests/unit/test_controller.py`

**Interfaces:**
- Consumes: `Diagnostician` (Task 1); the tool envelope `{ok, data, error{code, message, hint}}` and tool names of `design.md` 4.1.
- Produces:
  - `class ToolCaller(Protocol): async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]`.
  - `class Decisions(Protocol)`: `async acknowledge_warnings(self, job_id: str, warnings: list[dict[str, Any]]) -> list[str]` (returns the ids the researcher accepted; the CLI records the approval), `async on_interrupted(self, job_id: str, stage: str, cause: str, can_resume: bool) -> str` returning `resume`, `restart`, or `stop`. `class NoDecisions` implements both with "nothing acknowledged" and `stop`.
  - `@dataclass Outcome(status: str, stage: str | None, message: str, diagnosis: Diagnosis | None = None, disclosures: list[str] = field(default_factory=list))`; `status` in `done`, `escalate`, `cancelled`, `failed`, `limit`.
  - `POLL_FLOOR_S = 10.0`; `class Controller: __init__(self, tools: ToolCaller, cfg: Config, decisions: Decisions | None = None, *, sleep: Callable[[float], Awaitable[None]] | None = None, poll_interval_s: float = 10.0, max_tool_calls: int = 500, max_rounds: int = 12, on_event: Callable[[dict[str, Any]], None] | None = None, diagnostician: Diagnostician | None = None)`.
  - `async Controller.run(self, job_id: str) -> Outcome` and `async Controller.cancel(self, job_id: str) -> dict[str, Any]`.
  - Private: `async _call(name, args) -> dict` counts calls (above `max_tool_calls` raises `_Stop(limit)`), wraps any exception or envelope without `ok` into `_Stop(failed, "tool <name> failed: ...")`, and returns the envelope (callers inspect `ok`).

- [ ] **Step 1: Write failing tests** in `tests/unit/test_controller.py` using a scripted `FakeTools` (a dict of tool name to a list of envelopes or callables, recording `(name, args)` calls and raising on unscripted calls) and a fake `sleep` that records intervals and advances nothing:
  - `test_start_check_failure_stops_before_any_job_call`: `gmx_check_environment` returns `E_GMX_NOT_FOUND`; outcome `failed`, message contains the server's message and hint, only one call made.
  - `test_runs_a_pending_stage_in_order`: job with `em` `PENDING`; calls in order `get_job`, `write_mdp(stage=em)`, `run_grompp`, `run_mdrun(resume=False)`, `get_run_status` (`running`, then `finished`), `validate_stage` (passed); events carry workflow state `EXECUTE`.
  - `test_polling_floor`: the recorded sleeps are all `>= 10.0` even when `poll_interval_s=0.1` is passed (T-AGENT-07 slice).
  - `test_cancel_during_poll_sleep`: the default sleeper waits on the controller's cancel event; calling `cancel(job_id)` while the controller sleeps issues `cancel_run` for the `RUNNING` stage, wakes the sleep at once (assert elapsed under 1 s with a real 10 s interval), and `run` returns `cancelled` without calling `validate_stage`.
  - `test_tool_failure_stops_the_run`: the tools object raises `BrokenPipeError` on `run_mdrun`; outcome `failed`, message names `run_mdrun`; and a non-envelope return gives the same. The call count never exceeds the script.
  - `test_call_budget_stops_with_limit`: `max_tool_calls=3` gives `limit`.
  - `test_warnings_flow`: `run_grompp` first returns `E_GROMPP_FAILED` with `data.warnings=[{id: "w1", ...}]`; `Decisions.acknowledge_warnings` returns `["w1"]`; the controller does not call `record_approval` itself (the decisions object does) and calls `run_grompp` a second time.
  - `test_declined_warning_escalates`: acknowledge returns `[]`; outcome `escalate`, exactly one `run_grompp` call, message lists the warning texts.
  - `test_stage_already_passed_is_skipped_and_ready_goes_straight_to_mdrun`.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_controller.py -q`. Expected: FAIL (import error).
- [ ] **Step 3: Implement** `Controller` for the stage loop only: `run` checks the environment, reads `get_job`, and for each of `em`, `nvt`, `npt`, `prod` dispatches on the stored status (`PASSED` skip; `PENDING` write_mdp then grompp then mdrun; `READY` mdrun; `RUNNING` poll), polls with the injected `sleep` (default: wait on an `asyncio.Event` with timeout `max(poll_interval_s, POLL_FLOOR_S)`, which `cancel` sets), then calls `validate_stage`. A failed validation returns `Outcome("escalate", ...)` for now (Task 3 replaces it with diagnosis). Emit events `{"state": "EXECUTE"|"ESCALATE"|"DONE"|"FAILED"|"CANCELLED", "stage": ..., "job_id": ...}` through `on_event`. After `prod` passes return `Outcome("done", "prod", ...)`.
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: workflow controller core`.

### Task 3: Diagnosis, automatic retry, escalation (TASK-030, TASK-032)

**Files:**
- Modify: `src/simagent/agent/controller.py`, `tests/unit/test_controller.py`

**Interfaces:**
- Consumes: `Diagnostician.diagnose`, `Evidence`, `RetryStep` (Task 1); `get_log_excerpt(job_id, stage, which, lines=40)`; `write_mdp(job_id, stage, retry={key, value}, failing_stage)`.
- Produces: `async Controller._diagnose(job_id, stage, validation, job) -> Outcome | None` (returns `None` when a retry was applied and the loop should restart from the first non-`PASSED` stage); a `RETRY` event and a disclosure line such as `Retried EM with emtol=100 after the SETTLE failure in nvt (automatic, 1 of 2)`.

- [ ] **Step 1: Write failing tests**:
  - `test_settle_failure_applies_the_first_step_and_reruns`: `nvt` validation fails (`process_died` check with stderr text matching the SETTLE row); the controller reads three log excerpts (`log`, `grompp`, `stderr`), calls `write_mdp(stage="em", retry={"key": "emtol", "value": 100}, failing_stage="nvt")`, then the next `get_job` shows `em` `PENDING` and the loop re-runs `em`, `nvt`; `Outcome.disclosures` has the retry line.
  - `test_second_occurrence_uses_the_second_step`: with `job.retries` already holding `emtol=100`, the next step is `dt=0.001` targeting `nvt`.
  - `test_recurring_failure_escalates`: both steps already in `job.retries` gives `escalate` with the matched entry's cause and the log excerpt, no `write_mdp` call.
  - `test_refused_retry_escalates`: `write_mdp` returns `E_RETRY_NOT_ALLOWED` (and separately `E_RETRY_LIMIT`); outcome `escalate` with the server's message.
  - `test_non_auto_entry_escalates_with_explanation`: the `em_machine_precision` row gives `escalate` and the message mentions a plan edit.
  - `test_grompp_failure_without_warnings_is_diagnosed`: `E_GROMPP_FAILED` with grompp text matching `count_mismatch` gives `escalate` (needs new approval), no retry.
  - `test_max_rounds_guard`: a script where every round "succeeds" at retrying but the stage fails again stops with `limit` after `max_rounds` rounds.
  - `test_failures_in_playbook_are_never_protected_keys`: every `write_mdp` retry the controller issues across all tests has `key` in the four allowed keys (assert on the recorded calls).
- [ ] **Step 2: Run** the file. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** `_diagnose`: build `Evidence` from the failed `validate_stage` checks (names with status in `fail`, `no_receipt`, `not_run`), `job.failures` causes for the stage, three log tails fetched with `get_log_excerpt` (a missing file, `E_ANALYSIS_INPUT`, is just absent), and `has_checkpoint` from `list_artifacts(stage)` containing `<stage>.cpt`; call `Diagnostician.diagnose(evidence, job["retries"])`; for `action == "retry"` with a `step`, call `write_mdp` with `failing_stage=<failing stage>`; any `ok: false` becomes `escalate`. The `run` loop counts rounds (`max_rounds`) and restarts the stage scan after a retry.
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: controller diagnosis and bounded retries`.

### Task 4: Resume, reattach, limits (TASK-031, TASK-030)

**Files:**
- Modify: `src/simagent/agent/controller.py`, `tests/unit/test_controller.py`

**Interfaces:**
- Consumes: `Decisions.on_interrupted`; `run_mdrun(resume=True)`; `write_mdp(reset=True)`.
- Produces: no new public names; `Controller.run` handles `FAILED` (process died or timed out) and `CANCELLED` stages and `RUNNING` stages with a dead wrapper.

- [ ] **Step 1: Write failing tests**:
  - `test_reattach_to_a_running_stage_polls_without_restarting`: `prod` is `RUNNING` in `get_job`; the controller never calls `write_mdp`, `run_grompp`, or `run_mdrun`, polls until `finished`, validates, and returns `done` (T-INT-03 reattach, unit level).
  - `test_died_process_moves_to_failed_through_validate_then_offers_resume`: `get_run_status` reports `died`; `validate_stage` returns the `process_died` failure; with a checkpoint listed and `on_interrupted` returning `resume`, the controller calls `run_mdrun(resume=True)` and polls on; with `restart` it calls `write_mdp(reset=True)` then the full stage sequence; with `stop` it returns `escalate`.
  - `test_died_without_checkpoint_cannot_offer_resume`: `can_resume` passed as `False`; `resume` answer from decisions is ignored in favour of reset.
  - `test_cancelled_stage_found_on_reattach`: stage `CANCELLED` calls `on_interrupted(..., cause="cancelled", ...)` and follows the answer.
  - `test_failed_gate_after_finished_run_is_not_resumed`: a gate failure never calls `run_mdrun(resume=True)` (design 3.4).
  - `test_resume_counts_against_the_round_limit` (a run that dies every time stops with `limit`).
- [ ] **Step 2: Run** the file. Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the interrupted-stage branch described by the tests; the `died` observation always calls `validate_stage` first so the stage reaches `FAILED` with a recorded cause before any resume.
- [ ] **Step 4: Run** the file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: controller resume and reattach`.

### Task 5: End-to-end against the real server, tracker (T-INT-03 and T-INT-09 slices)

**Files:**
- Modify: `tests/fakes/fake_gmx`; Create: `tests/integration/test_controller_flow.py`; Modify: `tracker.md`

**Interfaces:**
- Consumes: `McpSession`, the helpers in `tests/integration/mcp_flow.py`, `Controller`, `Decisions`.
- Produces: fake binary knobs `FAKE_GMX_FAIL_ONCE=<stage>:<kind>` (`settle` writes the SETTLE text to stderr and exits 1 the first time that stage's `mdrun` runs, recorded by a marker file `.fail_once_done` in the stage directory's parent) and `FAKE_GMX_MDRUN_SLEEP` (existing).

- [ ] **Step 1: Write failing tests** (all `@pytest.mark.anyio` and `@pytest.mark.posix`, executed on Linux or WSL):
  - `test_controller_runs_all_four_stages_in_simulated_mode`: create_job, build, set_plan, approve (the test records approval with its token), then `Controller(session, cfg).run(job_id)` returns `done`; `get_job` shows `em`, `nvt`, `npt`, `prod` `PASSED`.
  - `test_settle_failure_is_retried_automatically`: with `FAKE_GMX_FAIL_ONCE=nvt:settle`, the run returns `done`, `job.retries` holds `emtol=100` attributed to `nvt`, and the disclosure line is present. Note: the fake `mdrun` must exit non-zero before writing outputs so `validate_stage` sees `process_died`.
  - `test_warning_acknowledgement_flow` with `FAKE_GMX_WARNINGS=1` and a `Decisions` that calls `record_approval(kind=warnings)` with the test token.
  - `test_kill_and_resume` (T-INT-09 slice with the fake binary): start `prod` with `FAKE_GMX_MDRUN_SLEEP=20`, SIGKILL the wrapper and the `gmx` child using the pids in `run.json`, run the controller with `on_interrupted -> resume`; the stage ends `PASSED`, `run_history/1/` exists. A `real_gmx` variant that checks `gmx check` frame continuity is added as `test_resume_continuity_real` (skipped here).
  - `test_reattach_after_controller_restart`: a first controller is cancelled (task cancelled) mid-poll, a second `Controller.run` on a new session finishes the job without a second `run_mdrun` call (assert via the ledger: one `start` record per stage).
- [ ] **Step 2: Run** `uv run pytest tests/integration/test_controller_flow.py -v` in WSL (`UV_PROJECT_ENVIRONMENT=$HOME/.venvs/simagent`). Expected: FAIL first (knob missing), SKIPPED on Windows.
- [ ] **Step 3: Implement** the fake knobs and fix whatever the flows reveal in earlier code (record each as a ledger ruling).
- [ ] **Step 4: Run** `uv run pytest -q`, `uv run ruff check .`, `uv run mypy src` on Windows and the full suite in WSL. Expected: all pass, zero lint and type errors.
- [ ] **Step 5: Update `tracker.md`**: TASK-030, TASK-031, TASK-032 Done with test names; TASK-034 stays Not started with a note that the shipped playbook entries are `verified: false` until real fixtures are recorded; snapshot row M3 and the next action. Commit `docs: mark M3 tasks done in tracker`, then `test: controller flows against the fake binary`.
