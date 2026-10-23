# Architecture

| Field | Value |
|---|---|
| Document | `architecture.md` v1.8 |
| Date | 2026-10-23 |
| Implements | `prd.md` FR-01 to FR-25, NFR-01 to NFR-10 (traceability in section 14) |
| Companion | `design.md` (schemas and parameters), `rules.md` (guardrails) |

---

## 1. Architectural drivers

| Driver | Source | Consequence |
|---|---|---|
| Never present unproven results as real | AG1, AG4, FR-11 | Evidence is produced by the MCP server in code (provenance ledger, gates), not by the LLM |
| Science decisions belong to the researcher | AG2, FR-02, FR-04 | Explicit clarification and approval states in the agent workflow |
| Agent and GROMACS share one Linux host | C-02 | stdio MCP transport, local subprocesses, no network hop to GROMACS |
| GROMACS must be reachable regardless of environment | Section 2 of PRD | Explicit binary path in config, startup verification, environment captured per run |
| Long runs outlive sessions | NFR-03, FR-15 | `mdrun` detached from the server; state on disk; resumable |
| Provider can change | NFR-08 | Thin LLM client behind an interface; OpenAI-compatible protocol |

## 2. System context

```
 Researcher (terminal)
        |  natural language
        v
 +-----------------------------------------------------------+
 |                    Linux host (single machine)            |
 |                                                           |
 |  +------------------+   stdio MCP    +------------------+ |
 |  |  Agent process   | <------------> |  MCP server      | |
 |  |  (simagent.agent)|  JSON-RPC      |  (gmx-mcp)       | |
 |  +--------+---------+                +----+--------+----+ |
 |           |                               |        |      |
 |   HTTPS   |                      subprocess|        | read |
 |           v                               v        v      |
 |  +----------------+              +-----------+  +-------+ |
 |  | LLM endpoint   |              | gmx 2025.3|  | runs/ | |
 |  | gpt6-luna      |              | (detached)|  | store | |
 |  | OpenAI-compat. |              +-----------+  +-------+ |
 |  +----------------+                                       |
 +-----------------------------------------------------------+
```

External dependencies: the LLM endpoint (network), GROMACS (local binary), the local filesystem. Nothing else.

## 3. Components

| ID | Component | Responsibility | Package |
|---|---|---|---|
| CMP-01 | CLI front end | Terminal chat, plan display, approval prompts, progress display | `simagent.cli` |
| CMP-02 | Conversation manager | Holds dialogue, token budget, tool-call loop | `simagent.agent.loop` |
| CMP-03 | Intent parser | One structured-output LLM call turns natural language into `SimulationRequest` fields with origin tags; code finds the missing fields | `simagent.agent.intent` |
| CMP-04 | Planner | Pure function from `SimulationRequest` to `WorkflowPlan`; `why` text comes from templates | `simagent.agent.planner` |
| CMP-05 | Workflow controller | Deterministic code that sequences stages, polls status (at most once per 10 s), applies allowed retries, and resumes; the sole caller of `run_grompp`, `run_mdrun`, and `validate_stage` (the CLI also calls `cancel_run` on `/cancel`, and the model may read `get_run_status`) | `simagent.agent.workflow` |
| CMP-06 | Diagnostician | Maps failures to causes and fixes using the playbook | `simagent.agent.diagnose` |
| CMP-07 | Analyst and reporter | Chooses analyses; drafts the four prose sections and calls `write_report`, after which the server assembles `report.md` from its own header and tables | `simagent.agent.report` |
| CMP-08 | LLM client | OpenAI-compatible chat with tools, retries, token accounting | `simagent.llm` |
| CMP-09 | Memory store | Preferences, playbook, job index | `simagent.memory` |
| CMP-10 | MCP client | Connects to the server, exposes tools to CMP-02 | `simagent.mcp_client` |
| CMP-11 | MCP server | Tool registry, input validation, error mapping | `simagent.mcp_server.app` |
| CMP-12 | GMX runner | Builds argv and the process environment, runs `gmx`, captures output, runs the environment probe, writes provenance | `simagent.core.gmx` |
| CMP-13 | System builder | Box, solvation, exact molecule count, topology | `simagent.core.build` |
| CMP-14 | MDP renderer | Template rendering and range validation | `simagent.core.mdp` |
| CMP-15 | Gate engine | Stage checks | `simagent.core.gates` |
| CMP-16 | Run manager | Launches the detached `mdrun` wrapper, reads pid and exit files, cancels, resumes | `simagent.core.runs`, `simagent.core.mdrun_wrapper` |
| CMP-17 | Analysis library and summary writer | XVG parsing, block averaging, drift, RDF and MSD wrappers; `build_summary` writes `summary.json`, `report_tables.md`, and plots | `simagent.analysis`, `simagent.core.summary` |
| CMP-18 | Provenance ledger | Append-only `provenance.jsonl`, hashing | `simagent.core.provenance` |
| CMP-19 | Config loader | Environment plus `config.yaml`, secret handling | `simagent.config` |

