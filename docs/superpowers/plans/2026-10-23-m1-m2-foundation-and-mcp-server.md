# SimAgent M1 + M2 Implementation Plan (foundation and MCP server)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build milestones M1 (foundation) and M2 (MCP server) of SimAgent: configuration, provenance, the `gmx` runner and environment check, LLM and MCP clients, and 15 of the 21 MCP tools with server-side approval, stage state, and gates.

**Architecture:** A Python package `simagent` (src layout). `core/` holds pure logic and the `gmx` runner; `mcp_server/` registers tools that return one envelope shape; the LLM and MCP clients are thin and testable against stubs. Everything that decides pass or fail, approval, or provenance lives in server code.

**Tech Stack:** Python 3.11+ (3.12 here), `uv`, `mcp>=2,<3` (server class `MCPServer`), `openai`, `pydantic`, `pyyaml`, `jinja2`, `numpy`, `matplotlib`, `filelock`; dev: `pytest`, `pytest-cov`, `ruff`, `mypy`.

**Spec:** `design.md` (v1.8) is the build contract; `prd.md` (requirements and acceptance criteria), `architecture.md` (components), `rules.md` (guardrails), `tracker.md` (task IDs and decisions) support it. Section references below (for example "design 4.3 item 6") point into `design.md`. Where this plan and `design.md` disagree, `design.md` wins and the executor ledgers a ruling.

## Global Constraints

- Python `>=3.11`; MCP SDK `mcp>=2,<3`; the server class is `MCPServer` in `mcp.server.mcpserver` (not `FastMCP`); the client uses `ClientSession`, `stdio_client`, `StdioServerParameters`.
- GROMACS binary default path (config default only, no other module may contain the literal): `/home/rontu/Rontu/software/software_install/gromacs-2025.3/bin/gmx`.
- LLM settings from environment only: `LUNA_API_KEY`, `LUNA_BASE_URL`, `LUNA_MODEL` (default `gpt6-luna`). Approval token `SIMAGENT_APPROVAL_TOKEN`. Other env: `GMX_BIN`, `GMX_RUNS_DIR`, `GMX_NTOMP`, `GMX_NTMPI`, `SIMAGENT_REQUIRE_APPROVAL`, `SIMAGENT_TOKEN_BUDGET`, `SIMAGENT_ALLOW_FAKE`, `SIMAGENT_ALLOW_UNAPPROVED`, `SIMAGENT_LOG_PROMPTS`, `SIMAGENT_MEMORY_DIR`, `SIMAGENT_USER`. Secrets are never written to disk, logs, ledger `argv`, `repr`, or tool results.
- `gmx` and every child process are started with an argument list and `shell=False`. `ruff` rules `S602` and `S605` stay enabled.
- Tool envelope: `{"ok": bool, "data": {...}, "error": {"code", "message", "hint"}}`; error codes are exactly those of design 4.2 (as `ErrorCode`). Check order (design 4.2): argument validation, `E_JOB_NOT_FOUND`, `E_BINARY_CHANGED` (grompp/mdrun), `E_APPROVAL_UNAVAILABLE`, `E_NOT_APPROVED`, `E_MDRUN_RUNNING`, `E_STAGE_ORDER`, `E_STAGE_STATE`, `E_RETRY_*`, `E_INPUT_MODIFIED`.
- Stages: `build`, `em`, `nvt`, `npt`, `prod`, `analysis`, in directories `01_build`, `02_em`, `03_nvt`, `04_npt`, `05_prod`, `06_analysis`. Stage statuses: `PENDING, READY, RUNNING, GATING, PASSED, FAILED, CANCELLED`.
- Units: ps, nm, K, bar, kJ/mol. Defaults: 1,000 TIP4P waters, OPLS-AA, 300 K, 1 bar, dt 0.002 ps, cutoffs 1.0 nm.
- Platform: the authoring host is Windows; the target is Linux. Tests that need POSIX features (process groups, `/proc`, executing a `#!` script) carry `@pytest.mark.posix` and skip on `win32`; tests that need the real GROMACS carry `@pytest.mark.real_gmx` and skip unless `GMX_BIN` runs `--version`. Both kinds must pass on the Linux host.
- Every commit message ends with the line `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`.
- Run tests with `uv run pytest <path> -v` from the repository root.

## Review Focus

- A `job_id` such as `../../x`, `a/b`, `.hidden`, or `a..b` sent to any tool never touches the filesystem outside the runs directory and returns `E_PATH_ESCAPE` (Tasks 3 and 10).
- `GMX_BIN` that is missing, a directory, a text file, a script without the fake flag, or a wrong-version binary gives an actionable error code and message, never a traceback (Task 7).
- `LUNA_API_KEY` and the approval token never appear in events, ledger `argv`, `repr(Config)`, tool results, or the environment passed to `gmx` (Tasks 2, 5, 6, 9).
- An LLM endpoint that rejects `temperature`, `max_tokens`, or `tools`, omits `usage`, or answers 429 or 5xx is handled by the documented fallbacks and never leaks the key in an error (Task 8).
- Out-of-order or repeated tool calls (run before approval, `set_plan` during a run, rebuild after planning, `invalidate` while running) never corrupt `job.json` or move files while a process runs (Tasks 11, 16, 17).

---

## File Structure

```
pyproject.toml  config.yaml  .github/workflows/ci.yml  .gitignore
src/simagent/
  __init__.py  cli.py  config.py  errors.py  events.py  llm.py  mcp_client.py
  mcp_server/  __init__.py  app.py  registry.py  tools_env.py  tools_job.py  tools_build.py  tools_exec.py
  core/        __init__.py  paths.py  provenance.py  gmx.py  envcheck.py  jobs.py  mdp.py  build.py
               grompp.py  gates.py  approval.py  runs.py  mdrun_wrapper.py
  analysis/    __init__.py  xvg.py  stats.py
  templates/mdp/  em.mdp.j2  nvt.mdp.j2  npt.mdp.j2  prod.mdp.j2  dry.mdp.j2
tests/  conftest.py  unit/  integration/  fakes/fake_gmx
```

Each file has one responsibility: `errors.py` codes and envelope; `config.py` loading; `core/jobs.py` job directory and stage state; `core/approval.py` plan hash and approval rules; `core/runs.py` process state and progress; everything in `mcp_server/` only adapts `core/` functions to tools.

---

### Task 1: Scaffold, git, and CI (TASK-010, TASK-017)

**Files:**
- Modify: `pyproject.toml` (add `[build-system]` with hatchling and `src/` layout, `[project.scripts]` `simagent = "simagent.cli:main"` and `gmx-mcp = "simagent.mcp_server.app:main"`, `[tool.ruff]` with `select = ["E", "F", "I", "S602", "S605"]`, `[tool.pytest.ini_options]` with markers `posix`, `real_gmx`, `slow` and `testpaths = ["tests"]`, `[tool.mypy]`; remove `package = false`)
- Create: `src/simagent/__init__.py`, `src/simagent/cli.py`, `tests/conftest.py`, `tests/unit/test_scaffold.py`, `.github/workflows/ci.yml`, `src/simagent/mcp_server/__init__.py` and `src/simagent/mcp_server/app.py` (stub `main()` that raises `NotImplementedError`, replaced in Task 10)

**Interfaces:**
- Produces: `simagent.__version__: str = "0.1.0"`; `simagent.cli.main(argv: list[str] | None = None) -> int` (prints `simagent 0.1.0`, returns 0); pytest markers; `conftest.py` skip logic for `posix` and `real_gmx`.

