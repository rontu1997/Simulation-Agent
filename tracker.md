# Tracker

| Field | Value |
|---|---|
| Document | `tracker.md` v1.8 |
| Date | 2026-10-23 |
| Purpose | Plan, status, traceability, decisions, and document quality scores for SimAgent |
| Update rule | Update in the same change as the work, with evidence (RULE-D-04) |

---

## 1. Legend

| Status | Meaning |
|---|---|
| Done | Finished and verified; the Evidence column names the test, output, or score that proves it |
| In progress | Work started or written but not yet verified |
| Not started | Planned |
| Blocked | Waiting on something named in Notes |

Priority: P0 must for v1, P1 should, P2 could. Dependencies list task numbers (`013` means `TASK-013`) and must be finished first; a P0 task never depends on a lower-priority task. Task numbers are stable labels, not an execution order; order work by milestone and the Depends column. Rows are listed in milestone order.

## 2. Snapshot (2026-10-23)

| Milestone | Status | Notes |
|---|---|---|
| M0 Documents | In progress | Six documents written; round 8 scored and corrected (v1.8); round 9 pending (section 7.3) |
| M1 Foundation | Done on the authoring host | TASK-010 to 016 and 018 done; TASK-017 CI file written, first CI run pending; TASK-013 and TASK-014 live checks pending (real binary, live endpoint) |
| M2 MCP server | Done on the authoring host | TASK-019 to 028, 035, 050, 060, 068 done. Suite on Windows: 312 passed, 8 skipped; on Linux (WSL Ubuntu): 316 passed, 4 skipped. Pending on the Linux host with GROMACS: `test_real_flow_em` (T-INT-01 slice), `test_build_real`, `test_envcheck_real`; the nvt, npt, prod gates are untested against a real run |
| M3 Workflow engine | Not started | |
| M4 Agent | Not started | |
| M5 Analysis and report | Not started | |
| M6 Hardening and acceptance | Not started | |

Next action: run the real-GROMACS tests on the Linux host, then M3 (workflow engine); M0 scoring round 9 is still open (section 7.3). No time estimates exist yet because team size and GROMACS host speed are unknown (OI-02); first estimates come from TASK-013 and TASK-063.

## 3. Task list