Supporting modules without their own component ID: `core/jobs.py` (job directory and `job.json` access, used by CMP-11), `core/paths.py` (path confinement), `core/errors.py` (error codes), and the `templates` package data (used by CMP-14).

### 3.1 Trust boundary

The LLM is untrusted for facts and trusted only for language: interpreting requests, proposing plans, explaining results. Everything that decides pass or fail, and every number in a report, comes from CMP-11 to CMP-18 (numbers are written to `summary.json` and rendered into `report_tables.md` by `build_summary`, CMP-17; `write_report` inserts that file verbatim into `report.md`). The agent may not write to `provenance.jsonl`, may not set a gate result, may not call `record_approval` (the client withholds it from the model's function list, and the model never holds the approval token), and may not run `gmx` except through MCP tools. The server independently refuses `run_mdrun` without a recorded approval (section 9). The server, not the agent, computes `plan_sha256` in `set_plan`, and the CLI records approval against that hash (`design.md` section 4.3).

```
 [LLM: language, proposals]  --tool calls-->  [MCP server: validation, execution, evidence]
        untrusted for facts                          source of truth
```

### 3.2 Control flow and internal interfaces

The model is a proposer and explainer; deterministic code sequences the work (`design.md` section 14).

| Interface | Contract |
|---|---|
| CMP-02 conversation loop to CMP-08 LLM client | `chat(messages, functions) -> message` with tool calls; token accounting and retries inside CMP-08 |
| CMP-02 to CMP-03 | Structured `submit_request` function call returns `SimulationRequest` fields with origins |
| CMP-05 controller to CMP-10 MCP client | `call(tool, args) -> envelope` for every MCP tool; only CMP-05 and the CLI hold the full tool set |
| CMP-05 to CMP-04 planner | `plan(request, build_result) -> WorkflowPlan` (pure function) |
| CMP-05 to CMP-06 diagnostician | `diagnose(stage, log_excerpt, playbook) -> Diagnosis` (pure function; the model only phrases the explanation) |
| CLI (CMP-01) to CMP-10 | Calls `get_job` to render the plan and `record_approval` with the per-launch token |
| CMP-02, CMP-05 to CMP-09 memory | `get(key)`, `put(entry)`, `forget(key)`; no other component touches memory files |
| CLI input and controller | An `asyncio` loop runs the input task and the controller task concurrently, so `/cancel` is handled during poll sleeps (`design.md` section 14.5) |

## 4. Workflow state machine (agent side)

```
INTAKE -> CLARIFY -> PLAN -> AWAIT_APPROVAL -> EXECUTE -> ANALYZE -> REPORT -> DONE
            ^ |        ^           |               |          |         |
            | +--------+- revise --+               +----------+---------+   (stage or tool failure)
            |                                                  v
            +------------ more information ------ ESCALATE <- DIAGNOSE -> RETRY -> EXECUTE
                                                      |
                                      PLAN (revise, re-approve) | FAILED
any non-terminal state -> CANCELLED (researcher cancels; running stages are cancelled first)
```

| State | Entry condition | Exit |
|---|---|---|
| INTAKE | New message | `SimulationRequest` drafted |
| CLARIFY | Science-critical field missing | All such fields set, or the researcher accepts disclosed defaults |
| PLAN | Request complete | Plan produced. The controller calls `create_job`, `build_water_box` (which includes a dry `grompp` check), and `set_plan` here; none start `mdrun`, and the plan shows the real box edge. |
| AWAIT_APPROVAL | Plan shown | Researcher approves, edits (back to PLAN), or rejects |
| EXECUTE | Approved | All stages passed gates, or failure |
| DIAGNOSE | A stage failed during EXECUTE, or a tool failed during ANALYZE or REPORT | Fix proposed; RETRY if allowed (max 2 per stage), else ESCALATE |
| ANALYZE | Production passed | Analysis outputs exist |
| REPORT | Analysis done | `report.md` written |
| RETRY | The plan allows the diagnosed fix and the failing stage has used fewer than 2 retries | The fix is applied; the failing stage, and any earlier stage the fix invalidates (in order), re-runs from `grompp`; for ANALYZE the whole analysis sequence re-runs after the reset (`design.md` section 3.4); for REPORT `write_report` repeats; then back to EXECUTE |
| DONE | Report delivered | Final message states execution mode, stages passed, warnings count, report path |
| ESCALATE | Retries exhausted, the fix would change the science, or the failure is unknown | The researcher chooses: revise the plan, including any fix outside the plan's automatic retries (back to PLAN, then `set_plan` and re-approval); resume a died stage from its checkpoint (`run_mdrun(resume=true)`); supply missing information (back to CLARIFY); or stop (FAILED) |
| FAILED | The researcher stops after ESCALATE, or an unrecoverable error occurs | Terminal; the job keeps all evidence and is listed with status `failed` |
| CANCELLED | The researcher cancels | Terminal for the session; running stages are cancelled first through `cancel_run`, and `/resume <job_id>` may later resume a cancelled stage from its checkpoint |

Per-stage status, process state, and gate check vocabularies are defined once in `design.md` section 3.4 (`PENDING, READY, RUNNING, GATING, PASSED, FAILED, CANCELLED` for stages; `not_started, running, finished, died, cancelled` for the `mdrun` process).

## 5. Execution model for GROMACS

1. The MCP server builds `argv` as a list. No shell is invoked (`shell=False`), so no quoting or injection issues.
2. Short commands (`editconf`, `solvate`, `grompp`, `energy`, `rdf`, `msd`, `check`) run synchronously with a timeout and captured output.
3. `mdrun` runs under a small Python wrapper (`mdrun_wrapper`) started in a new session, so an agent or server crash leaves it running. The wrapper writes `run.json`, waits for `gmx`, and writes the provenance "end" record and then, last, `run.exit` with the real exit code, so a poller that sees `run.exit` always finds the receipts. `get_run_status` decides the process state from `run.exit`, the wrapper pid, its start time and `/proc/<pid>/cmdline` (guarding against pid reuse), and the `.log` tail; it never trusts memory alone. Details: `design.md` section 4.5.
4. Every job invocation, short or long, appends provenance records at start and end (CMP-18); environment-check invocations (version, probe) are summarized in `job.json.environment.probe` instead (`design.md` section 4.6).
5. The environment passed to `gmx` is built, not inherited: a configurable passthrough list (`gmx.env_passthrough`), plus `GMXDATA`, `GMXBIN`, and `LD_LIBRARY_PATH` derived from the binary's install prefix exactly as `GMXRC` would set them, without running a shell. `LUNA_*` variables are never passed to the server. The resulting non-secret values are recorded in each provenance entry. This directly addresses the "GROMACS reachable in one environment but not another" problem (`design.md` section 2.1).
6. Thread settings come, in this order, from the request's `hardware` field (a stored preference only proposes a value for it), the environment (`GMX_NTOMP`, `GMX_NTMPI`), and `config.yaml` (`ntomp`, `ntmpi`); the LLM never chooses them.

## 6. Data and storage layout

```
runs/<job_id>/                      (simulated jobs live under runs_simulated/<job_id>/ only)
  job.json                  request, plan, plan_sha256, state, approval, environment, config snapshot
  events.jsonl              structured event log
  provenance.jsonl          append-only ledger of gmx invocations
  01_build/   water_raw.gro water.gro topol.top build.json
  02_em/      em.mdp em_out.mdp em.tpr em.gro em.edr em.trr em.log run.json run.exit
  03_nvt/     nvt.mdp nvt_out.mdp nvt.tpr nvt.gro nvt.cpt nvt.edr nvt.log nvt.xtc run.json run.exit
  04_npt/     npt.mdp npt_out.mdp npt.tpr npt.gro npt.cpt npt.edr npt.log npt.xtc run.json run.exit
  05_prod/    prod.mdp prod_out.mdp prod.tpr prod.gro prod.cpt prod.edr prod.log prod.xtc run.json run.exit  (every stage dir holds grompp.out; mdrun stage dirs also hold mdrun.out, mdrun.err, and possibly run.cancelled, run.timeout, run_history/)
  06_analysis/ <stage>_<term>.xvg  rdf_OO.xvg  msd.xvg  summary.json  report_tables.md  *.png
  report.md
  superseded/<n>/           files of stages invalidated by a re-run
  <stage dir>/run_history/<n>/  archived run.* files from earlier attempts
runs/_probe/<timestamp>/    environment probe runs (removed on success; not created in simulated mode, which skips the probe)
memory/
  preferences.json   playbook.json   jobs_index.json
```

Rules: all paths the server accepts are resolved against the job directory and rejected if they escape it (RULE-SEC-02). A job whose `execution_mode` is `simulated` is created only under `runs_simulated/`, so no simulated data can sit in the real-run store (RULE-I-04).

## 7. Interfaces

### 7.1 MCP (agent to server)

Transport: stdio, JSON-RPC, official MCP Python SDK, version 2.x (`mcp>=2,<3`; the server class is `MCPServer` in `mcp.server.mcpserver`, the former `FastMCP`; the client uses `ClientSession` and `stdio_client`). The server exposes 21 tools. Full contracts, error-code mapping, and exact commands are in `design.md` section 4.

| Group | Tools |
|---|---|
| Environment | `gmx_check_environment` |
| Job | `create_job`, `set_plan`, `record_approval` (needs the approval token; withheld from the LLM), `get_job`, `list_jobs`, `list_artifacts`, `get_log_excerpt` |
| Build | `build_water_box`, `write_mdp` |
| Execute | `run_grompp`, `run_mdrun`, `get_run_status`, `cancel_run`, `validate_stage` |
| Analysis | `extract_energy`, `compute_rdf`, `compute_msd`, `analyze_timeseries`, `build_summary`, `write_report` |

Every tool returns the envelope `{ok, data, error{code,message,hint}}`. Over MCP the envelope travels as `structuredContent` with `isError = not ok` (**[verify TASK-020]** against the pinned SDK), and the server writes nothing to stdout except protocol frames. Error codes are in `design.md` section 4.2.

### 7.2 LLM (agent to model)

OpenAI-compatible Chat Completions with `tools`. Settings from `LUNA_API_KEY`, `LUNA_BASE_URL`, `LUNA_MODEL`. MCP tool schemas are converted to the OpenAI function format by CMP-10, and the model is offered only the read-only allow-list of `design.md` section 14.1. If the endpoint rejects `tools`, CMP-08 switches to JSON-schema mode where the model returns `{"tool": ..., "args": ...}` (assumption A-01 in the PRD).

### 7.3 Configuration

| Name | Default | Meaning |
|---|---|---|
| `GMX_BIN` | `/home/rontu/Rontu/software/software_install/gromacs-2025.3/bin/gmx` | GROMACS executable |
| `GMX_RUNS_DIR` | `./runs` | Root of job directories |
| `GMX_NTOMP` | auto | OpenMP threads per rank |
| `GMX_NTMPI` | auto | Thread-MPI ranks |
| `LUNA_API_KEY` | none, required | LLM key, never persisted |
| `LUNA_BASE_URL` | none, required | OpenAI-compatible endpoint |
| `LUNA_MODEL` | `gpt6-luna` | Model identifier |
| `SIMAGENT_REQUIRE_APPROVAL` | `1` | Plan approval gate |
| `SIMAGENT_TOKEN_BUDGET` | `200000` | Per-job token cap |
| `SIMAGENT_ALLOW_FAKE` | `0` | Permit the fake binary; jobs are then labeled SIMULATED and stored under `runs_simulated/` |
| `SIMAGENT_LOG_PROMPTS` | `0` | Store prompt text in events (default stores only hashes) |
| `SIMAGENT_MEMORY_DIR` | `./memory` | Agent memory location |
| `SIMAGENT_USER` | OS user name | Researcher id for memory |
| `SIMAGENT_APPROVAL_TOKEN` | generated per launch | Token that `record_approval` requires; set only by the CLI in the server environment; secret |
| `SIMAGENT_ALLOW_UNAPPROVED` | unset | Operator opt-out that lets a client without the token run jobs; logged and shown in the report |

Further non-secret keys (`gmx.expected_sha256`, `gmx.env_passthrough`, `gmx.cpt_minutes`, `gmx.max_wall_hours`, gate thresholds) live in `config.yaml`; see `design.md` section 2.

## 8. Real-run integrity mechanism

This answers the observed "mock versus real" problem and is a first-class architectural concern.

1. **Binary identity**: `gmx_check_environment` checks that the file is an ELF executable (a script that merely prints a version string is rejected), records the resolved path and SHA-256 (optionally pinned by `gmx.expected_sha256`), and checks the version string.
2. **Probe run**: the environment check performs a real end-to-end micro-run in the server's own environment (solvate, `grompp`, 20-step `mdrun`, `gmx check` on the `.edr`). A binary that cannot compute is caught before any job exists, in the environment that will actually run the jobs. See `design.md` section 4.6.
3. **Receipted execution**: every produced artifact is linked in `provenance.jsonl` to the invocation that produced it (server-written files, such as rendered inputs and the trimmed coordinates, carry an `internal` receipt with their hash instead; for gmx invocations: argv, exit code, start and end time, stdout and stderr hashes, SHA-256 of each output file). The `mdrun` exit code comes from the wrapper's `run.exit`.
4. **Gate evidence**: `validate_stage` re-reads files and hashes at check time and re-parses the trajectory and energy files with `gmx check`. A file with no matching receipt fails the gate (`no_receipt`).
5. **Execution mode**: each job has `execution_mode` of `real` or `simulated`. The mode comes only from the binary identity and the probe: an ELF that passes both is `real` whatever `SIMAGENT_ALLOW_FAKE` says; a script is `simulated` only when its version output contains `FAKE` and the flag is set, and any other script gives `E_FAKE_BINARY` (`design.md` section 4.6). The mode is stamped in `job.json`, in every report header, and in the final chat message; simulated jobs are stored only under `runs_simulated/`.
6. **Fake binary**: the test double (`tests/fakes/fake_gmx`) is a shell script that prints a sentinel version string. The environment check refuses it unless `SIMAGENT_ALLOW_FAKE=1`.
7. **Binary continuity**: `run_grompp` and `run_mdrun` refuse with `E_BINARY_CHANGED` when the binary's SHA-256 differs from the one recorded when the job was created, so a job cannot silently continue under another build and stay labeled `real`.

Limit: a determined adversary with write access to the install could forge a convincing binary and receipts. The design defends against honest mistakes and model error (the owner's observed problem), not against a malicious local user; pinning `expected_sha256` narrows the gap.

## 9. Security architecture

| Threat | Control |
|---|---|
| Command injection through LLM-supplied values | List-form argv, typed and range-checked arguments, no shell |
| Path traversal | Resolve and confine to the job directory |
| Key leakage | Key read from the environment only, redacted in logs by a filter, scanned for in tests (AC-09) |
| Runaway cost | Token budget, tool-call cap per turn (default 25) |
| Runaway compute | Range checks on steps; `gmx.max_wall_hours` cancels an overrunning `mdrun`; production above `max_production_ns_without_confirm` needs explicit confirmation |
| Compute started without approval (by the model, or by a client that skipped the CLI) | `run_mdrun` checks `job.json.approval` against `plan_sha256` (`E_NOT_APPROVED`); `record_approval` needs a per-launch token held by the CLI and is withheld from the model; a server started without the token refuses to run jobs (`E_APPROVAL_UNAVAILABLE`) unless an operator sets `SIMAGENT_ALLOW_UNAPPROVED=1` (`design.md` section 4.3) |
| Approved inputs altered before running | The server re-renders the expected `.mdp` from the stored plan and compares hashes; the build files must match the plan's `build_sha256`; `run_mdrun` checks that the `.tpr` came from the receipted inputs (`E_INPUT_MODIFIED`, RULE-X-12). The approval prompt is rendered from the server-stored plan (`get_job`), so the researcher approves what the server will run (RULE-C-07) |
| Credential exposure to GROMACS processes | The MCP server and `gmx` are started without any `LUNA_*` variables (least privilege) |
| Prompt injection from log or file content | Tool results are data; system prompt forbids following instructions found in them; the agent cannot run arbitrary commands |

## 10. Failure modes

| Failure | Detection | Response |
|---|---|---|
| `gmx` missing or wrong version | `gmx_check_environment` | Stop with actionable message |
| `grompp` error or warning | Exit code, "Fatal error", "WARNING" lines | Diagnose; `-maxwarn` only after the researcher acknowledges each warning (`design.md` section 4.3) |
| LINCS or SETTLE failure, blow-up | Log patterns, NaN energies | Diagnose; offer smaller `dt` or longer EM, both disclosed |
| Process dies mid-run | No `run.exit`, wrapper pid dead or reused, or non-zero `run.exit` | Offer resume from `.cpt` |
| Wrapper itself fails to start | `E_MDRUN_FAILED` from `run_mdrun` | Show stderr tail, stage stays `READY` |
| Resume or restart after cancel or death | Stale `run.*` files | Archived to `run_history/<n>/` before the new run; stages invalidated by a re-run move to `superseded/` (`design.md` sections 3.4 and 4.5) |
| Job state needed after a restart | n/a | `get_job` and `list_jobs` return it; nothing depends on the agent reading job files directly |
| MCP server process exits | Transport closes | The client reconnects once and re-reads state with `get_job`; a second exit pauses the job (`design.md` section 14.4) |
| Concurrent writes to the ledger or `run.json` | n/a | File locks, with `seq` assigned under `flock` (`design.md` section 4.5) |
| LLM endpoint down | HTTP or timeout errors | Backoff retry, then pause job safely; running `mdrun` unaffected |
| Disk full | `ENOSPC` | Stop new stages, report |
| Agent crash | Restart | Reattach via `job.json`, pid, logs |

## 11. Technology choices and decisions

| ADR | Decision | Reason | Alternative considered |
|---|---|---|---|
| ADR-01 | Python 3.11+ | MCP SDK, scientific stack, `subprocess` control | Go, Node |
| ADR-02 | Official MCP Python SDK with stdio transport | Local-only, simple, no ports to secure | HTTP/SSE transport |
| ADR-03 | `openai` Python client with configurable `base_url` | Works with any OpenAI-compatible endpoint | Hand-rolled HTTP |
| ADR-04 | Detached `mdrun` with pid and log files | Survives agent crash | Child process of the server |
| ADR-05 | Gates and provenance in the server, not in prompts | LLM must not be the source of truth | Prompt-only checks |
| ADR-06 | JSON and Markdown files, no database | One host, easy to audit and diff | SQLite |
| ADR-07 | OPLS-AA with `tip4p.itp` for the reference workflow | Ships with GROMACS, TIP4P topology included | TIP4P-Ew with another force field (accepted but untested in version 1); TIP4P/2005 needs a topology that the researcher supplies |
| ADR-08 | `numpy` and `matplotlib` for analysis; GROMACS tools for RDF and MSD | Reproducible numerics, GROMACS-native outputs | MDAnalysis (possible later) |
| ADR-09 | C-rescale barostat, V-rescale thermostat | Correct ensemble fluctuations with robust behavior in GROMACS 2025 | Berendsen, Parrinello-Rahman |
| ADR-10 | `mdrun` under a waiting wrapper process | Captures the real exit code and survives crashes; a bare detached child cannot be waited on after the server dies | Detach and infer success from the log only |
| ADR-11 | Approval enforced in the server (`record_approval` with a per-launch token, `E_NOT_APPROVED`, input-hash checks) | The model cannot start compute by omission, by calling `record_approval`, or by editing inputs after approval; a generic MCP client cannot run jobs without the operator's opt-in | Approval only in the agent state machine |
| ADR-12 | Environment derived from the install prefix, no shell sourcing of `GMXRC` | No shell, reproducible, recorded | Spawn `bash -c "source GMXRC"` |

## 12. Deployment

Single machine, single user. A uv-managed virtual environment (`uv venv`, `uv sync`; `design.md` section 15) holds the package; `simagent` (CLI) spawns `gmx-mcp` as a child over stdio, with an explicit environment that excludes `LUNA_*` (console scripts: `design.md` section 1). GROMACS is used from its install prefix without modification. Authoring of documents happens on Windows; code and runs happen on Linux (context A-04).

## 13. Extensibility

New systems add a builder under `simagent.core.build` and templates under `templates/mdp`; the tool surface for execution, gating, and analysis is already system-agnostic. Cluster scheduling would replace CMP-16 behind the same `run_mdrun` and `get_run_status` contract.

## 14. Traceability

| Requirement | Component(s) |
|---|---|
| FR-01, FR-02 | CMP-03 |
| FR-03, FR-04 | CMP-04, CMP-05, CMP-01, CMP-11 |
| FR-05 | CMP-13 |
| FR-06 | CMP-14 |
| FR-07, FR-15 | CMP-05, CMP-16 |
| FR-08, FR-16 | CMP-16 |
| FR-09 | CMP-15 |
| FR-10 | CMP-06 (shipped playbook and diagnosis), CMP-09 (learned entries and counters) |
| FR-11, FR-14 | CMP-18 |
| FR-12, FR-13 | CMP-17, CMP-07 |
| FR-17, FR-18, FR-23 | CMP-19, CMP-12 |
| FR-19 | CMP-08, CMP-19 |
| FR-20 | CMP-09 |
| FR-21 | CMP-08, CMP-02, CMP-19 (redaction) |
| FR-22 | CMP-11, CMP-10 |
| FR-25 | CMP-11 |
| NFR-01 | CMP-11, CMP-12, CMP-19 |
| NFR-02 | CMP-13, CMP-14, CMP-04 |
| NFR-03 | CMP-16 |
| NFR-04 | CMP-02, CMP-11 (event log) |
| NFR-05 | CMP-11 (`tool_time_s` instrumentation) |
| NFR-06 | build and CI configuration |
| NFR-07 | test suite |
| NFR-08 | CMP-08 |
| NFR-09 | CMP-02, CMP-08 |
| NFR-10 | CMP-16, CMP-11 |