- [ ] **Step 1: Initialize git.** `git init -b main` in the repository root if no `.git` exists; set repo-local `user.name` and `user.email` only if no global values exist (use `prosun@prescience.in`); commit the existing docs, `pyproject.toml`, `uv.lock`, `.gitignore` on `main`, then `git switch -c m1-m2-implementation`.
- [ ] **Step 2: Write the failing tests** in `tests/unit/test_scaffold.py`: `test_version_is_0_1_0` (`simagent.__version__ == "0.1.0"`); `test_cli_main_prints_version_and_returns_zero` (capsys contains `simagent 0.1.0`, return 0); `test_ruff_blocks_shell_true` (parse `pyproject.toml` with `tomllib`; `"S602"` and `"S605"` in `tool.ruff.select`); `test_markers_registered` (`posix`, `real_gmx`, `slow` in `tool.pytest.ini_options.markers`).
- [ ] **Step 3: Run** `uv run pytest tests/unit/test_scaffold.py -v`. Expected: FAIL (module `simagent` not importable).
- [ ] **Step 4: Implement** the files above. `conftest.py` skips `posix` tests when `sys.platform == "win32"` and `real_gmx` tests unless running `GMX_BIN --version` succeeds. `ci.yml`: Ubuntu runner, `astral-sh/setup-uv`, `uv sync --frozen`, `uv run ruff check .`, `uv run mypy src`, `uv run pytest -m "not slow and not real_gmx"`.
- [ ] **Step 5: Run** `uv sync` then `uv run pytest tests/unit/test_scaffold.py -v`, `uv run ruff check .`, `uv run mypy src`, and `uv run simagent`. Expected: all PASS and `simagent 0.1.0` printed.
- [ ] **Step 6: Commit** `feat: scaffold simagent package, tooling, and CI`.

### Task 2: Configuration (TASK-011)

**Files:**
- Create: `src/simagent/config.py`, `config.yaml`, `tests/unit/test_config.py`

**Interfaces:**
- Consumes: `simagent.errors` is not used here; define `class ConfigError(Exception)` in `config.py`.
- Produces: frozen dataclasses `GmxConfig(bin: str, expected_version_prefix: str, expected_sha256: str | None, ntomp: int | str, ntmpi: int | str, cpt_minutes: int, short_cmd_timeout_s: int, max_wall_hours: float, env_passthrough: tuple[str, ...])`, `LlmConfig(model: str, base_url: str | None, api_key: str | None, temperature: float, max_tool_calls_per_turn: int, token_budget: int, request_timeout_s: int)`, `WorkflowConfig(require_approval: bool, max_auto_retries_per_stage: int, max_production_ns_without_confirm: float)`, `PathsConfig(runs_dir: Path, sim_runs_dir: Path, memory_dir: Path)`, `GatesConfig` (every key of design section 2 `gates:`), `Config(gmx, llm, workflow, paths, gates, approval_token: str | None, allow_fake: bool, allow_unapproved: bool, log_prompts: bool, user: str)`; `load_config(env: Mapping[str, str] | None = None, yaml_path: Path | None = None) -> Config`; `Config.__repr__` masks `api_key` and `approval_token` as `***set***` or `***missing***`.

- [ ] **Step 1: Write failing tests**: `test_defaults` (gmx.bin equals the path in Global Constraints; `llm.model == "gpt6-luna"`; `workflow.require_approval is True`; `gates.density_warn_g_cm3 == (0.96, 1.02)`); `test_env_overrides_yaml_overrides_defaults` (yaml sets `gmx.cpt_minutes: 5`, env `GMX_BIN=/x/gmx` and `LUNA_MODEL=m`; env wins, yaml wins over default); `test_ntomp_auto_and_int` (`GMX_NTOMP=4` gives int 4; unset gives `"auto"`); `test_secret_in_yaml_refused` (yaml containing `llm: {api_key: abc}` or key `LUNA_API_KEY` raises `ConfigError` naming the key but not its value); `test_repr_masks_secrets` (`repr(cfg)` contains `***set***` and neither `sekrit` nor the token); `test_require_approval_env` (`SIMAGENT_REQUIRE_APPROVAL=0` gives False).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_config.py -v`. Expected: FAIL (no module).
- [ ] **Step 3: Implement** `load_config` (precedence env, yaml, defaults; `yaml.safe_load`; unknown yaml keys raise `ConfigError`); write `config.yaml` with the non-secret defaults of design section 2.
- [ ] **Step 4: Run** the test file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: layered configuration with secret handling`.

### Task 3: Errors, envelope, paths, hashing (TASK-015)

**Files:**
- Create: `src/simagent/errors.py`, `src/simagent/core/__init__.py`, `src/simagent/core/paths.py`, `tests/unit/test_errors_paths.py`

**Interfaces:**
- Produces: `class ErrorCode(str, Enum)` with every code of design 4.2 (value equals name); `class SimAgentError(Exception)` with `code: ErrorCode`, `message: str`, `hint: str`, `data: dict | None`; `ok(data: dict | None = None) -> dict` and `fail(code: ErrorCode, message: str, hint: str = "", data: dict | None = None) -> dict` (the envelope); `error_envelope(exc: SimAgentError) -> dict`; `validate_job_id(job_id: str) -> str`; `confine(base: Path, rel: str | Path) -> Path`; `sha256_file(path: Path) -> str`; `sha256_text(text: str) -> str`.

- [ ] **Step 1: Write failing tests**: `test_envelope_shapes` (`ok({"a":1}) == {"ok": True, "data": {"a":1}, "error": None}`; `fail(ErrorCode.E_STAGE_ORDER, "m", "h")` has `ok False`, `error == {"code": "E_STAGE_ORDER", "message": "m", "hint": "h"}`); `test_job_id_path_escape` parametrized over `"../x"`, `"a/b"`, `"a\\b"`, `".hidden"`, `"a..b"`, `"/abs"` all raise `E_PATH_ESCAPE`; `test_job_id_malformed` over `""`, `"x"*81`, `"a b"`, `"é"` raise `E_PARAM_OUT_OF_RANGE`; `test_job_id_valid` (`water-1000-tip4p-300K-20261023-1` returned unchanged); `test_confine_rejects_escape` (`"../x"`, an absolute path, and a symlink leading outside raise `E_PATH_ESCAPE`; the symlink case skips when symlinks cannot be created); `test_confine_nested_ok`; `test_sha256_known_digest` (`sha256_text("abc")` equals `ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad`; `sha256_file` of a file holding `abc` equals the same).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_errors_paths.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces. `validate_job_id` pattern `^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$` plus the rules of design 4.1: separators, `..`, or a leading dot give `E_PATH_ESCAPE`; any other mismatch gives `E_PARAM_OUT_OF_RANGE`.
- [ ] **Step 4: Run** the test file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: error codes, envelope, path confinement, hashing`.

### Task 4: Provenance ledger (TASK-012)

**Files:**
- Create: `src/simagent/core/provenance.py`, `tests/unit/test_provenance.py`

**Interfaces:**
- Consumes: `sha256_file` from Task 3.
- Produces: `class Ledger(path: Path)` with `append(record: dict) -> int` (adds `seq` and UTC ISO `ts` under a `filelock.FileLock(str(path) + ".lock")`, writes one JSON line, returns `seq`), `records() -> list[dict]`, `has_receipt(rel_path: str, sha256: str) -> bool` (true if any record lists that path and hash under `outputs`), `latest_end(stage: str) -> dict | None`; module functions `file_entry(base: Path, rel: str) -> dict` (`{"path", "bytes", "sha256"}`), `record_internal(ledger: Ledger, *, stage: str, tool: str, base: Path, outputs: list[str]) -> int` (record with `event: "end"`, `kind: "internal"`), `record_ack(ledger: Ledger, *, warning_id: str, text: str, note: str) -> int` (`event: "ack"`, `kind: "internal"`). The class has no method that edits or deletes a record.

- [ ] **Step 1: Write failing tests**: `test_seq_monotonic` (three appends give 1, 2, 3); `test_concurrent_appends_unique_seq` (8 threads x 25 appends give 200 records, `seq` values exactly 1..200, every line valid JSON); `test_append_only_api` (`Ledger` has no attribute `update`, `delete`, `remove`, `rewrite`); `test_record_internal_has_receipt` (after `record_internal` on a written file, `has_receipt(rel, sha)` is True, a changed hash is False); `test_record_ack_shape` (record has `event == "ack"`, `kind == "internal"`, the fields); `test_file_entry_hash` (matches `sha256_file`).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_provenance.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces.
- [ ] **Step 4: Run** the test file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: append-only provenance ledger with internal receipts`.