| Task | Milestone | Description | Requirements | Depends | Priority | Status | Evidence |
|---|---|---|---|---|---|---|---|
| TASK-001 | M0 | Write `prd.md` | all | none | P0 | In progress | Awaiting score above 95 |
| TASK-002 | M0 | Write `architecture.md` | all | 001 | P0 | In progress | same |
| TASK-003 | M0 | Write `design.md` | all | 002 | P0 | In progress | same |
| TASK-004 | M0 | Write `rules.md` | RULE series | 001 | P0 | In progress | same |
| TASK-005 | M0 | Write `memory.md` | FR-20 | 001 | P0 | In progress | same |
| TASK-006 | M0 | Write `tracker.md` | n/a | 001 to 005 | P0 | In progress | same |
| TASK-007 | M0 | Score all six documents above 95, fixing defects each round | n/a | 001 to 006 | P0 | In progress | Section 7.3 |
| TASK-010 | M1 | Repository scaffold from the existing `pyproject.toml` and `uv.lock` (`uv venv`, `uv sync`, `design.md` section 15): create `src/simagent`, add a `[build-system]` table and `[project.scripts]` entries for `simagent` and `gmx-mcp`, remove `package = false`, keep the `mcp>=2,<3` pin, configure `ruff` (including rules S602 and S605 for NFR-01), `mypy`, `pytest` | NFR-01, NFR-06, NFR-07, C-05 | 007 (the environment part was done early at the owner's request, D-38) | P0 | Done | tests/unit/test_scaffold.py; ruff, mypy, pytest configured in pyproject.toml |
| TASK-011 | M1 | Config loader, secret handling, masked repr, precedence | FR-17, FR-19, NFR-01 | 010 | P0 | Done | T-UNIT-06; tests/unit/test_config.py |
| TASK-012 | M1 | Provenance ledger (start, end, and ack records, `internal` receipts for server-written files, `flock`-assigned `seq`, append-only) and hashing | FR-11, FR-14 | 010 | P0 | Done | T-UNIT-19, T-UNIT-23; tests/unit/test_provenance.py |
| TASK-013 | M1 | `gmx` runner (argv, constructed environment, receipts) and `gmx_check_environment` with ELF check, version check, data check, and probe run; the probe strings tagged TASK-013 in `design.md` are confirmed here on the first run against the real binary and stored as fixtures | FR-17, FR-18, FR-11, FR-23 | 011, 012 | P0 | Done | T-INT-05, T-SEC-01, T-UNIT-21 (`E_TIMEOUT`, `E_DISK_FULL`); tests/unit/test_gmx_runner.py, test_envcheck.py, test_envcheck_fake.py. Pending on the Linux host: real-binary probe `tests/integration/test_envcheck_real.py` pending on the Linux host (skipped without GMX_BIN) |
| TASK-014 | M1 | LLM client (`design.md` section 14.3: request format, parallel tool calls, usage fallback, backoff, JSON-mode fallback) plus a live smoke test of `gpt6-luna` tool calling | FR-19, NFR-08, A-01 | 011 | P0 | Done | T-AGENT-05 and T-NFR-08 against a stub endpoint; the live T-LLM-01 runs when the owner supplies the endpoint and key (R-01 stays open until then); tests/unit/test_llm.py. Pending on the Linux host: live `gpt6-luna` smoke test `tests/integration/test_llm_live.py` pending (needs LUNA_API_KEY and LUNA_BASE_URL) |
| TASK-015 | M1 | Path confinement and error module | NFR-01, RULE-SEC-02 | 010 | P0 | Done | T-SEC-02; tests/unit/test_errors_paths.py |
| TASK-016 | M1 | Event log with key redaction and `tool_time_s` | FR-21, NFR-04, RULE-SEC-01 | 011 | P0 | Done | T-NFR-04; tests/unit/test_events.py |
| TASK-017 | M1 | CI pipeline using `uv sync --frozen`: lint, type check, unit tests with a coverage gate of 85% | NFR-06, NFR-07 | 010 | P0 | Done | CI run; .github/workflows/ci.yml written; local `ruff`, `mypy`, `pytest` pass. Pending on the Linux host: first CI run and the coverage gate pending (no remote yet) |
| TASK-018 | M1 | MCP client (`design.md` section 14.4: explicit server environment without `LUNA_*`, with the approval token; confirms the SDK behavior tagged TASK-018) and conversion of MCP tool schemas to OpenAI function format for the allow-listed tools; tested against a stub MCP server until TASK-020 | FR-22, FR-04, NFR-01 | 014 | P0 | Done | T-AGENT-03 (function-list part); the environment part of T-SEC-04 completes in TASK-062; tests/unit/test_mcp_client.py |
| TASK-019 | M2 | `create_job` (server-generated `job_id`), `core/jobs.py` (job directory, `job.json` access under lock, `invalidate`), and the stage transition table | FR-14, FR-18 | 013, 015, 020 | P0 | Done | T-UNIT-10, T-UNIT-13 (slice: transitions and `invalidate`; the analysis rows complete in TASK-051), T-UNIT-16; tests/unit/test_jobs.py, test_create_job_tool.py |
| TASK-020 | M2 | MCP server skeleton, tool registry, result envelope, stdout discipline; confirm SDK behavior tagged TASK-020 in `design.md` | FR-22 | 013, 015 | P0 | Done | T-INT-08 (slice: the tools available at M2; completes in TASK-045); tests/unit/test_server_skeleton.py, tests/integration/test_mcp_plain_client.py |
| TASK-021 | M2 | `build_water_box` (exact molecule count, `build_sha256`, dry `grompp` with `dry.mdp.j2`) and the solvate-determinism check behind NFR-02 | FR-05, AC-02 | 019, 020, 022, 023 | P0 | Done | T-UNIT-02, T-INT-07, T-NFR-02 (solvate determinism part); tests/unit/test_build.py, test_build_tool.py. Pending on the Linux host: real-binary T-INT-07 `tests/integration/test_build_real.py` pending on the Linux host |
| TASK-022 | M2 | MDP templates, renderer, range validation, structured retries (`write_mdp`) | FR-06 | 019, 020 | P0 | Done | T-UNIT-01, T-UNIT-15 (slice: the render side; the run side completes in TASK-023); tests/unit/test_mdp.py, test_exec_tools.py (write_mdp retry and reset) |
| TASK-023 | M2 | `run_grompp` with the two-pass warning flow (reads acknowledgements from `job.json`) and input-hash checks | FR-07, FR-09, FR-26 | 019, 022 | P0 | Done | T-UNIT-12 (with fixture acknowledgements; end to end after TASK-028), T-UNIT-15; tests/unit/test_grompp.py, test_exec_tools.py |
| TASK-024 | M2 | `run_mdrun`, `mdrun_wrapper` (end record before `run.exit`, `max_wall_hours`), the process-state decision table, and the binary-continuity check | FR-07, NFR-03, NFR-10 | 023 | P0 | Done | T-UNIT-08, T-UNIT-22, T-INT-06; tests/unit/test_runs.py, test_wrapper.py, test_exec_tools.py |
| TASK-025 | M2 | `get_run_status` (ns/day and ETA from deltas) and `cancel_run`; P0 because the controller (TASK-030) polls status and cancels, although FR-08 and FR-16 are Should in the PRD and the ETA part may finish late | FR-08, FR-16 | 024 | P0 | Done | T-UNIT-08, T-UNIT-09, T-INT-03 (slice: cancel; completes in TASK-031); tests/unit/test_runs.py, test_exec_tools.py (get_run_status, cancel_run) |
| TASK-026 | M2 | Gate engine (G-BUILD, G-EM, G-NVT, G-NPT, G-PROD, G-ANALYSIS, the `process_died` check, and the simulated-mode profile with in-process energy and check wrappers) and `validate_stage` | FR-09, FR-11 | 012, 024, 050 | P0 | Done | T-UNIT-03; tests/unit/test_gates.py, test_validation.py, test_exec_tools.py (validate_stage). Pending on the Linux host: nvt, npt, prod energy extraction in `core/stage_checks.py` follows documented GROMACS output and is untested against a real run; analysis gate arrives with M3 |
| TASK-027 | M2 | `list_artifacts` and `get_log_excerpt` (the diagnosis flow uses the latter) | FR-10, FR-14 | 012, 020 | P0 | Done | T-INT-08 (slice, completes in TASK-045); tests/unit/test_job_tools.py |
| TASK-028 | M2 | `record_approval` (kinds `plan` and `warnings`, approval token), server-side enforcement in `run_mdrun`, `E_APPROVAL_UNAVAILABLE` and operator-override paths | FR-04, FR-26, AC-11, NFR-10 | 020, 023, 024, 035 | P0 | Done | T-UNIT-07, T-UNIT-18; tests/unit/test_approval.py, test_exec_tools.py, tests/integration/test_mcp_plain_client.py |
| TASK-035 | M2 | `set_plan` (range validation, build verification, plan hash, `invalidate` logic, `E_MDRUN_RUNNING` guard, G-BUILD call) | FR-03, AC-13 | 019, 021, 022, 026 | P0 | Done | T-UNIT-11, T-UNIT-17, T-UNIT-20, T-UNIT-13 (slice: stage and `invalidate` rows; the analysis rows complete in TASK-051); tests/unit/test_approval.py, test_job_tools.py |
| TASK-050 | M2 | Analysis library (XVG parsing, block averages, drift) used by gates and by the analysis tools | FR-12 | 010 | P0 | Done | T-UNIT-05; tests/unit/test_analysis.py |
| TASK-060 | M2 | Fake binary (`tests/fakes/fake_gmx`), `runs_simulated/` routing, SIMULATED job label (the simulated gate profile is in TASK-026 and the banners in TASK-052) | FR-11, AC-06 | 013, 019 | P0 | Done | T-INT-04 (control-flow part; banner part completes in TASK-052); tests/unit/test_envcheck_fake.py, tests/integration/test_m2_flow.py::test_simulated_flow_with_fake_binary (run under WSL Ubuntu) |
| TASK-068 | M2 | `get_job` and `list_jobs` (needed by `/status`, `/resume`, and reattach) | FR-25 | 019, 020 | P0 | Done | T-INT-10; tests/unit/test_job_tools.py, tests/integration/test_m2_flow.py::test_job_survives_server_restart |
| TASK-030 | M3 | Workflow controller (`design.md` section 14.2 items 1, 4, 5, 8, 9: start check, EXECUTE, DIAGNOSE, reattach, limits) working from a stored plan, so M3 tests use plan fixtures; sole caller of `run_grompp`, `run_mdrun`, and `validate_stage`; state machine, stage transitions, 10 s polling floor | FR-07 | 018, 019, 025, 026, 028, 035, 068 | P0 | Not started | T-UNIT-13, T-AGENT-07 (slice: polling floor; the `/cancel` part completes in TASK-040) |
| TASK-031 | M3 | Resume protocol: stale-state archival, `RUNNING` to `FAILED` on `died`, checkpoint resume, downstream invalidation | FR-15, AC-16 | 024, 025, 030 | P0 | Not started | T-INT-09, T-INT-03 (completes the reattach part) |
| TASK-032 | M3 | Diagnostician, shipped playbook as package resource `playbook_default.json` (including EM non-convergence rows), retry attribution and limits | FR-10, AC-07 | 027, 030 | P0 | Not started | T-UNIT-04 (hand-written fixtures first; recorded real fixtures replace them in TASK-034), T-UNIT-14 |
| TASK-034 | M3 | Verify the **[verify]** items in `design.md` tagged TASK-034 (playbook regexes, gate log strings, `gmx check` output, the `gmx energy` term listing and stdin form, the `GPU support:` line) against real GROMACS output; store fixtures | FR-09, FR-10 | 013, 026, 032 | P0 | Not started | Fixtures in `tests/data/gmx_real/` recorded by `scripts/record_fixtures.sh` |
| TASK-040 | M4 | CLI (chat, progress, commands, approval prompt rendered from `get_job`, `/provenance`, and non-interactive `simagent run --request-file --yes`) | FR-04, US-04 | 018, 030, 043, 044 | P0 | Not started | T-AGENT-06, T-AGENT-09, T-AGENT-10 |
| TASK-041 | M4 | Intent parser to `SimulationRequest` | FR-01 | 014 | P0 | Not started | T-AGENT-01 (slice, completed in TASK-045) |
| TASK-042 | M4 | Clarification policy | FR-02, AC-01 | 041 | P0 | Not started | T-AGENT-01 (slice, completed in TASK-045) |
| TASK-043 | M4 | Controller items 2 and 3 (CLARIFY and PLAN): planner (pure function), `set_plan` call, plan display | FR-03, FR-04 | 019, 021, 035, 042 | P0 | Not started | T-AGENT-01 (slice, completed in TASK-045) |
| TASK-044 | M4 | Conversation loop, model function allow-list, tool-call cap, token budget | NFR-09, RULE-X-10, RULE-X-14 | 014, 018 | P0 | Not started | T-NFR-09, T-AGENT-03, T-AGENT-04 |
| TASK-045 | M4 | System prompts under `agent/prompts/` (`design.md` section 9.1) and the final console-script wiring for `simagent` and `gmx-mcp` | FR-01, FR-22 | 040, 041, 042, 043, 044 | P0 | Not started | T-AGENT-01, T-AGENT-02, T-INT-08 (slice, completes in TASK-052), T-AGENT-08 (slice, completes in TASK-046) |
| TASK-029 | M5 | `build_summary`: `summary.json`, `report_tables.md`, and PNG plots | FR-12, FR-13 | 026, 051 | P0 | Not started | T-REP-01, T-UNIT-21 (`E_NO_RECEIPT`, `E_GATE_FAILED`) |
| TASK-046 | M5 | Controller ANALYZE and REPORT steps (`design.md` section 14.2 items 6 and 7) and memory loading when the store exists | FR-12, FR-13 | 029, 030, 051, 052 | P0 | Not started | T-AGENT-08 |
| TASK-051 | M5 | `extract_energy`, `compute_rdf`, `compute_msd`, `analyze_timeseries` (analysis stage reset on entry) | FR-12 | 020, 050 | P0 | Not started | T-INT-01 (slice), T-UNIT-21 (`E_ANALYSIS_INPUT`) |
| TASK-052 | M5 | `write_report` tool (server-side assembly, unverified-number notice) with the SIMULATED and operator-override banners | FR-13, AC-06, AC-10 | 029, 060 | P0 | Not started | T-REP-01, T-REP-02, T-INT-04 |
| TASK-066 | M5 | Verify RDF and MSD flags and output parsing (`design.md` items tagged TASK-066) against real GROMACS; record fixtures | FR-12 | 051 | P0 | Not started | T-INT-01 |
| TASK-061 | M6 | Fault-injection suite and the test helper that injects faulty inputs with valid receipts (`design.md` section 4.3 item 6) | AC-07 | 032, 034 | P0 | Not started | T-INT-02 |
| TASK-062 | M6 | Secret-scan test and `LUNA_*` isolation test | AC-09, RULE-SEC-07 | 016, 018, 019, 020 | P0 | Not started | T-SEC-03, T-SEC-04 |
| TASK-063 | M6 | End-to-end acceptance run on real GROMACS; record wall time; calibrate gate thresholds | AC-01, AC-03, AC-04, AC-05, AC-10 | 034, 040, 043, 044, 045, 046, 052, 066 | P0 | Not started | T-INT-01 |
| TASK-064 | M6 | Memory store (L3 preferences, learned L4 entries and counters, L5 index) and tests T-MEM-01 to T-MEM-12 | FR-20, FR-10 | 043 | P1 | Not started | T-MEM-* |
| TASK-065 | M6 | NFR tests: reproducibility, event schema, overhead, provider swap | NFR-02, NFR-04, NFR-05, NFR-08 | 016, 063 | P0 | Not started | T-NFR-02, T-NFR-04, T-NFR-05, T-NFR-08 |
| TASK-067 | M6 | Measure the PRD section 10 success metrics (10 consecutive runs, injected-fault diagnosis rate, overhead, false-success count); post-v1 reporting | PRD section 10 | 061, 063 | P1 | Not started | Measurement report |

Evidence cells marked `(slice)` cite the part of a larger test that the task's milestone can already run; the full test completes in the later task named in the cell or in TASK-063.

## 4. Requirement coverage

Every requirement has at least one task and one verifying test. A blank cell is a defect in the plan.

### 4.1 Functional requirements

| Requirement | Tasks | Verified by |
|---|---|---|
| FR-01 | 041 | T-AGENT-01 |
| FR-02 | 042 | T-AGENT-01 |
| FR-03 | 035, 043 | T-AGENT-01 |
| FR-04 | 018, 028, 040, 043 | T-UNIT-07, T-AGENT-03 |
| FR-05 | 021 | T-UNIT-02 |
| FR-06 | 022 | T-UNIT-01 |
| FR-07 | 023, 024, 030 | T-INT-01 |
| FR-08 | 025 | T-UNIT-09, T-INT-01 |
| FR-09 | 026, 034 | T-UNIT-03 |
| FR-10 | 032, 034, 061 | T-UNIT-04, T-INT-02 |
| FR-11 | 012, 013, 026, 060 | T-UNIT-19, T-INT-01, T-INT-04 |
| FR-12 | 029, 050, 051, 066 | T-UNIT-05, T-INT-01 |
| FR-13 | 029, 052 | T-REP-01 |
| FR-14 | 012, 019, 027 | T-INT-01 |
| FR-15 | 031 | T-INT-09 |
| FR-16 | 025 | T-INT-03 |
| FR-17 | 011, 013 | T-UNIT-06 |
| FR-18 | 013, 019 | T-INT-05 |
| FR-19 | 011, 014 | T-UNIT-06, T-LLM-01 |
| FR-20 | 064 | T-MEM-01 to T-MEM-12 |
| FR-21 | 016, 062 | T-SEC-03, T-NFR-04 |
| FR-22 | 018, 020, 045 | T-INT-01, T-INT-08 |
| FR-23 | 013 | T-INT-05 |
| FR-24 | withdrawn (NG6) | n/a |
| FR-25 | 068 | T-INT-10 |
| FR-26 | 023, 028 | T-UNIT-12, T-INT-02 |

### 4.2 Non-functional requirements

| Requirement | Tasks | Verified by |
|---|---|---|
| NFR-01 | 011, 015, 062 | T-SEC-01, T-SEC-02, T-SEC-03, T-SEC-04 |
| NFR-02 | 021, 035, 065 | T-NFR-02 |
| NFR-03 | 024, 025 | T-INT-03 |
| NFR-04 | 016, 065 | T-NFR-04 |
| NFR-05 | 016, 065 | T-NFR-05 |
| NFR-06 | 010, 017 | CI run |
| NFR-07 | 017, 060 | Coverage gate, T-INT-04 |
| NFR-08 | 014, 065 | T-NFR-08 |
| NFR-09 | 044 | T-NFR-09 |
| NFR-10 | 024, 028 | T-UNIT-07, T-UNIT-08, T-INT-06 |

### 4.3 Acceptance criteria

| Acceptance criterion | Tasks | Verified by |
|---|---|---|
| AC-01 | 042, 043, 045, 063 | T-AGENT-01 |
| AC-02 | 021 | T-UNIT-02, T-INT-07 |
| AC-03, AC-04, AC-05 | 026, 063 | T-INT-01 |
| AC-06 | 052, 060 | T-INT-04 |
| AC-07 | 061 | T-INT-02, T-UNIT-12 |
| AC-08 | 025, 030, 031, 040, 045 | T-INT-03 |
| AC-09 | 062 | T-SEC-03 |
| AC-10 | 052, 063 | T-REP-01 |
| AC-11 | 028 | T-UNIT-07, T-AGENT-03 |
| AC-12 | 013 | T-INT-05 |
| AC-13 | 035, 065 | T-NFR-02 |
| AC-14 | withdrawn with FR-24 | n/a |
| AC-15 | 068 | T-INT-10 |
| AC-16 | 031 | T-INT-09 |

### 4.4 User stories

| Story | Requirements | Verified by |
|---|---|---|
| US-01 | FR-01, FR-03, FR-05, FR-07 | AC-01 |
| US-02 | FR-02 | AC-01 |
| US-03 | FR-04 | AC-11, T-AGENT-06 |
| US-04 | FR-08, FR-16 | T-UNIT-09, T-AGENT-07 |
| US-05 | FR-10 | AC-07 |
| US-06 | FR-09, FR-12, FR-13 | AC-10 |
| US-07 | FR-06, FR-14 | AC-13, T-UNIT-15 |
| US-08 | FR-11, FR-14, FR-17, FR-18, FR-21, FR-23 | AC-06, AC-12 |
| US-09 | FR-15, FR-25 | AC-08, AC-15, AC-16 |
| US-10 | FR-22 | T-INT-08 |

## 5. Decision log

"Owner" decisions come from the owner's explicit words. "Author" decisions were made without the owner's input; the owner may reverse any of them, and reversing one means updating the affected documents.

| ID | Date | Decision | Reason | By |
|---|---|---|---|---|
| D-01 | 2026-10-23 | Six documents named `prd.md`, `architecture.md`, `design.md`, `memory.md`, `rules.md`, `tracker.md` | Owner request | Owner |
| D-02 | 2026-10-23 | GROMACS at `/home/rontu/Rontu/software/software_install/gromacs-2025.3/bin/gmx` | Owner request | Owner |
| D-03 | 2026-10-23 | LLM `gpt6-luna`, OpenAI-compatible endpoint, key in an environment variable | Owner request and reply | Owner |
| D-04 | 2026-10-23 | Agent and GROMACS share one Linux host | Owner reply | Owner |
| D-05 | 2026-10-23 | Skip further questions; author supplies defaults | Owner reply | Owner |
| D-06 | 2026-10-23 | Use filename `architecture.md` for the owner's "architeture.md" | Assumed typo | Author |
| D-07 | 2026-10-23 | Python 3.11 and the official MCP Python SDK over stdio | Simplicity, local-only | Author |
| D-08 | 2026-10-23 | Reference system: OPLS-AA, `tip4p.itp`, 300 K, 1 bar, 100 ps NVT, 100 ps NPT, 1 ns production | Owner gave the stage list and 1 ns, not T or P or equilibration lengths | Author |
| D-09 | 2026-10-23 | Gates and provenance enforced in server code, not prompts | Owner's mock-versus-real experience | Author |
| D-10 | 2026-10-23 | Documents are scored by an independent reviewer agent, not by the author | Self-scoring is biased | Author |
| D-11 | 2026-10-23 | A document passes only when its score is strictly above 95 | Owner "more than 95" | Author |
| D-12 | 2026-10-23 | V-rescale thermostat (tau 0.1 ps), C-rescale barostat (tau 2 ps), dt 2 fs with rigid water, 1.0 nm cutoffs, PME | Robust GROMACS 2025 defaults for liquid water | Author |
| D-13 | 2026-10-23 | Environment variable names `LUNA_API_KEY`, `LUNA_BASE_URL`, `LUNA_MODEL`, `GMX_BIN`, `GMX_RUNS_DIR` | Owner asked for an environment variable but gave no names | Author |
| D-14 | 2026-10-23 | `filelock` library for memory file locking | RULE-D-09 justification: small, portable | Author |
| D-15 | 2026-10-23 | At most 3 questions per round, 2 automatic retries per stage, 85% line coverage target, 200,000-token budget, 25 tool calls per turn | Keep the researcher in control and costs bounded | Author |
| D-16 | 2026-10-23 | `mdrun` runs under a waiting wrapper process | Capture real exit codes while surviving agent crashes (ADR-10) | Author |
| D-17 | 2026-10-23 | Plan approval enforced by the MCP server, `record_approval` withheld from the model | The model must not be able to skip approval (ADR-11) | Author |
| D-18 | 2026-10-23 | Environment check includes a real probe run in the server's own environment | Owner's "GROMACS accessible in one environment but not another" problem | Author |
| D-19 | 2026-10-23 | Simulated jobs live only under `runs_simulated/` | Owner's mock-versus-real problem | Author |
| D-20 | 2026-10-23 | MCP tool surface fixed at 21 tools (`design.md` section 4.1), including `set_plan`, `write_report`, `get_job`, and `list_jobs` | Covers every functional requirement and keeps `job.json` and report writes in the server | Author |
| D-21 | 2026-10-23 | Rubric criteria renamed Q1 to Q8 and memory tiers L1 to L5 to avoid clashes with risk IDs and milestones | Reviewer finding | Author |
| D-22 | 2026-10-23 | Defaults: `SIMAGENT_*` variable names, `config.yaml` keys, gate thresholds (density bands 0.96 to 1.02 and 0.90 to 1.10 g/cm^3, temperature tolerances 2 K and 1 K), 50 ps analysis discard window, 1.0 nm cutoffs, environment derived from the install prefix (ADR-12) | Needed to build; heuristics for liquid water, labeled as such | Author |
| D-23 | 2026-10-23 | `-maxwarn` is added only with the exact count of warnings the researcher acknowledged one by one | Lets a researcher accept a known warning without any automatic suppression (RULE-S-03) | Author |
| D-24 | 2026-10-23 | Task numbers are stable labels, not an order | Avoids renumbering references across six documents | Author |
| D-25 | 2026-10-23 | Reference build uses exactly 1,000 molecules (the owner said approximately 1,000); default analyses RDF and MSD; Markdown report; non-goals NG1 to NG5 in the PRD | A concrete reference run is needed; the owner can change any of these | Author |
| D-26 | 2026-10-23 | `record_approval` needs a per-launch token held by the CLI; generic MCP clients cannot run jobs unless an operator opts out | Makes server-side approval hold against the model and other clients | Author |
| D-27 | 2026-10-23 | Simulated mode: the fake binary is allowed only with `SIMAGENT_ALLOW_FAKE=1`; gates skip physics and report `skipped_simulated` | Owner's mock-versus-real problem | Author |
| D-28 | 2026-10-23 | The shipped playbook is a package resource; memory stores learned entries and counters | The playbook must work before the memory store exists | Author |
| D-29 | 2026-10-23 | `build_summary` renders number tables and plots server-side | Report numbers come from trusted code | Author |
| D-30 | 2026-10-23 | Defaults not logged before: 0.5 ps trajectory output interval, 10 ns long-run limit, `max_wall_hours` 24, 15-minute checkpoints, a 20-step probe on a 1.5 nm box with 0.6 nm cutoffs, default seed derived from the job id (D-37) | Needed to build; all configurable except the probe | Author |
| D-31 | 2026-10-23 | Dependencies: `mcp` (official SDK), `openai` (OpenAI-compatible client), `pydantic` (typed tool inputs), `PyYAML` (config), `Jinja2` (MDP templates), `numpy` and `matplotlib` (analysis and plots), `filelock` (memory) | RULE-D-09 justification | Author |
| D-32 | 2026-10-23 | The model proposes and explains; deterministic controller code sequences work and calls every execution tool; the model's function list is a read-only allow-list | Closes approval-bypass paths and makes the state machine real | Author |
| D-33 | 2026-10-23 | Build files are bound into the plan by `build_sha256`; a rebuild after planning clears the approval | Prevents changing the system after approval | Author |
| D-34 | 2026-10-23 | Automatic retries are a structured, bounded list in the plan, enforced by the server | Retries must not drift into science changes | Author |
| D-35 | 2026-10-23 | Out of scope for version 1: extending a finished run, researcher-written `.mdp` text, and a looser-than-planned EM tolerance as an automatic retry (PRD NG6; FR-24 and AC-14 withdrawn) | Each opened approval-bypass paths that were costly to close; a longer run is a new job | Author |
| D-36 | 2026-10-23 | `write_report` tool: the server assembles `report.md` and writes `job.json.report_sha256`; prose numbers not found in trusted data are listed in a notice | Keeps `job.json` writes and report numbers in trusted code | Author |
| D-37 | 2026-10-23 | The default seed is derived from the job id | Reproducible, and different between jobs | Author |
| D-38 | 2026-10-23 | The Python environment is created and managed with `uv` (`uv venv`, `uv sync`, `uv.lock` committed, CI with `uv sync --frozen`); the venv and libraries were created on the authoring machine | Owner request after the first scoring rounds | Owner |
| D-39 | 2026-10-23 | Defaults not logged before: 10 s polling floor, 30 s sample spacing for ns/day, log excerpts capped at 20 lines (40 for `get_log_excerpt`), memory review after 180 days with a check every 7 days, at most 5 memory items per turn | Needed to build; all configurable in code constants | Author |
| D-40 | 2026-10-23 | `gmx.ntmpi: auto` means 1 rank below 10,000 atoms, so no automatic `ntmpi` retry exists; automatic retries are limited to a tighter `emtol`, a smaller `dt`, a larger `tau_p`, and more EM steps | A failed `mdrun` has no clean retry path for rank counts, and a looser setting changes the protocol | Author |
| D-41 | 2026-10-23 | The server derives the job outcome from stage statuses, and `get_job` returns the `summary` content | The controller cannot write `job.json`; the model needs a read path for past results | Author |
| D-42 | 2026-10-23 | The MCP SDK is pinned to the 2.x line (`mcp>=2,<3`); the server class is `MCPServer` (formerly `FastMCP`), verified in the installed package | The lock file resolved mcp 2.3.0, where `FastMCP` no longer exists | Author |
| D-43 | 2026-10-23 | `write_mdp(stage, reset=true)` restarts a failed or cancelled stage without a retry and keeps the approval; resume is allowed only for a died, timed-out, or cancelled stage | Closes the no-checkpoint dead end without opening a bypass | Author |
| D-44 | 2026-10-23 | The label "simulated" for mock jobs is kept (`execution_mode`, banner), with a glossary entry and the owner's own phrase quoted | Renaming it would touch most of the documents; the glossary removes the ambiguity | Author |
| D-45 | 2026-10-23 | Development tools and build setup: `pytest`, `pytest-cov`, `ruff`, `mypy`, `types-PyYAML` (dev group), `hatchling` as the build backend with a `src/` layout, and GitHub Actions for CI | RULE-D-09 justification | Author |
| D-46 | 2026-10-23 | A job is bound to the binary it was created with (`E_BINARY_CHANGED`, RULE-I-09) | A job must not continue under another build and stay labeled real | Author |
| D-47 | 2026-10-23 | Server-written inputs carry `internal` receipts, and `set_plan` takes and stores the completed request | Closes the receipt chain and the seed and request gap | Author |

## 6. Risks and open issues

Full risk table: `prd.md` section 12. Items needing action first:

| ID | Item | Next step | Status |
|---|---|---|---|
| R-01 / A-01 | `gpt6-luna` tool calling and endpoint unverified; the owner did not supply `LUNA_BASE_URL` | TASK-014; owner provides the endpoint when the code exists | Open |
| R-03 / A-02 | GROMACS data directory contents unverified | TASK-013 | Open |
| R-04 | TIP4P versus TIP4P/2005 intent | Ask the owner at first plan review | Open |
| OI-01 | Owner has not specified temperature and pressure | Defaults 300 K, 1 bar proposed (D-08); confirm | Open |
| OI-02 | Core count and expected wall time unknown | Measure in TASK-013 probe and TASK-063 | Open |
| OI-03 | GROMACS output strings and a few flags are written from documentation knowledge and tagged **[verify TASK-nnn]** in `design.md` | TASK-013, TASK-018, TASK-020, TASK-021, TASK-034, TASK-066 | Open |

## 7. Document quality scoring

### 7.1 Procedure

1. An independent reviewer (a fresh agent that did not write the documents) reads all six files and scores each against the rubric below, citing line-level evidence for each deduction.
2. Every document must score strictly above 95 (D-11). If any score is 95 or below, the defects are fixed in the documents and the whole set is scored again by a fresh reviewer. This repeats until all six exceed 95.
3. Scores and defect counts are recorded in section 7.3 exactly as the reviewer reports them. The author does not alter the reviewer's numbers.

### 7.2 Rubric (100 points per document)

| # | Criterion | Points | What earns full marks |
|---|---|---|---|
| Q1 | Completeness | 15 | Covers everything its role needs; nothing the owner asked for is missing |
| Q2 | Cross-document consistency | 15 | Names, IDs, numbers, paths, tool names, error codes, and states agree with the other five files |
| Q3 | Specificity and actionability | 15 | An engineer can build from it without asking questions; no vague wording |
| Q4 | Technical correctness | 15 | GROMACS commands, parameters, MCP and OpenAI-compatible usage, and physics are right or honestly flagged as assumptions |
| Q5 | Testability and traceability | 10 | Requirements map to tasks and tests; criteria are measurable |
| Q6 | Integrity and safety | 10 | Mock-versus-real, secrets, injection, science-decision ownership are covered |
| Q7 | Clarity and structure | 10 | Well organized, concise, consistent terminology, no filler |
| Q8 | Fidelity to the owner's request | 10 | Honors every stated instruction and does not present author choices as the owner's |

Deductions: a critical defect (wrong or contradictory in a way that would break the build or mislead) costs 5 points, a major defect 2 points, a minor defect 1 point, charged to the most relevant criterion; a criterion cannot go below zero.

### 7.3 Score history

Scores are out of 100 and are recorded verbatim from each reviewer's report. Defect counts (critical / major / minor) are the author's tally of that report; reviewers weigh severity differently from the nominal deductions in section 7.2, so the counts do not reproduce the scores arithmetically.

| Round | prd | architecture | design | memory | rules | tracker | All above 95? | Defects found (critical / major / minor) |
|---|---|---|---|---|---|---|---|---|
| 1 | 84 | 81 | 65 | 88 | 87 | 83 | No | prd 0/4/8, architecture 0/5/6, design 0/9/10, memory 0/0/7, rules 0/2/5, tracker 0/3/5 |
| 2 | 92 | 93 | 77 | 95 | 88 | 85 | No | prd 0/1/6, architecture 0/0/7, design 0/6/11, memory 0/0/5, rules 0/2/8, tracker 0/4/7 |
| 3 | 88 | 87 | 69 | 90 | 88 | 82 | No | prd 0/2/8, architecture 0/3/6, design 1/7/11, memory 0/2/5, rules 0/2/8, tracker 0/5/6 |
| 4 | 94 | 87 | 67 | 91 | 87 | 81 | No | prd 0/0/5, architecture 0/3/5, design 1/9/11, memory 0/0/7, rules 0/3/4, tracker 0/5/6 |
| 5 | 95 | 93 | 74 | 95 | 92 | 90 | No | prd 0/0/5, architecture 0/1/5, design 0/6/13, memory 0/0/5, rules 0/1/6, tracker 0/2/6 |
| 6 | 95 | 95 | 73 | 93 | 95 | 93 | No | prd 0/0/5, architecture 0/0/5, design 0/8/10, memory 0/1/5, rules 0/0/5, tracker 0/1/5 |
| 7 | 95 | 93 | 85 | 96 | 95 | 91 | No | prd 0/0/5, architecture 0/1/5, design 0/4/7, memory 0/0/4, rules 0/0/5, tracker 0/1/7 |
| 8 | 95 | 93 | 76 | 95 | 93 | 92 | No | prd 0/0/5, architecture 0/0/7, design 0/4/17, memory 0/0/5, rules 0/0/7, tracker 0/0/8 |
| 9 | pending | pending | pending | pending | pending | pending | pending | pending |

Round 1 note: the reviewer did not have the owner's pasted description of the motivating case, which explains its R8 findings on "observed" facts. The documents now cite the owner's description as the source (`prd.md` section 2, `memory.md` A.2). Round 1 defects were corrected in v1.1 (box-size unit factor, `mdrun` exit-code capture, server-side approval, probe run, summary tool, extra tools, test IDs for every requirement, corrected task plan). Whether those corrections were sufficient is decided by the next round, not asserted here. Round 2 defects were corrected in v1.2 (plan handoff through `set_plan` and the plan hash, retry handling, explicit server environment, probe `.mdp`, extension argv, warning acknowledgement, milestone and dependency alignment, rule and test alignment). Round 3 defects were corrected in v1.3 (approval-bypass closure through plan-only `.mdp` rendering and input-hash checks, approval token, simulated mode, resume protocol, job-state tools, `build_summary` rendering the tables, playbook bootstrap, dependency and evidence realignment). Round 4 defects were corrected in v1.4 (build binding through `build_sha256`, extension and resume transitions, structured retries, one execution-mode rule, the controller-versus-model split with an explicit model function list, agent runtime design in `design.md` section 14, server-rendered approval display, playbook rows for EM non-convergence, dependency fixes, and more logged decisions). Round 5 defects were corrected in v1.5 (an explicit `invalidate` operation with disjoint stage transitions, removal of run extension and researcher-written `.mdp` text from version 1, a `write_report` tool, a deterministic seed, a defined check order, wrapper cancellation and ledger locking, a workable simulated-mode build, and dependency cleanup). Round 6 defects were corrected in v1.6 (retries limited to keys that the plan bounds, `run.exit` written last, an analysis-stage reset, `E_MDRUN_RUNNING` guards on every invalidating call, a recorded `grompp` failure state, a fault-injection helper, `summary` returned by `get_job`, a tool-by-state matrix, and the uv environment setup). Round 7 defects were corrected in v1.7 (the `MCPServer` rename found in the installed package, a reset path for stages without a checkpoint, analysis-sequence retry, `ntmpi` and seed consistency, tighter resume rules, job-id and warning bookkeeping, and task dependency and evidence fixes). Round 8 defects were corrected in v1.8 (a completed request stored by `set_plan`, `internal` receipts for server-written files, binary continuity, a simulated-mode analysis path, unified ledger fields, failing-stage attribution, one owner for the analysis gate, capture-file and unit definitions, and more logged decisions).
