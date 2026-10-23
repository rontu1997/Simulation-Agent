# Product Requirements Document (PRD)

| Field | Value |
|---|---|
| Product | SimAgent: natural-language molecular simulation agent |
| Document | `prd.md` v1.8 |
| Date | 2026-10-23 |
| Status | Draft for approval |
| Related | `architecture.md` (how), `design.md` (details), `rules.md` (guardrails), `memory.md` (context and agent memory), `tracker.md` (plan and status) |

---

## 1. Summary

SimAgent lets a researcher describe a molecular dynamics (MD) task in plain language. The agent interprets the request, asks about any missing scientific information, prepares a validated GROMACS workflow, runs it through Model Context Protocol (MCP) tools, monitors execution, diagnoses failures, and analyzes the resulting data. It reduces the software-specific burden of running GROMACS. It does not replace the researcher's scientific judgment, and it never reports a result it cannot prove came from a real GROMACS run.

Version 1 targets one reference workflow: a reproducible MD simulation of about 1,000 TIP4P water molecules (preparation, energy minimization, NVT equilibration, NPT equilibration, 1 ns production, validation, analysis).

## 2. Problem and motivation

The motivating case, as the project owner described it on 2026-10-23, is a September 2026 workflow: prepare and run a reproducible 1,000-molecule TIP4P water simulation in GROMACS. In the owner's account, executing GROMACS was not the only hard part; the difficulty was everything around it. Items 1 to 5 and the table of problems below restate the owner's description; parenthetical examples and the wording of the table's requirement column are the author's elaboration:

1. Understanding the physical system and choosing appropriate parameters (cutoffs, thermostat, barostat, time step, constraints, run lengths).
2. Producing mutually consistent inputs (`.gro`, `.top`, `.mdp`) across dependent stages.
3. Running stages in order, with each stage consuming the previous stage's outputs.
4. Reading `grompp` and `mdrun` errors and deciding what to change.
5. Proving a run actually finished and produced physically sensible output (the owner wrote "verify that the simulation had actually completed successfully"; "physically sensible" is the author's addition).

Problems from that work, each of which becomes a requirement here (rows 1 to 3 are the owner's named problems; row 4 restates the owner's difficulty of interpreting errors):

| Observed problem | Requirement it drives |
|---|---|
| GROMACS accessible in one execution environment but not another | FR-17, FR-18, FR-23 (explicit binary path, constructed process environment, startup check with a real probe run in the server's own environment) |
| Hard to distinguish genuine simulated results from mock execution (owner's words: "distinguishing simulated results from mock execution") | FR-11 (real-run proof), RULE-I series |
| Coordinating dependent computational tasks by hand | FR-07, FR-15 (staged, resumable workflow) |
| Needing GROMACS-specific expertise to interpret errors | FR-10 (diagnosis and bounded remediation) |

## 3. Users

| Persona | Description | Needs |
|---|---|---|
| P1: Domain researcher (primary) | Chemist or physicist who knows the science but not every GROMACS flag. | Plain-language control, explained parameter choices, trustworthy results. |
| P2: Computational scientist | Knows GROMACS well; wants speed and reproducibility. | Full visibility of generated inputs and commands, ability to override anything. |
| P3: Reviewer or collaborator | Needs to audit what was run. | Provenance record, exact parameters, evidence of completion. |

## 4. Goals, non-goals, anti-goals

### 4.1 Goals

- G1: Go from a plain-language request to a completed, validated 1,000-molecule TIP4P water run with the researcher making only scientific decisions.
- G2: Make every run reproducible and auditable from a single job bundle.
- G3: Make "did it really run?" answerable from machine-checked evidence, not from the agent's say-so.
- G4: Turn raw GROMACS output into a clear analysis report that states what was and was not checked.
- G5: Keep GROMACS invocation behind typed MCP tools so any MCP-capable client could reuse them.

### 4.2 Non-goals (v1)

- NG1: Systems other than pure water (proteins, membranes, ligands, free-energy calculations). The design must not block them later.
- NG2: Multi-node or cluster scheduling (Slurm). Agent and GROMACS share one Linux machine.
- NG3: Building a GUI. v1 is a terminal chat interface.
- NG4: Acting as the authority on force-field choice. The agent proposes and explains; the researcher decides.
- NG5: Replacing GROMACS expertise for novel or unusual methods.
- NG6: Extending a finished run and researcher-written `.mdp` files. A longer run is a new job, and parameters change by editing the plan, which requires re-approval (decision D-35).

### 4.3 Anti-goals (a failure even if it technically works)

- AG1: Presenting mock, cached, or fabricated output as a real simulation result.
- AG2: Silently choosing scientific parameters (water model, temperature, pressure, length) the researcher did not approve.
- AG3: Suppressing GROMACS warnings (for example raising `-maxwarn`) without the researcher acknowledging each warning.
- AG4: Declaring success because a process exited with code 0 without checking outputs and physics.
- AG5: Leaking the API key into logs, reports, memory files, or prompts.

## 5. Scope of version 1

### 5.1 Reference workflow

| Stage | ID | Default | Purpose |
|---|---|---|---|
| System build | S1 | Cubic box, exactly 1,000 TIP4P waters, OPLS-AA topology | Prepare starting coordinates and topology |
| Energy minimization | S2 | Steepest descent, convergence at Fmax < 1000 kJ/mol/nm | Remove bad contacts |
| NVT equilibration | S3 | 100 ps, 300 K, V-rescale, 2 fs | Equilibrate temperature |
| NPT equilibration | S4 | 100 ps, 300 K, 1 bar, C-rescale | Equilibrate density |
| Production | S5 | 1 ns, 300 K, 1 bar, C-rescale | Collect trajectory |
| Analysis | S6 | T, P, density, energies, O-O RDF, MSD and diffusion | Scientific output |
| Report | S7 | Markdown with plots and checks | Communicate and archive |

All defaults are overridable by the researcher. Values are defaults proposed by the agent, not facts the researcher chose, and the agent says so when it uses them. The owner said "approximately 1,000" molecules; the reference run uses exactly 1,000 (decision D-25).

### 5.2 Example requests the agent must handle

- "Run a 1 ns simulation of 1000 TIP4P water molecules at 300 K and 1 bar and give me the density and the O-O RDF."
- "Same as before but at 280 K." (`get_job` or `list_jobs` finds the earlier job; the agent builds a new plan with the changed parameter)
- "Why did the NPT step fail?" (diagnose from logs)
- "Resume the run that was interrupted." (continue from the checkpoint, FR-15)

## 6. User stories

| ID | Story | Priority |
|---|---|---|
| US-01 | As P1, I describe my simulation in a sentence and get a proposed plan with the reason for each parameter. | Must |
| US-02 | As P1, I am asked about science-critical details I left out, and not bothered with technical ones. | Must |
| US-03 | As P1, I approve the plan before anything expensive runs. | Must |
| US-04 | As P1, I see live progress (stage, step, ns/day, ETA). | Should |
| US-05 | As P1, when a stage fails I get a plain-language cause and a proposed fix, not a raw log. | Must |
| US-06 | As P1, I get a report whose numbers I can trace to files. | Must |
| US-07 | As P2, I can read every generated `.mdp` and the exact commands used, and change any plan parameter by editing the plan, which requires re-approval. | Must |
| US-08 | As P3, I can verify from the provenance record that GROMACS really executed. | Must |
| US-09 | As P1, I can resume after an interruption without redoing finished stages. | Should |
| US-10 | As P2, I can call the GROMACS tools from another MCP client. | Could |

## 7. Functional requirements

| ID | Requirement | Priority | Story |
|---|---|---|---|
| FR-01 | Parse a natural-language request into a structured `SimulationRequest` (schema in `design.md` section 3). | Must | US-01 |
| FR-02 | Detect missing science-critical fields (molecule count, water model, force field, temperature, pressure, production length, and analyses when none are named) and ask, at most 3 questions per round, never about a field already stated. Technical fields (cutoffs, seeds, output frequency, equilibration lengths) receive disclosed defaults. | Must | US-02 |
| FR-03 | Generate a workflow plan listing the 7 ordered stages S1 to S7 (build, EM, NVT, NPT, production, analysis, report), their parameters, and a one-line justification for each non-default choice. | Must | US-01 |
| FR-04 | Require explicit researcher approval of the plan before any `mdrun` starts (configurable, default on). The MCP server enforces this, so that neither the language model nor an MCP client without the CLI's approval token can start `mdrun` (mechanism and the explicit operator opt-out `SIMAGENT_ALLOW_UNAPPROVED`: `design.md` section 4.3). | Must | US-03 |
| FR-05 | Build a cubic box containing exactly the requested number of water molecules with a consistent `topol.top`. | Must | US-01 |
| FR-06 | Render `.mdp` files from versioned templates; reject parameters outside documented ranges. | Must | US-07 |
| FR-07 | Run stages in dependency order; a stage may only start when its predecessor passed its gate. | Must | US-01 |
| FR-08 | Report progress for running jobs: step and time read from the `.log`; ns/day and ETA computed from step/time deltas between polls. | Should | US-04 |
| FR-09 | Apply stage gates (exit status, log markers, output files, physical checks) after every stage. | Must | US-06 |
| FR-10 | Diagnose failures using a pattern playbook; propose a fix; allow at most 2 automatic retries per stage, only with changes that do not alter the science and are disclosed. | Must | US-05 |
| FR-11 | Record a provenance entry for every `gmx` invocation (argv, exit code, timestamps, output hashes, GROMACS version string) and refuse to label a job "real" without them. | Must | US-08 |
| FR-12 | Analyze production data: temperature, pressure, density, potential and total energy with block-averaged errors (always); O-O RDF and MSD with diffusion coefficient (proposed by default, run when named or accepted); discard an equilibration window (default 50 ps, disclosed). | Must | US-06 |
| FR-13 | Generate a Markdown report with parameters, gate results, plots, numbers with uncertainties, and a limitations section. | Must | US-06 |
| FR-14 | Save a job bundle (`job.json`, inputs, provenance, analysis) sufficient to reproduce the protocol. | Must | US-08 |
| FR-15 | Resume from the last passed stage or from an `mdrun` checkpoint (`.cpt`) after interruption. | Should | US-09 |
| FR-16 | Cancel a running job cleanly and mark its state. | Should | US-04 |
| FR-17 | Read the GROMACS path from `GMX_BIN` (default `/home/rontu/Rontu/software/software_install/gromacs-2025.3/bin/gmx`); never hard-code it in prompts or code paths other than config defaults. | Must | US-08 |
| FR-18 | On startup, verify the binary exists, is a real executable (not a script impostor), reports the expected version, and that `oplsaa.ff/forcefield.itp`, `oplsaa.ff/tip4p.itp`, and `tip4p.gro` are found; fail with an actionable message otherwise. | Must | US-08 |
| FR-19 | Read LLM settings from environment: `LUNA_API_KEY`, `LUNA_BASE_URL`, `LUNA_MODEL` (default `gpt6-luna`) using an OpenAI-compatible client. | Must | US-01 |
| FR-20 | Persist researcher preferences and a validated error-fix playbook as agent memory (`memory.md` section B). | Should | US-02 |
| FR-21 | Log every LLM call and tool call (metadata and hashes, redacted) for audit. | Should | US-08 |
| FR-22 | Expose all GROMACS operations as MCP tools usable by any MCP client over stdio (starting `mdrun` needs the approval token or the operator opt-out, FR-04). Exposing the tools through MCP is Must; use by clients other than SimAgent (US-10) is Could. | Must | US-10 |
| FR-23 | During the environment check, run a real micro-simulation (solvate, `grompp`, a 20-step `mdrun`, `gmx check`) in the MCP server's own process environment, so "GROMACS works here" is proven rather than assumed. | Must | US-08 |
| FR-24 | Withdrawn: extending a finished run is out of scope for version 1 (NG6). | n/a | n/a |
| FR-25 | List jobs and read a job's request, plan, stage statuses, and approval state through MCP tools, so a restarted agent or another client can reattach without reading files directly. | Should | US-09 |
| FR-26 | Let the researcher acknowledge individual `grompp` warnings; only acknowledged warnings may be passed on, and `grompp -maxwarn` is set to exactly their count (AG3, RULE-S-03). | Must | US-05 |

## 8. Non-functional requirements

| ID | Requirement | Measure |
|---|---|---|
| NFR-01 | Security: no shell-string execution, no path escape from the runs directory, secrets never written to disk. | Static checks (`ruff` rules S602 and S605, TASK-010) plus tests T-SEC-01 to T-SEC-04 |
| NFR-02 | Reproducibility: same request, seed, hardware, and GROMACS build gives the same plan hash, rendered `.mdp` and `topol.top` files, and `water.gro` byte for byte (the `water.gro` part assumes `gmx solvate` is deterministic; TASK-021 verifies it); trajectories can differ on other hardware, and the report states this. | T-NFR-02 (hash comparison of inputs) |
| NFR-03 | Resilience: an agent crash must not kill a running `mdrun`; the agent can reattach through the wrapper's pid, exit, and log files. | T-INT-03 |
| NFR-04 | Observability: every job has a structured event log. | T-NFR-04 (`events.jsonl` schema validation) |
| NFR-05 | Performance: tool-side overhead (time in server code excluding the `gmx` subprocess and excluding LLM latency, as `tool_time_s`) below 5% of stage wall time or 10 s per stage, whichever is larger; status polling at most once per 10 s. | T-NFR-05 in the acceptance run; T-AGENT-07 for the polling interval |
| NFR-06 | Portability: Linux (x86_64 assumed, A-05), Python 3.11 or newer; lint, type check, and unit tests run in CI (TASK-017). | CI pipeline result |
| NFR-07 | Testability: core logic at or above 85% line coverage; integration tests use the real `gmx`; fake-`gmx` tests are labeled and cannot produce a "real" job. | Coverage report, T-INT-04 |
| NFR-08 | Provider portability: swapping `LUNA_BASE_URL` and `LUNA_MODEL` needs no code change. | T-NFR-08 |
| NFR-09 | Cost control: per-job token budget configurable (default 200,000 tokens) with a hard stop. | T-NFR-09 |
| NFR-10 | Resource limits: a production length above 10 ns needs explicit confirmation, and any `mdrun` is stopped after `max_wall_hours` (default 24) with the stage marked failed. | T-UNIT-07, T-UNIT-08, T-INT-06 |

## 9. Acceptance criteria

| ID | Criterion | Verified by |
|---|---|---|
| AC-01 | Given a request that states molecule count, water model, temperature, pressure, length, and analyses, the agent asks at most 1 question (force field, if unstated) and none about stated fields; given a request missing temperature, pressure, and force field, it asks exactly those 3. Both present a plan with all 7 stages S1 to S7. | T-AGENT-01 |
| AC-02 | The built system contains exactly 1,000 `SOL` molecules; `atoms_in_gro == 4 * SOL_count` (O, H1, H2, virtual site) and the `[ molecules ]` count in `topol.top` equals `SOL_count`. | T-UNIT-02, T-INT-07 |
| AC-03 | EM, NVT, NPT, and production complete on the real binary; every stage has provenance start and end records with exit code 0. | T-INT-01 |
| AC-04 | Production `.xtc` spans 1.000 ns (500,000 steps at 2 fs; 2,001 frames at 0.5 ps output) and the `.log` contains the GROMACS completion marker. | T-INT-01 (G-PROD gate) |
| AC-05 | Mean density in the production window (after the discard window) lies between 0.96 and 1.02 g/cm^3 at 300 K, otherwise the report flags the deviation. | T-INT-01 (G-PROD gate and `summary.json`) |
| AC-06 | With `GMX_BIN` pointed at the fake binary and `SIMAGENT_ALLOW_FAKE=1`, the job is labeled SIMULATED, stored under `runs_simulated/`, the report carries a banner, and the agent never calls it a real result. Without the flag the environment check refuses the binary. | T-INT-04 |
| AC-07 | Corrupting a stage input (for example an unknown atom type, injected through the fault-injection helper of `design.md` section 4.3 item 6) leads to a diagnosed failure with a plain-language cause and no `-maxwarn` unless the researcher acknowledged that exact warning. | T-INT-02, T-UNIT-12 |
| AC-08 | Killing the agent and the server during production leaves `mdrun` running, and a restarted agent reattaches (`get_job`, `get_run_status`) and finishes the running stage and its gate; from M5 it also finishes analysis and report. | T-INT-03 |
| AC-09 | A search of the entire job bundle, logs, and memory for the value of `LUNA_API_KEY` finds nothing. | T-SEC-03 |
| AC-10 | The report includes numbers with uncertainties, a file path for each number, and a limitations section, and every number in its tables equals the value in `summary.json`; numbers in the prose that are not in the trusted data are flagged in a notice. | T-REP-01 |
| AC-11 | `run_mdrun` called without a recorded approval for the current plan returns `E_NOT_APPROVED` and starts nothing. | T-UNIT-07, T-AGENT-03 |
| AC-12 | The environment check passes against the real binary (including the probe run) and returns `E_GMX_NOT_FOUND` for a missing path, `E_GMX_VERSION` for a wrong version, `E_FAKE_BINARY` for a minimal test script, and `E_GMX_NOT_ELF` for a non-ELF non-script file. | T-INT-05 |
| AC-13 | A job's stored plan and request reproduce the same rendered `.mdp` files, `topol.top`, and plan hash when re-submitted to a new job with the same seed. | T-NFR-02 |
| AC-14 | Withdrawn together with FR-24. | n/a |
| AC-15 | After the agent and the server restart, `get_job` and `list_jobs` return the plan, stage statuses, and approval state. | T-INT-10 |
| AC-16 | After SIGKILL of the `mdrun` wrapper and `gmx` during production, `run_mdrun(resume=true)` continues from the checkpoint, and the trajectory is continuous and ends at 1,000 ps. | T-INT-09 |

Requirements without their own acceptance criterion (FR-08, FR-16, FR-20, FR-21, FR-22) are verified by the tests listed in `tracker.md` section 4.1.

## 10. Success metrics

| Metric | Target |
|---|---|
| Request-to-validated-result success on the reference workflow | At least 9 of 10 consecutive runs without manual file editing |
| False "success" claims (mock or failed run reported as good) | 0 |
| Clarification questions in the first round | At most 3 |
| Median tool-side overhead (NFR-05 definition) | Under 5% of stage wall time or 10 s per stage, whichever is larger |
| Failures diagnosed correctly from the playbook | At least 80% of injected faults |

These metrics are post-v1 measurements collected by TASK-067; they are not release gates for M0 to M6 except where an acceptance criterion says so.

## 11. Constraints and assumptions

| ID | Item | Type |
|---|---|---|
| C-01 | GROMACS 2025.3 at `/home/rontu/Rontu/software/software_install/gromacs-2025.3/bin/gmx` | Constraint |
| C-02 | Agent and GROMACS run on the same Linux machine | Constraint (user stated) |
| C-03 | The LLM is `gpt6-luna`, reached through an OpenAI-compatible endpoint with the key in an environment variable | Constraint (user stated) |
| C-04 | Tool access to GROMACS is through MCP | Constraint (user stated) |
| C-05 | The Python environment is created and managed with `uv` (`uv venv`, `uv sync`), with libraries installed from `pyproject.toml` (`design.md` section 15) | Constraint (user stated) |
| A-01 | `gpt6-luna` supports function (tool) calling in the OpenAI-compatible API. If not, fall back to JSON-schema-constrained replies parsed by the agent. | Assumption, to be verified in TASK-014 |
| A-02 | GROMACS 2025.3 was built with the `oplsaa.ff` force-field directory and `tip4p.gro` available. | Assumption, verified by FR-18 |
| A-03 | The machine runs 1 ns of 4,000-atom water in minutes to a few hours rather than days; the actual wall time is recorded by the first acceptance run (OI-02). | Assumption |
| A-04 | Windows is the authoring machine for these documents; code runs on Linux. | Context |
| A-05 | The owner said only "Linux"; the author assumes x86_64 and Python 3.11 or newer are available on the GROMACS host. | Assumption |

## 12. Risks

| ID | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R-01 | `gpt6-luna` lacks tool calling or behaves differently from the assumed API | Medium | High | A-01 smoke test early; JSON fallback mode; provider-agnostic client |
| R-02 | LLM invents a parameter or claims a result without evidence | Medium | High | Server-side range checks; gates in code not in prompt; report numbers come from tools only |
| R-03 | GROMACS binary or data directory not found in the MCP server's environment | Medium | High | FR-18 startup check; environment captured in provenance |
| R-04 | TIP4P (original) differs from TIP4P/2005 researchers often intend | Medium | Medium | Water model is a science-critical question; both options explained |
| R-05 | Finite-size effects bias diffusion for about 1,000 molecules | High | Medium | Report notes the Yeh-Hummer correction and flags raw D as uncorrected |
| R-06 | Long runs outlive an agent session | Medium | Medium | Detached `mdrun`, pid file, resume logic (NFR-03, FR-15) |
| R-07 | Token cost grows with long logs | Medium | Low | Logs summarized by tools; never pasted in full into prompts |
| R-08 | Gate thresholds are heuristics and may fail good runs or pass bad ones | Medium | Medium | Thresholds live in config, reports label them heuristic, density bands apply only near ambient conditions, and TASK-063 calibrates them against real runs |

## 13. Release plan

| Milestone | Outcome | Exit criteria |
|---|---|---|
| M0 Documents | Six planning documents scoring above 95 | Scores in `tracker.md` section 7 |
| M1 Foundation | Config, provenance ledger, `gmx` runner and environment check with probe run, LLM client (live smoke test once the owner supplies the endpoint and key), MCP client, CI | AC-12 |
| M2 MCP server | Tools 1 to 12 and 19 to 21 of `design.md` section 4.1 (job, plan, approval, build, `grompp`, `mdrun`, status, cancel, gates, log excerpt, artifacts, job state) working against the real `gmx`, server-side approval, the analysis library used by gates, and the fake binary | AC-02, AC-11 (server part, T-UNIT-07), AC-15 |
| M3 Workflow engine | Staged execution, resume, diagnosis, verification of the `design.md` items tagged TASK-034 (gates, logs, probe, playbook) | AC-16 |
| M4 Agent | LLM client, MCP client, controller, clarification, planning, approval flow | AC-01, AC-08, AC-11 (agent part, T-AGENT-03) |
| M5 Analysis and report | Analysis tools 13 to 17 (including `build_summary`), `write_report` (tool 18), report with the SIMULATED banner | AC-06, AC-10 |
| M6 Hardening and acceptance | Fault injection, secret scan, NFR tests, full acceptance run on the real binary | AC-03, AC-04, AC-05, AC-07, AC-09, AC-13 |

Detailed tasks and status: `tracker.md`.

## 14. Glossary

| Term | Meaning |
|---|---|
| MD | Molecular dynamics |
| MCP | Model Context Protocol, a standard for exposing tools to an LLM client |
| TIP4P | Four-site rigid water model: the oxygen carries the Lennard-Jones site and no charge, and the negative charge sits on a massless virtual site (M) on the H-O-H bisector |
| EM | Energy minimization |
| NVT / NPT | Constant particle number, volume, temperature / constant particle number, pressure, temperature |
| Gate | A machine-checked pass or fail test run after a stage |
| Provenance | Record of exactly which commands, inputs, and versions produced a file |
| Job bundle | The directory holding everything needed to audit or reproduce a run |
| Mock job | A job run with the fake binary. Its `execution_mode` is `simulated` and its report carries a SIMULATED banner; the name has nothing to do with simulated time in MD |