### Task 5: Event log and redaction (TASK-016)

**Files:**
- Create: `src/simagent/events.py`, `tests/unit/test_events.py`

**Interfaces:**
- Produces: `class Redactor(secrets: Iterable[str])` with `redact(text: str) -> str` (replaces every secret substring of length at least 6 with `***`) and `redact_obj(obj)` (recursive over dict, list, str); `class EventLog(path: Path, redactor: Redactor)` with `emit(level: str, component: str, event: str, job_id: str | None = None, stage: str | None = None, data: dict | None = None) -> None` writing a JSON line with keys `ts, level, component, event, job_id, stage, data`; `timed_tool(log: EventLog, tool: str, job_id: str | None)` context manager that emits `tool_done` with `data.tool_time_s` (seconds in the block excluding any `external_s` the block adds through `.add_external(seconds)`).

- [ ] **Step 1: Write failing tests**: `test_redactor_replaces_canary` (canary key `sk-canary-123456` in a string and nested dict is replaced); `test_event_line_schema` (line has exactly the seven keys); `test_event_log_never_contains_secret` (emit with the canary in `data`; file text lacks it); `test_tool_time_excludes_external` (block sleeps 0.05 s total and declares 0.04 s external; `tool_time_s < 0.03`); `test_short_secret_ignored` (a 3-character secret is not redacted, to avoid mangling text).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_events.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces.
- [ ] **Step 4: Run** the test file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: structured event log with secret redaction`.

### Task 6: GROMACS environment and runner (TASK-013, part 1)

**Files:**
- Create: `src/simagent/core/gmx.py`, `tests/unit/test_gmx_runner.py`

**Interfaces:**
- Consumes: `Config`/`GmxConfig` (Task 2); `Ledger` and `file_entry` (Task 4); `SimAgentError`/`ErrorCode` (Task 3).
- Produces: `build_gmx_env(gmx: GmxConfig, parent_env: Mapping[str, str]) -> dict[str, str]`; `@dataclass RunResult(exit_code: int, stdout: str, stderr: str, seq_start: int | None, seq_end: int | None)`; `class GmxRunner(cfg: Config, ledger: Ledger | None = None, parent_env: Mapping[str, str] | None = None)` with `run(args: list[str], *, cwd: Path, stage: str = "", tool: str = "", stdin: str | None = None, inputs: Sequence[Path] = (), outputs: Sequence[Path] = (), base: Path | None = None, timeout: float | None = None) -> RunResult` executing `[cfg.gmx.bin, *args]` with `shell=False`.

- [ ] **Step 1: Write failing tests**: `test_env_allow_list_only` (parent has `FOO`, `PATH`, `LUNA_API_KEY`, `SIMAGENT_APPROVAL_TOKEN`; result has `PATH` and not `FOO`, `LUNA_API_KEY`, or `SIMAGENT_APPROVAL_TOKEN`); `test_env_derived_from_prefix` (a temp prefix `P/bin/gmx` with `P/share/gromacs` and `P/lib64` gives `GMXDATA == P/share/gromacs`, `GMXBIN == P/bin`, and `LD_LIBRARY_PATH` containing `P/lib64` but not the missing `P/lib`); `test_run_records_start_and_end` (config with `gmx.bin = sys.executable`, `run(["-c", "print('hi')"], cwd=tmp)` returns exit 0 and stdout `hi`; ledger holds a `start` and an `end` record whose `argv[0]` is the binary and `exit_code == 0`, with `stdout_sha256` equal to the hash of the stdout); `test_semicolon_argument_is_literal` (`run(["-c", "import sys; print(sys.argv[1])", "a;b"])` prints `a;b`); `test_timeout_maps_to_E_TIMEOUT`; `test_enospc_maps_to_E_DISK_FULL` (monkeypatch `subprocess.run` to raise `OSError(errno.ENOSPC, ...)`); `test_no_secret_in_ledger_env` (the `env_allow` recorded in provenance lacks the canary key).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_gmx_runner.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces (`subprocess.run` with `capture_output=True, text=True, shell=False`; ledger records per design 12 with `inputs` and `outputs` entries from `file_entry`; record only non-secret env values).
- [ ] **Step 4: Run** the test file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: gmx runner with constructed environment and receipts`.

### Task 7: Environment check, fake binary, simulated mode (TASK-013 part 2, TASK-060)

**Files:**
- Create: `src/simagent/core/envcheck.py`, `tests/fakes/fake_gmx`, `tests/unit/test_envcheck.py`, `tests/integration/test_envcheck_real.py`

**Interfaces:**
- Consumes: `GmxRunner`, `Config`, `SimAgentError`.
- Produces: `check_environment(cfg: Config, *, version_fn: Callable[[], str] | None = None, probe_fn: Callable[[], dict] | None = None) -> dict` returning `bin_path, sha256, is_elf, version, has_oplsaa, has_tip4p_itp, has_tip4p_gro, ncpu, gpu_detected, env_allow, probe, execution_mode`; the default `version_fn` runs `[bin, "--version"]` through `GmxRunner`; the default `probe_fn` runs the design 4.6 step 5 micro-run in a temporary directory. `tests/fakes/fake_gmx` is a Python script (`#!/usr/bin/env python3`) printing `GROMACS version: FAKE-0.0` for `--version` and implementing `solvate` (valid `.gro` with 4 atoms per `SOL`, molecule count `round(33.3 * L**3)` from `-box`), `editconf`, `grompp`, `mdrun` (log containing `Finished mdrun on rank 0`), `energy`, `check`, `rdf`, `msd` (placeholder files whose first line is `FAKE-GMX OUTPUT`).

- [ ] **Step 1: Write failing tests** (all take `cfg` with `gmx.bin` set to a temp path and `version_fn`/`probe_fn` stubs unless stated): `test_missing_binary` (`E_GMX_NOT_FOUND`, hint names `GMX_BIN`); `test_directory_not_file` (`E_GMX_NOT_FOUND`); `test_text_file_not_elf_not_script` (`E_GMX_NOT_ELF`); `test_script_without_flag_is_fake` (a file starting `#!` and `allow_fake=False` gives `E_FAKE_BINARY`); `test_script_with_flag_and_FAKE_output_is_simulated` (`allow_fake=True`, `version_fn` returns `GROMACS version: FAKE-0.0`, result `execution_mode == "simulated"` and `probe == "skipped (simulated)"`); `test_script_with_flag_but_no_FAKE_output` (`E_FAKE_BINARY`); `test_real_elf_ignores_allow_fake` (bytes `\x7fELF...` with `allow_fake=True` and a real-looking version gives `real`); `test_wrong_version_prefix` (`E_GMX_VERSION`); `test_sha256_pin_mismatch` (`E_GMX_VERSION`); `test_missing_oplsaa_data` (`E_DATA_MISSING` when `<prefix>/share/gromacs/top/oplsaa.ff/tip4p.itp` is absent); `test_probe_failure` (`probe_fn` raising `SimAgentError(E_PROBE_FAILED)` propagates); `test_success_shape` (all result keys present; `execution_mode == "real"`). In `tests/unit/test_envcheck_fake.py` marked `posix`: `test_fake_version_and_solvate` (run the fake as a subprocess; `--version` prints the string; `solvate -box 3.1 3.1 3.1 -o out.gro` writes a file whose atom count is divisible by 4 and equals `4 * round(33.3 * 3.1**3)`). In `tests/integration/test_envcheck_real.py` marked `real_gmx`: `test_real_environment_check_passes` (T-INT-05: default `check_environment(load_config())` returns `real` with a passing probe).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_envcheck.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** `check_environment` following design 4.6 steps 1 to 6 exactly (ELF magic or `#!`; SHA-256 and optional pin; version via `version_fn`; `GMXDATA` file checks; probe; one mode rule), and the fake binary. The probe `.mdp` is the one in design 4.6 step 5.
- [ ] **Step 4: Run** the unit tests, then `uv run pytest tests/integration/test_envcheck_real.py -v`. Expected: unit PASS; the integration test SKIPPED here and required to pass on the Linux host.
- [ ] **Step 5: Commit** `feat: environment check with probe, fake binary, simulated mode`.

### Task 8: LLM client (TASK-014)

**Files:**
- Create: `src/simagent/llm.py`, `tests/unit/test_llm.py`, `tests/integration/test_llm_live.py`

**Interfaces:**
- Consumes: `LlmConfig`, `Redactor`.
- Produces: `@dataclass ToolCall(id: str, name: str, arguments: dict)`; `@dataclass ChatReply(content: str | None, tool_calls: list[ToolCall], total_tokens: int, usage_estimated: bool, used_json_fallback: bool)`; `class LlmUnavailable(Exception)`; `class LlmClient(cfg: LlmConfig, *, http_client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep)` with `chat(messages: list[dict], functions: list[dict] | None = None, max_tokens: int = 1024) -> ChatReply`. Wraps `openai.OpenAI(base_url=cfg.base_url, api_key=cfg.api_key, http_client=..., max_retries=0)`.

- [ ] **Step 1: Write failing tests** using `httpx.MockTransport` handlers: `test_plain_reply` (returns content, `total_tokens` from `usage`); `test_tool_calls_parsed` (function arguments JSON string becomes a dict, `id` preserved); `test_authorization_header_uses_key` (header `Bearer <key>`); `test_400_naming_temperature_retries_without_it_once_and_remembers` (second request body lacks `temperature`, and so does a third call); `test_400_naming_max_tokens_retries_with_max_completion_tokens`; `test_429_backoff_waits_1_2_4_8` (`sleep` spy receives `[1, 2, 4, 8]` before success on the 5th call); `test_5xx_exhausts_retries_raises_LlmUnavailable` (after 4 retries); `test_missing_usage_estimated` (`usage_estimated True`, tokens equal to total message characters divided by 4, rounded up); `test_tools_rejected_uses_json_fallback` (400 mentioning `tools` makes the client resend without `tools`, with the function list in the system text and `response_format` of type `json_object`; reply content `{"tool": "get_job", "args": {"job_id": "j"}, "message": "m"}` yields one `ToolCall`, `used_json_fallback True`); `test_error_never_contains_key` (the `LlmUnavailable` message lacks the API key). In `tests/integration/test_llm_live.py` marked `slow`: `test_live_tool_calling` skipped unless `LUNA_API_KEY` and `LUNA_BASE_URL` are set (T-LLM-01: one tool-call round trip).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_llm.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** per design 14.3 (fallback state remembered on the instance; JSON fallback parses the first JSON object in the reply).
- [ ] **Step 4: Run** the unit tests. Expected: PASS.
- [ ] **Step 5: Commit** `feat: OpenAI-compatible LLM client with fallbacks`.

### Task 9: MCP client helpers (TASK-018)

**Files:**
- Create: `src/simagent/mcp_client.py`, `tests/unit/test_mcp_client.py`, `tests/fakes/stub_mcp_server.py`

**Interfaces:**
- Consumes: `mcp` SDK, `ErrorCode`.
- Produces: `LLM_ALLOWED_TOOLS: frozenset[str] = frozenset({"get_job", "list_jobs", "get_log_excerpt", "list_artifacts", "get_run_status"})`; `to_openai_functions(tools: Sequence[mcp.types.Tool], allow: Collection[str] = LLM_ALLOWED_TOOLS) -> list[dict]`; `server_params(parent_env: Mapping[str, str], token: str | None) -> StdioServerParameters` (command `sys.executable`, args `["-m", "simagent.mcp_server.app"]`, env = parent env minus every `LUNA_*` key plus `SIMAGENT_APPROVAL_TOKEN` when given); `class McpSession(params: StdioServerParameters)` async context manager with `list_tools() -> list[mcp.types.Tool]` and `call(name: str, args: dict) -> dict` returning the parsed envelope from `structuredContent`. `tests/fakes/stub_mcp_server.py` is a tiny `MCPServer` with an `echo` tool returning an envelope.

- [ ] **Step 1: Write failing tests**: `test_allow_list_is_exactly_five_and_excludes_record_approval`; `test_to_openai_functions_filters_and_converts` (given tools including `record_approval` and `get_job`, only `get_job` appears, as `{"type": "function", "function": {"name", "description", "parameters": inputSchema}}`); `test_server_params_strips_luna_and_adds_token` (parent env with `LUNA_API_KEY`, `LUNA_BASE_URL`, `PATH`, `LANG`; result env lacks both `LUNA_*`, has `PATH`, `LANG`, and the token; the token is absent when `None`); `test_session_lists_and_calls_stub_server` (anyio test: start the stub through `StdioServerParameters(command=sys.executable, args=[path])`; `list_tools` includes `echo`; `call("echo", {"x": 1})["data"] == {"x": 1}`); verify against the installed SDK that `env` is merged over the SDK default environment and record the observation in the ledger (design 2.1 item 3, `[verify TASK-018]`).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_mcp_client.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces.
- [ ] **Step 4: Run** the test file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: MCP client helpers and model allow-list`.

### Task 10: MCP server skeleton (TASK-020)

**Files:**
- Create: `src/simagent/mcp_server/registry.py`, `src/simagent/mcp_server/tools_env.py`; Modify: `src/simagent/mcp_server/app.py`; Test: `tests/unit/test_server_skeleton.py`

**Interfaces:**
- Consumes: `Config`, `check_environment`, envelope helpers, `McpSession` (tests).
- Produces: `class ServerContext(cfg: Config, events: EventLog)`; `registry.tool(name: str, model: type[pydantic.BaseModel] | None = None)` decorator registering `fn(ctx: ServerContext, args: dict) -> dict` (the returned dict is `data`); the wrapper validates `args` with `model` (extra fields forbidden; failure gives `E_PARAM_OUT_OF_RANGE`), converts `SimAgentError` and unexpected exceptions into envelopes (unexpected ones as an error code `E_PARAM_OUT_OF_RANGE`-free generic: use `E_TIMEOUT`? no; map to a message with hint, `ok False`), and returns `mcp.types.CallToolResult(structuredContent=envelope, content=[TextContent(...)], isError=not ok)`; `build_server(cfg: Config | None = None) -> MCPServer`; `main() -> None` runs it over stdio with stdout reserved for protocol frames; tool 1 `gmx_check_environment`.

- [ ] **Step 1: Write failing tests** (anyio, spawning the server with `server_params`): `test_lists_registered_tools` (contains `gmx_check_environment`); `test_error_returns_envelope_and_is_error` (call a test-only tool registered via an env-var-gated hook or a second module that raises `SimAgentError(E_STAGE_ORDER)`; result `ok False`, code `E_STAGE_ORDER`, `isError` true); `test_unknown_field_rejected` (extra argument gives `E_PARAM_OUT_OF_RANGE` in the envelope, not a plain-text SDK error); `test_stdout_noise_does_not_break_protocol` (a tool that calls `print("noise")` still returns a parsed envelope); `test_bad_job_id_returns_path_escape_for_job_tools` (once any job tool exists; here use the test-only tool taking `job_id` and calling `validate_job_id`, input `"../../x"` gives `E_PATH_ESCAPE`); `test_check_environment_tool_reports_missing_binary` (server started with `GMX_BIN=/nonexistent`; call returns `E_GMX_NOT_FOUND`). Record in the ledger what the installed SDK does with typed handler arguments and with returned `CallToolResult` (`[verify TASK-020]`); adapt `registry.py` to use a permissive dict schema if the SDK validates first.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_server_skeleton.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces. For stdout discipline, duplicate the original stdout descriptor for the protocol transport and point file descriptor 1 at stderr before any tool runs (check whether `stdio_server` accepts streams; if not, record a ruling).
- [ ] **Step 4: Run** the test file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: MCP server skeleton with envelope and stdout discipline`.

### Task 11: Jobs and the stage state machine (TASK-019)

**Files:**
- Create: `src/simagent/core/jobs.py`, `src/simagent/mcp_server/tools_job.py`, `tests/unit/test_jobs.py`

**Interfaces:**
- Consumes: `Config`, `Ledger`, `validate_job_id`, `confine`, `SimAgentError`.
- Produces: `STAGES: tuple[str, ...]`, `STAGE_DIRS: dict[str, str]`; `class JobHandle` with `job_id: str`, `dir: Path`, `ledger: Ledger`, `read() -> dict`, `update(fn: Callable[[dict], None]) -> dict` (under a file lock, atomic write), `stage_dir(stage: str) -> Path`, `status(stage: str) -> str`, `set_status(stage: str, new: str) -> None` (validates the transition table of design 3.4, else `E_STAGE_STATE`), `invalidate(stage: str) -> list[str]` (stage and later stages to `PENDING`, files moved to `superseded/<n>/`, refuses with `E_MDRUN_RUNNING` while any stage is `RUNNING`), `job_status() -> str` (the derivation of design 3.4); `class JobStore(cfg: Config)` with `create(request: dict, execution_mode: str, environment: dict) -> JobHandle` (generates `<kind>-<n>-<water_model>-<T>K-<yyyymmdd>-<k>`, `k` incremented until unused, simulated jobs under `paths.sim_runs_dir`), `get(job_id: str) -> JobHandle` (`E_JOB_NOT_FOUND`), `list(limit: int | None = None, status: str | None = None) -> list[dict]`; tool 2 `create_job` in `tools_job.py` (inputs `request`; output `job_id`, `job_dir`, `environment`; environment from `check_environment`, cached per binary hash, mtime, `env_allow` hash, and host name).

- [ ] **Step 1: Write failing tests**: `test_create_makes_layout_and_job_json` (six stage directories, `job.json` keys of design 3.3 present, all stages `PENDING`, `ledger` file exists); `test_job_id_format_and_collision_increments_k` (second job with the same parameters ends `-2`; an existing directory is never overwritten, T-UNIT-10); `test_simulated_job_goes_to_sim_runs_dir`; `test_get_unknown_job` (`E_JOB_NOT_FOUND`); `test_transition_table` parametrized: every legal row of design 3.4 succeeds (`PENDING→READY`, `READY→RUNNING`, `RUNNING→GATING`, `RUNNING→FAILED`, `RUNNING→CANCELLED`, `GATING→PASSED`, `GATING→FAILED`, `FAILED→RUNNING`, `CANCELLED→RUNNING`) and illegal ones (`PENDING→RUNNING`, `PASSED→RUNNING`, `READY→PASSED`) raise `E_STAGE_STATE`; `test_invalidate_resets_later_stages_and_moves_files` (set `em` and `nvt` to `PASSED` with files; `invalidate("em")` sets `em`, `nvt`, `npt`, `prod`, `analysis` to `PENDING` and the files appear under `superseded/1/`); `test_invalidate_refused_while_running` (`E_MDRUN_RUNNING`, nothing moved); `test_job_status_derivation` (running beats failed beats cancelled beats passed beats planned); `test_concurrent_updates_do_not_lose_writes` (8 threads incrementing a counter field 25 times each give 200); `test_create_job_tool_cache` (the environment check function is called once for two `create_job` calls with unchanged inputs and again when the binary hash changes, T-UNIT-16); `test_create_job_tool_bad_request_out_of_range` (T-UNIT-01 partial: a request whose temperature is 199 gives `E_PARAM_OUT_OF_RANGE`; the full range logic arrives in Task 12 and this test is completed there).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_jobs.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces; the temperature-range test may be added in Task 12 once `validate_request` exists (remove it from this task if so and ledger the move).
- [ ] **Step 4: Run** the test file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: job store, stage state machine, create_job tool`.

### Task 12: MDP templates, ranges, retries (TASK-022)

**Files:**
- Create: `src/simagent/core/mdp.py`, `src/simagent/templates/mdp/{em,nvt,npt,prod,dry}.mdp.j2`, `tests/unit/test_mdp.py`

**Interfaces:**
- Consumes: `SimAgentError`, `sha256_text`.
- Produces: `DEFAULT_REQUEST: dict` and `derive_seed(job_id: str) -> int` (first 8 hex characters of SHA-256 of the UTF-8 job id, as an integer, modulo 2**31; `derive_seed("water-1000-tip4p-300K-20261023-1") == 1904784231`); `validate_request(request: dict) -> None` and `validate_plan(plan: dict, request: dict) -> None` enforcing design 5.3 (raise `E_PARAM_OUT_OF_RANGE` naming the field); `render_mdp(stage: str, plan: dict, request: dict, retries: Sequence[dict] = ()) -> str` (Jinja2 with `StrictUndefined`; `dry` stage takes no plan values); `rescale_for_dt(stage_params: dict, new_dt: float) -> dict` (scales `nsteps`, `nstxout`, `nstenergy`, `nstlog` by `dt_old/dt_new`); `check_retry(plan: dict, state: dict, key: str, value: float, failing_stage: str | None) -> None` where `state = {"failures": [...], "retries": [...]}` raising `E_RETRY_NOT_ALLOWED` or `E_RETRY_LIMIT` per design 4.3 item 7; `build_default_plan(request: dict, build: dict) -> dict` (the seven-stage plan of design 3.2 with the structured `automatic_retries_allowed`, `emtol` `max` equal to the planned value).

- [ ] **Step 1: Write failing tests**: `test_seed_derivation_matches_design_example` (value above) and `test_seed_differs_between_jobs`; `test_render_prod_contains_spec_values` (for the reference request and plan the text contains `integrator            = md`, `nsteps                = 500000`, `dt                    = 0.002`, `pcoupl                = C-rescale`, `tau-p                 = 2.0`, `ref-t                 = 300.0`, `nstxout-compressed    = 250`); `test_render_em_values` (`integrator            = steep`, `emtol                 = 1000.0`, `nstxout-compressed    = 0`, no `tcoupl` line); `test_render_nvt_gen_vel_and_seed` (`gen-vel               = yes`, `gen-seed              = 1904784231` for the example job); `test_render_is_deterministic` (two renders identical, same `sha256_text`); `test_render_dry_is_plan_independent`; `test_range_boundaries` parametrized per design 5.3: temperature 199 rejects and 200, 450 accept, 451 rejects; pressure 0.09 rejects, 0.1 accepts; dt 0.0021 rejects, 0.002 accepts, 0.0005 accepts, 0.0004 rejects; `n_molecules` 308 rejects and 309 accepts (rc 1.0); `prod_ns` 0.009 rejects; negative seed rejects; unknown `water_model` rejects; `tip4pew` with `oplsaa` rejects; `test_dt_retry_rescales` (dt 0.002 to 0.001 gives `nsteps` 1,000,000 for prod and `nstxout` 500); `test_retry_bounds` (`emtol` 5000 above plan max gives `E_RETRY_NOT_ALLOWED`; `dt` 0.0015 not allowed; no recorded failure not allowed; two prior retries on the failing stage gives `E_RETRY_LIMIT`; `emtol` 100 with a failure is accepted); `test_default_plan_shape` (seven stages S1 to S7, retries list as design 3.2, `max` equals planned `emtol`).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_mdp.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces and the five templates (design 5.1 and 5.2; template package data via `importlib.resources`).
- [ ] **Step 4: Run** the test file, plus `uv run pytest tests/unit/test_jobs.py -v` (re-add the temperature test from Task 11 here if it was deferred). Expected: PASS.
- [ ] **Step 5: Commit** `feat: mdp templates, range validation, bounded retries`.

### Task 13: Water box builder (TASK-021)

**Files:**
- Create: `src/simagent/core/build.py`, `src/simagent/mcp_server/tools_build.py`, `tests/unit/test_build.py`, `tests/integration/test_build_real.py`

**Interfaces:**
- Consumes: `GmxRunner`, `JobHandle`, `render_mdp`, `record_internal`, `sha256_file`.
- Produces: `number_density(density_g_cm3: float) -> float` (`density * 602.2140857 / 18.01528`); `initial_edge(n: int, density: float) -> float`; `count_sol(gro_text: str) -> int`; `find_edge(count_fn: Callable[[float], int], n: int, density: float) -> float` (bisection of design 4.7 step 3: at most 12 iterations in `[0.9*L0, 1.1*L0]`, smallest tested edge with count at least `n`, `E_BUILD_COUNT` if none); `trim_gro(gro_text: str, n: int) -> str` (keeps the first `n` molecules, rewrites the atom-count line to `4*n`); `topology_text(n: int, edge: float) -> str` (design 4.7 step 5); `build_water_box(job: JobHandle, runner: GmxRunner, *, n_molecules: int, water_model: str, target_density_g_cm3: float = 0.997) -> dict` returning `box_nm, n_molecules, n_atoms, density_g_cm3, build_sha256, files`; tool 5 `build_water_box` (a call after a plan exists applies `invalidate("build")` and clears the approval; refused with `E_MDRUN_RUNNING` while a process runs).

- [ ] **Step 1: Write failing tests**: `test_number_density_and_edge` (`number_density(0.997)` within 0.01 of 33.33; `initial_edge(1000, 0.997)` within 0.002 of 3.107); `test_find_edge_hits_exact_count` (a stub `count_fn = lambda L: round(33.33 * L**3 + (L > 3.2) * 2)` for `n=1000` returns an edge whose count is at least 1000 and is the smallest tested such edge); `test_find_edge_raises_when_never_reached` (`count_fn = lambda L: 10` gives `E_BUILD_COUNT`); `test_trim_gro_rewrites_header` (a gro with 1010 molecules trimmed to 1000 has line 2 equal to `4000` and 4,000 atom lines); `test_topology_text_exact` (equals the design 4.7 block for `n=1000`, edge 3.1070); `test_build_with_stub_runner` (a stub runner whose `run` mimics solvate and editconf by writing gro files of the right molecule count writes `water.gro` with 4,000 atoms, `topol.top` whose `[ molecules ]` count is 1000, `build.json` with `density_g_cm3` equal to `1000 * 18.01528 / (602.2140857 * L**3)`, and internal receipts for `water_raw.gro`, `water.gro`, `topol.top`, `build.json`); `test_build_after_plan_invalidates_and_clears_approval` (job with a stored plan and approval: after the call, `approval` is empty and `build` and later stages are `PENDING`; T-UNIT-17 first half); `test_build_refused_while_running` (`E_MDRUN_RUNNING`). In `tests/integration/test_build_real.py` marked `real_gmx`: `test_real_build_exactly_1000_sol` (T-INT-07: 1,000 `SOL`, 4,000 atoms, matching topology, dry grompp exits 0).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_build.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces (dry `grompp` through the runner with `dry.mdp.j2`; the stub-runner tests inject the runner).
- [ ] **Step 4: Run** the unit tests. Expected: PASS (integration test SKIPPED here).
- [ ] **Step 5: Commit** `feat: water box builder with exact molecule count`.

### Task 14: grompp with warning acknowledgement and input checks (TASK-023)

**Files:**
- Create: `src/simagent/core/grompp.py`, `tests/unit/test_grompp.py`

**Interfaces:**
- Consumes: `JobHandle`, `GmxRunner`, `render_mdp`, `Ledger.has_receipt`, `sha256_file`, `SimAgentError`.
- Produces: `parse_warnings(text: str) -> list[dict]` (each `{"id", "text", "source"}`; `id` is the first 12 hex characters of the SHA-256 of the normalized text plus source file and line; recognizes `WARNING N [file ..., line ...]:` blocks `[verify TASK-034]`); `check_binary(job: JobHandle, cfg: Config) -> None` (`E_BINARY_CHANGED` when the SHA-256 of `cfg.gmx.bin` differs from `job.environment.sha256`; size and mtime compared first and the hash recomputed only when they changed); `check_inputs(job: JobHandle, stage: str, plan: dict, request: dict) -> None` (`E_INPUT_MODIFIED` for a differing `.mdp`, `topol.top`, input `.gro`, or build hash, design 4.3 item 6); `run_grompp(job: JobHandle, stage: str, runner: GmxRunner, cfg: Config) -> dict` returning `{"tpr", "warnings", "notes"}`: first pass without `-maxwarn`; warnings stored in `job.json.pending_warnings`; if there are warnings and all are acknowledged, a second pass with `-maxwarn N` where `N` equals the warning count; otherwise `E_GROMPP_FAILED` with `data.warnings`; a failure is recorded in `job.json.failures`; success moves the stage `PENDING→READY`; writes `grompp.out` (merged stdout and stderr) and an internal receipt for the rendered `.mdp`.

- [ ] **Step 1: Write failing tests** with a scripted stub runner: `test_parse_warnings_ids_stable_and_distinct`; `test_no_warnings_single_pass_no_maxwarn` (argv lacks `-maxwarn`; stage becomes `READY`; `grompp.out` written); `test_unacknowledged_warning_fails_without_maxwarn` (`E_GROMPP_FAILED`, `data.warnings` lists the id, no second invocation, failure recorded, stage stays `PENDING`); `test_acknowledged_warning_second_pass_exact_maxwarn` (one warning acknowledged gives a second argv with `-maxwarn 1`; two warnings with one acknowledged fail; T-UNIT-12); `test_never_maxwarn_otherwise` (no argv ever contains `-maxwarn` unless the count rule holds); `test_modified_mdp_gives_input_modified` (edit `ref-t` in the written `.mdp`, T-UNIT-15); `test_modified_topology_gives_input_modified`; `test_binary_changed` (T-UNIT-22: replace the configured binary file's bytes; error `E_BINARY_CHANGED`; with unchanged size and mtime the hash is not recomputed, asserted through a counter); `test_stage_order_enforced` (`nvt` before `em` is `PASSED` gives `E_STAGE_ORDER`); `test_input_coordinates_per_stage` (argv `-c` values equal the table of design 4.4; `-t` present for `npt` and `prod` only).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_grompp.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces.
- [ ] **Step 4: Run** the test file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: grompp runner with warning acknowledgement and input checks`.

### Task 15: Analysis library, gates, validate_stage (TASK-050, TASK-026)

**Files:**
- Create: `src/simagent/analysis/__init__.py`, `src/simagent/analysis/xvg.py`, `src/simagent/analysis/stats.py`, `src/simagent/core/gates.py`, `tests/unit/test_analysis.py`, `tests/unit/test_gates.py`

**Interfaces:**
- Consumes: `GatesConfig`, `Ledger`.
- Produces: `parse_xvg(text: str) -> tuple[list[str], list[list[float]]]` (legends, rows; skips `#` and `@` lines except legends); `block_average(values: Sequence[float], n_blocks: int = 5) -> tuple[float, float]` (mean, block standard error); `linear_drift(times: Sequence[float], values: Sequence[float]) -> tuple[float, float]` (slope per time unit, half-width of the 95% interval); `@dataclass GateCheck(name: str, status: str, value: object, threshold: object, evidence_file: str | None)`; `gate_result(checks: Sequence[GateCheck]) -> bool` (False when any status is `fail`, `no_receipt`, or `not_run`); one pure function per gate returning `list[GateCheck]`: `gate_build(...)`, `gate_em(...)`, `gate_nvt(...)`, `gate_npt(...)`, `gate_prod(...)`, `gate_analysis(...)`, each taking plain values (series, log text, process state, receipts, thresholds from `GatesConfig`, `simulated: bool`) and applying the table in design section 6 including the density-region rule and `skipped_simulated`; `validate_stage` tool (tool 11) assembling inputs from the job and delegating, moving `RUNNING→GATING` or `RUNNING→FAILED` per design 3.4, returning `{"passed", "checks", "warnings"}` and `E_CANCELLED` for a cancelled stage.

- [ ] **Step 1: Write failing tests** `tests/unit/test_analysis.py`: `test_parse_xvg_skips_comments_and_reads_legends` (a four-line sample); `test_block_average_known_series` (constant series gives mean c and SEM 0; `[1,2,3,4,5,6,7,8,9,10]` with 5 blocks gives mean 5.5); `test_linear_drift_known_slope` (series `3t + 2` gives slope 3, interval half-width below 1e-9). `tests/unit/test_gates.py` (T-UNIT-03), one test per gate with constructed inputs: `test_gate_em` (log with `converged to Fmax < 1000`, Fmax 800, Epot per molecule -41 gives `pass`; not converged gives `fail`; Epot per molecule -50 gives `warn`); `test_gate_nvt` (mean T 300.5 with ref 300 passes; 303 fails; drift 0.7 K/100 ps warns); `test_gate_npt` (density 0.998 passes; 0.95 warns; 0.89 fails; temperature 303 fails; pressure outside `3*SEM + 50` warns); `test_density_bands_only_near_ambient` (ref T 400 K gives a `warn` density check with no fail); `test_gate_prod` (frame count 2001 and last time 1000.0 passes; 2000 frames fails; temperature outside 1 K fails; conserved drift above 0.005 warns); `test_gate_build` (hashes equal and atoms 4000 pass; atoms 3999 fails; hash mismatch fails); `test_gate_analysis` (missing `rdf_OO.xvg` fails); `test_all_checks_receipt_and_process` (process state `died` gives a `process_died` fail; missing receipt gives `no_receipt`; `not_run` for a stage that never ran); `test_simulated_profile` (physical checks return `skipped_simulated`, structural ones still evaluate); `test_gate_result_rule`. Add `test_validate_stage_tool_running_died_goes_failed` using a prepared job directory (stage `RUNNING`, process state `died`): status `FAILED` and a `process_died` check.
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_analysis.py tests/unit/test_gates.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces. Energy and `gmx check` extraction wrappers are called by `validate_stage` through the runner and are covered later by `real_gmx` tests; gates themselves stay pure.
- [ ] **Step 4: Run** both files. Expected: PASS.
- [ ] **Step 5: Commit** `feat: analysis library, stage gates, validate_stage`.

### Task 16: Plan, approval, and job tools (TASK-035, TASK-028 approval part, TASK-068, TASK-027)

**Files:**
- Create: `src/simagent/core/approval.py`, `tests/unit/test_approval.py`; Modify: `src/simagent/mcp_server/tools_job.py`

**Interfaces:**
- Consumes: `JobHandle`, `validate_plan`, `gate_build`, `Redactor`, `Config`.
- Produces: `plan_sha256(request: dict, plan: dict) -> str` (SHA-256 of canonical JSON of the request, each stage's `id`, `name`, `params`, `analyses`, and `automatic_retries_allowed`; `why` and `estimates` excluded); `require_run_approval(job: JobHandle, cfg: Config, planned_prod_ns: float) -> None` implementing design 4.3 items 3 to 5 (`E_APPROVAL_UNAVAILABLE`, `E_NOT_APPROVED`, operator override recording `mode = operator_override` once per plan hash); tools 3 `set_plan`, 4 `record_approval`, 12 `get_log_excerpt`, 19 `list_artifacts`, 20 `get_job`, 21 `list_jobs` per design 4.1 (`set_plan` takes `job_id`, `request`, `plan`; `get_job` includes `summary` as null until a summary exists).

- [ ] **Step 1: Write failing tests**: `test_plan_hash_ignores_why_and_estimates_and_retries_applied` (T-UNIT-11); `test_plan_hash_changes_with_any_param`; `test_set_plan_validates_and_stores` (out-of-range temperature gives `E_PARAM_OUT_OF_RANGE`; success returns the hash and stores the request, replacing the one from `create_job`); `test_set_plan_build_mismatch` (declared `n_molecules` different from `build.json` gives `E_PARAM_OUT_OF_RANGE`; declared `build_sha256` different from the files gives `E_GATE_FAILED`); `test_set_plan_refused_while_running` (T-UNIT-20: `E_MDRUN_RUNNING`, statuses unchanged); `test_set_plan_clears_approval_and_invalidates_changed_stages`; `test_record_approval_checks` (wrong token `E_BAD_TOKEN`; wrong hash `E_PLAN_MISMATCH`; server without token `E_APPROVAL_UNAVAILABLE`; unknown warning id `E_PARAM_OUT_OF_RANGE`); `test_require_run_approval_matrix` (T-UNIT-07: no approval gives `E_NOT_APPROVED`; matching approval passes; planned production 12 ns without `long_run_confirmed` gives `E_NOT_APPROVED`); `test_operator_override` (T-UNIT-18: with `allow_unapproved` and no token, a stored plan yields a recorded `operator_override` approval, 12 ns still refused, warning acknowledgement still `E_APPROVAL_UNAVAILABLE`); `test_check_order` (a call violating `E_NOT_APPROVED` and `E_STAGE_ORDER` reports `E_NOT_APPROVED`); `test_get_job_and_list_jobs` (`job_status`, `stage_status`, `approval` presence, `summary` null; `list_jobs(status="planned")` filters; field `title` present); `test_get_log_excerpt` (at most 40 lines, `which` mapping `log`, `grompp`, `stderr`, regex filter, missing file gives `E_ANALYSIS_INPUT`); `test_list_artifacts_has_receipt_flag`; `test_token_never_in_results` (canary token absent from every returned envelope).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_approval.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces and register the six tools.
- [ ] **Step 4: Run** the test file. Expected: PASS.
- [ ] **Step 5: Commit** `feat: set_plan, approval, and job read tools`.

### Task 17: Execution tools and the mdrun wrapper (TASK-024, TASK-025)

**Files:**
- Create: `src/simagent/core/runs.py`, `src/simagent/core/mdrun_wrapper.py`, `src/simagent/mcp_server/tools_exec.py`, `tests/unit/test_runs.py`, `tests/unit/test_wrapper.py`

**Interfaces:**
- Consumes: `JobHandle`, `require_run_approval`, `check_binary`, `check_inputs`, `GmxRunner`, `Ledger`, `check_retry`, `render_mdp`.
- Produces: `@dataclass RunFiles` and `@dataclass ProcSnapshot(alive: bool, start_ticks: int | None, cmdline: str | None)`; `decide_process_state(files: RunFiles, proc: ProcSnapshot | None, now: float) -> str` implementing the table of design 4.5 (`not_started`, `running` including `launching` younger than 30 s, `cancelled`, `finished`, `died`); `read_proc_snapshot(pid: int) -> ProcSnapshot` (reads `/proc` on Linux); `estimate_rate(samples: Sequence[tuple[float, float]], remaining_ps: float) -> tuple[float | None, float | None]` (ns/day = delta time_ps / delta wall_s * 86.4 using the last two samples at least 30 s apart, ETA seconds); `run_wrapper(stage_dir: Path, argv: list[str], env: Mapping[str, str], *, ledger_path: Path, max_wall_hours: float, now: Callable[[], float] = time.time) -> int` implementing design 4.5 steps 1 to 5 (end record first, then `run.exit` last; `run.timeout` on overrun; SIGTERM handler forwarding to the child on POSIX); `launch_wrapper(stage_dir: Path, argv: list[str], env: Mapping[str, str]) -> int` (detached process; returns the wrapper pid); tools 7 `run_grompp`, 6 `write_mdp`, 8 `run_mdrun`, 9 `get_run_status`, 10 `cancel_run` registered with the check order of design 4.2.

- [ ] **Step 1: Write failing tests** `tests/unit/test_runs.py`: `test_state_table` parametrized over every row of design 4.5 (no `run.json` is `not_started`; fresh `launching` is `running`; `launching` older than 30 s with no wrapper is `died`; `run.cancelled` wins over `run.exit`; exit 0 is `finished`; exit 1 is `died`; `run.timeout` is `died`; alive wrapper with matching ticks and cmdline is `running`; reused pid (different ticks) is `died`); `test_finished_never_without_receipts` (T-UNIT-08: `run.exit` is never present before the end record, asserted against the wrapper in the next file); `test_estimate_rate` (samples `(0, 0.0)` and `(60, 5.0)` with 38 ps remaining give 7.2 ns/day and ETA about 456 s; fewer than two samples or spacing under 30 s gives `(None, None)`; T-UNIT-09). `tests/unit/test_wrapper.py`: `test_wrapper_runs_child_and_writes_files_in_order` (child `[sys.executable, "-c", "print(1)"]`; an injected hook asserts the ledger already contains the `end` record when `run.exit` is written; `run.exit` holds `0`); `test_wrapper_nonzero_exit_recorded`; `test_wrapper_timeout_writes_run_timeout` (child sleeps 30 s, `max_wall_hours=0.0003`; `run.timeout` exists, `run.exit` non-zero; T-INT-06 in miniature); `test_launch_survives_parent` marked `posix` (launch, then read `run.exit` after a short wait). Add to `tests/unit/test_approval.py` or a new `tests/unit/test_exec_tools.py`: `test_run_mdrun_check_order_and_states` (no approval gives `E_NOT_APPROVED`; stage `PENDING` gives `E_STAGE_STATE`; stage `RUNNING` elsewhere gives `E_MDRUN_RUNNING`; stale `.mdp` gives `E_INPUT_MODIFIED`; changed binary gives `E_BINARY_CHANGED`); `test_write_mdp_retry_and_reset` (retry within bounds invalidates the stage and later ones and does not change `plan_sha256`; `reset=true` on a `FAILED` stage keeps the approval and adds no retry entry; both refused with `E_MDRUN_RUNNING` while a process runs; T-UNIT-14 integration part); `test_cancel_run_noop_when_idle` (`cancelled False`); `test_resume_rules` (resume allowed from `FAILED` caused by `died` or from `CANCELLED` with a checkpoint, archiving `run.*` into `run_history/1/`; refused after a failed gate on a finished run).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_runs.py tests/unit/test_wrapper.py tests/unit/test_exec_tools.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the interfaces. On `win32` use `creationflags` for a detached process and skip `/proc` use; the Linux implementation is the contract.
- [ ] **Step 4: Run** the three files. Expected: PASS (POSIX-marked tests SKIPPED on this host, to be run on Linux).
- [ ] **Step 5: Commit** `feat: execution tools, mdrun wrapper, process-state logic`.

### Task 18: M2 integration, tracker update, and wrap-up (TASK-060 remainder, T-INT-04, T-INT-08)

**Files:**
- Create: `tests/integration/test_m2_flow.py`, `tests/integration/test_mcp_plain_client.py`; Modify: `tracker.md`

**Interfaces:**
- Consumes: every tool from Tasks 7 to 17, `McpSession`, `server_params`, the fake binary.

- [ ] **Step 1: Write failing tests**: `test_plain_mcp_client_lists_15_m2_tools_and_run_is_unavailable` (T-INT-08 slice; start the server with no token and no override; `list_tools` names are exactly tools 1 to 12 and 19 to 21 of design 4.1; `run_mdrun` on a prepared job returns `E_APPROVAL_UNAVAILABLE`); `test_simulated_flow_with_fake_binary` marked `posix` (T-INT-04 slice: `GMX_BIN` is the fake and `SIMAGENT_ALLOW_FAKE=1`; `create_job` reports `simulated` and the job directory lives under `runs_simulated/`; `build_water_box` gives 4 atoms per `SOL`; `set_plan`, `record_approval` (CLI-side token), `write_mdp`, `run_grompp`, `run_mdrun` for `em` run, `get_run_status` reaches `finished`, `validate_stage` returns physical checks as `skipped_simulated`); `test_real_flow_em` marked `real_gmx` (T-INT-01 slice: the same flow against the real binary for `build` and `em` only; EM gate passes); `test_job_survives_server_restart` (T-INT-10: `get_job` and `list_jobs` return plan, stage statuses, and approval presence after the server process is restarted).
- [ ] **Step 2: Run** `uv run pytest tests/integration -v`. Expected: the plain-client and restart tests FAIL first; POSIX and real tests SKIPPED on this host.
- [ ] **Step 3: Fix** whatever the integration tests reveal in earlier tasks' code (ledger each ruling).
- [ ] **Step 4: Run** `uv run pytest -v`, `uv run ruff check .`, `uv run mypy src`. Expected: all PASS, zero ruff and mypy errors.
- [ ] **Step 5: Update `tracker.md`**: set TASK-010 through TASK-028, TASK-035, TASK-050, TASK-060, and TASK-068 to Done with the test names as evidence, except items whose tests were SKIPPED on this host (mark those "Done on authoring host; POSIX and real-gmx tests pending on the Linux host"); record the date in the snapshot. Commit `docs: mark M1 and M2 tasks done in tracker`.
- [ ] **Step 6: Commit** `test: M2 integration flows`.
