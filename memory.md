# Memory

| Field | Value |
|---|---|
| Document | `memory.md` v1.8 |
| Date | 2026-10-23 |
| Purpose | Part A: durable project context for anyone (person or coding assistant) who joins the project. Part B: design of the memory the running agent keeps. |
| Related | `rules.md` section 6 (RULE-M), `architecture.md` CMP-09, `prd.md` FR-20 |

---

# Part A. Project context memory

Facts that are not derivable from the code, and that a new contributor must not have to rediscover. Each item has a source and a date. If an item is wrong, correct it here and record the change in `tracker.md` section 5.

## A.1 Stated by the project owner

| ID | Fact | Source | Date |
|---|---|---|---|
| MEM-A-01 | The product is an AI agent: a researcher describes an MD task in natural language; the agent interprets it, prepares a workflow, runs GROMACS through MCP tools, and analyzes the data. | Owner request | 2026-10-23 |
| MEM-A-02 | Required planning documents: `prd.md`, `architecture.md`, `design.md`, `memory.md`, `rules.md`, `tracker.md`. Each must score above 95 on the rubric in `tracker.md` section 7; documents scoring 95 or below are corrected and scored again until all exceed 95. The owner wrote "architeture.md"; this project uses the spelling `architecture.md` (assumed typo, decision D-06). | Owner request | 2026-10-23 |
| MEM-A-03 | GROMACS 2025.3 executable: `/home/rontu/Rontu/software/software_install/gromacs-2025.3/bin/gmx`. | Owner request | 2026-10-23 |
| MEM-A-04 | The LLM is called `gpt6-luna`. It is reached through an OpenAI-compatible endpoint. Credentials come from an environment variable. The key itself is never stored in this repository or in these documents. | Owner request | 2026-10-23 |
| MEM-A-05 | The agent and GROMACS run on the same Linux machine. | Owner reply | 2026-10-23 |
| MEM-A-06 | The owner chose to skip further questions and have the documents written with stated defaults. All defaults chosen by the author are recorded in `tracker.md` section 5 as the author's decisions, not the owner's. | Owner reply | 2026-10-23 |
| MEM-A-07 | The authoring machine is Windows (`D:\Rontu\Python\AI-Agent\Simulation-agent`); the code will run on Linux (the GROMACS path is a Linux path). How the documents reach the Linux machine is not specified; assumed to be a manual copy by the owner. | Environment (working directory and path), author assumption (transfer) | 2026-10-23 |
| MEM-A-16 | The Python environment is created and managed with `uv` (`uv venv`, `uv sync`), with libraries installed from `pyproject.toml`. Done on the authoring machine (uv 0.11.32, CPython 3.12.6); the Linux host repeats it (`design.md` section 15). | Owner request | 2026-10-23 |

## A.2 Motivating case (September 2026 workflow)

Source: the owner's description pasted on 2026-10-23. Everything in this section restates it, except the final "lesson" line, which is the author's reading.

A reproducible MD of about 1,000 TIP4P water molecules in GROMACS: system preparation, energy minimization, NVT equilibration, NPT equilibration, 1 ns production, validation, analysis. Observed difficulties:

- GROMACS accessibility differed across execution environments.
- It was hard to distinguish genuine simulated results from mock execution (the owner's words: "distinguishing simulated results from mock execution").
- Dependent computational tasks needed manual coordination.
- Interpreting errors and verifying that the simulation had actually completed successfully were part of the difficulty.

Author's reading of the lesson (not the owner's words): the dangerous failure is believing a result that was not produced by a real run. Evidence beats assurance (RULE-I series).

## A.3 Decisions and defaults made by the author (not by the owner)

Listed so the owner can accept or reject them. This table shows only the decisions that shape how the other documents read; the complete list of author decisions is the decision log, `tracker.md` section 5 (D-01 to D-44). Matching entries (MEM-A-08 is D-07, MEM-A-09 is D-08, MEM-A-10 is D-12, MEM-A-11 is D-09, MEM-A-12 is D-13).

| ID | Decision |
|---|---|
| MEM-A-08 | Python 3.11 and the official MCP Python SDK over stdio. |
| MEM-A-09 | Reference system: OPLS-AA with `tip4p.itp`, cubic box, 1,000 molecules, about 3.107 nm edge, 300 K, 1 bar. These are proposals; temperature and pressure were not specified by the owner. |
| MEM-A-10 | V-rescale thermostat, C-rescale barostat, 2 fs time step with rigid water, 1.0 nm cutoffs, PME. |
| MEM-A-11 | Gates and provenance are enforced in server code, never by prompt wording. |
| MEM-A-12 | Environment variable names: `LUNA_API_KEY`, `LUNA_BASE_URL`, `LUNA_MODEL`, `GMX_BIN`, `GMX_RUNS_DIR`. |

## A.4 Open assumptions

| ID | Assumption | How it gets resolved |
|---|---|---|
| MEM-A-13 | `gpt6-luna` supports tool calling over the OpenAI-compatible API (PRD A-01). The model name and endpoint could not be verified from this environment. | `TASK-014`, test T-LLM-01 |
| MEM-A-14 | GROMACS data directory includes `oplsaa.ff/tip4p.itp` and `tip4p.gro`. | `gmx_check_environment` (FR-18) |
| MEM-A-15 | Hardware is sufficient for 1 ns of a 4,000-atom system in hours. | First acceptance run records wall time |

## A.5 Conventions for contributors

- Documents are the contract (RULE-D-07). Change a document before or with the code that departs from it.
- Update `tracker.md` in the same change as the work, with evidence (test name, output path).
- Use units ps, nm, K, bar, kJ/mol in code and prose.
- Names: tools in `snake_case`, error codes `E_UPPER_SNAKE`, requirement IDs `FR-nn`, tasks `TASK-nnn`, rules `RULE-X-nn`.

---

# Part B. Agent runtime memory design

## B.1 Goals and limits

The agent remembers what makes it more useful across sessions: researcher preferences, fixes that worked, and an index of past jobs. It does not remember facts about results in place of reading files, and memory never replaces a gate or a tool result (RULE-M-02).

## B.2 Memory tiers

| Tier | Scope | Lifetime | Storage | Contents |
|---|---|---|---|---|
| L1 Working | One conversation | Session | In process | Dialogue, current `SimulationRequest` draft, tool results summaries, token count |
| L2 Job | One job | Permanent with the job | `runs/<job_id>/job.json`, `events.jsonl`, `provenance.jsonl` | Request, plan, state, retries, environment identity |
| L3 Researcher | One researcher | Persistent | `memory/preferences.json` | Preferred defaults and style choices |
| L4 Playbook | Whole installation | Persistent | `memory/playbook.json` (learned entries and counters) plus the shipped package resource `agent/playbook_default.json` | Error pattern, cause, fix, outcome counts |
| L5 Index | Whole installation | Persistent | `memory/jobs_index.json` | Job id, one-line description, date, status, key parameters, path |

No vector database in version 1. Retrieval is by exact key and simple filters (decision ADR-06). Every L3 and L4 entry carries `source`, `last_used`, and `confidence`; every L5 entry carries `source` and `created` (RULE-M-05).

## B.3 Schemas

### L3 preferences entry

```json
{
  "key": "temperature_K",
  "value": 300.0,
  "source": "user_stated",
  "session": "2026-10-23T10:12:00Z",
  "times_confirmed": 2,
  "last_used": "2026-10-23",
  "confidence": "high",
  "scope": "default_proposal"
}
```

Allowed keys: `temperature_K`, `pressure_bar`, `water_model`, `force_field`, `prod_ns`, `analyses`, `report_detail`, `ntomp`. Anything else is rejected. Field meanings: `source` is `user_stated` or `confirmed_default`; `confidence` is `high` (confirmed in 2 or more sessions), `medium` (once), or `low` (accepted without being stated, source `confirmed_default`); `scope` is `default_proposal` (offered as the proposed answer, still tagged `origin: memory` and confirmed per RULE-M-04) or `style` (output preference only); `report_detail` is `brief` or `full`; `ntomp` is a researcher-stated hardware preference that only proposes a value for the request's `hardware.ntomp`; the stored preference becomes the request's `hardware.ntomp` only when the researcher accepts it, and the resolution order is then the request, `GMX_NTOMP` from the environment, `config.yaml`.

File structure of `memory/preferences.json`: `{"version": 1, "last_review": "<timestamp>", "entries_by_researcher": {"<SIMAGENT_USER or OS user name>": [ ...entries as above... ]}}`. One file serves all researcher ids on the host.

### L4 playbook entry

```json
{
  "pattern": "Water molecule starting at atom \\d+ can not be settled",
  "cause": "overlaps after build or dt too large",
  "fix": [{"emtol": 100.0}, {"dt": 0.001}],
  "auto_allowed": "yes",
  "seen": 5, "fixed": 4, "failed": 1,
  "last_used": "2026-10-23",
  "confidence": "high",
  "source": "shipped"
}
```

`pattern` is a regular expression, as in the shipped table. `fix` is an ordered list: step 1 is tried first and step 2 on the next failure of the same stage. `auto_allowed` is `yes`, `no`, or `ask` (the "Ask" rows of the shipped playbook). `source` is `shipped` or `learned`. Shipped entries are not copied into the memory file: they are loaded at start from the package resource `agent/playbook_default.json` (versioned, content from `design.md` section 7.1), so the playbook works before the memory store exists. `memory/playbook.json` holds only learned entries and the counters for every pattern, and is created on first write. `confidence` is `high` when `seen >= 3` and `fixed/seen >= 0.8`, `low` when `seen >= 3` and `fixed/seen < 0.4`, and `medium` otherwise (including every entry not yet seen, shipped or learned). A learned entry is created with `auto_allowed: no`; it becomes `yes` only when, after a later successful application, the researcher answers yes to "allow this fix automatically next time?"; the approved plan's `automatic_retries_allowed` still bounds every automatic application. The `fix` keys of a learned entry are validated against the RULE-S-04 whitelist (`emtol` tightened, `dt`, `tau_p`, `em_nsteps`); a pattern derived from log text can therefore never introduce a science-changing or arbitrary action (RULE-M-06). Learning flow: when an escalated failure is resolved by a researcher-approved change on the whitelist, the agent proposes a playbook entry whose regex is built from the first matching error line: split the line on runs of digits, `re.escape` each piece, join the pieces with `\d+`, then reject the pattern if it is longer than 200 characters, fails to compile, or does not match its own source line within 100 ms; the researcher approves or rejects it; an approved entry is stored with `source: learned` and `auto_allowed: no`. `memory/playbook.json` has the structure `{"version": 1, "learned": [ ...entries... ], "counters": {"<pattern>": {"seen": n, "fixed": n, "failed": n, "last_used": "<date>"}}, "disabled_shipped": ["<pattern>", ...]}`, and the diagnostician skips every shipped pattern listed in `disabled_shipped`.

### L5 index entry

```json
{"job_id": "water-1000-tip4p-300K-20261023-1", "created": "2026-10-23",
 "title": "1000 TIP4P water, 300 K, 1 bar, 1 ns", "status": "passed", "key_params": {"n_molecules": 1000, "water_model": "tip4p", "temperature_K": 300.0, "pressure_bar": 1.0, "prod_ns": 1.0},
 "execution_mode": "real", "source": "job_end", "path": "runs/water-1000-tip4p-300K-20261023-1"}
```

`status` is the job outcome derived by the server: `planned`, `running`, `passed` (every stage `PASSED`), `failed`, or `cancelled`; whether the job was real or simulated is carried only by `execution_mode` (vocabularies: `design.md` section 3.4, job outcome row). `list_jobs` is authoritative for jobs; L5 is a lookup cache that stores only the summary text and key parameters and is rebuilt from `list_jobs` and `get_job` when it is missing or stale.

## B.4 Lifecycle

| Event | Memory action |
|---|---|
| Session start | Run the environment check first, then load L3, L4, L5 if the store exists (a missing store is not an error); show nothing from memory unless relevant |
| Researcher states a science-critical value | Held in L1 with origin `user`; saved to L3 only after the plan is approved |
| Plan built | Values drawn from L3 are tagged `origin: memory` and must be confirmed (RULE-M-04) |
| Stage failure diagnosed and fixed | Increment L4 `seen`; after the retry outcome is known, increment `fixed` or `failed` |
| Job end | Write L5 entry; update L3 `times_confirmed` for values used unchanged |
| Researcher says "forget X" | Delete the matching L3 or learned L4 entry and acknowledge; for a shipped L4 entry add it to `disabled_shipped` in `memory/playbook.json` instead |
| Session start, when the last review is older than 7 days (timestamp kept in `preferences.json`) | List entries unused for 180 days for review (RULE-M-05) |

## B.5 Retrieval rules

1. Lookup by key. For failures, match the playbook by pattern text against the log excerpt.
2. Memory proposes; the current conversation decides. A current explicit instruction always wins (RULE-M-03).
3. Cross-job questions ("what density did I get at 280 K?") go to L5 to find the job, then read that job's `summary` field from `get_job`; the answer cites the job and its `summary.json` path, not the index.
4. At most 5 memory items are injected into the prompt per turn, each with its source and date.

## B.6 Privacy and safety

- Never store: API keys, host credentials, full logs, trajectories, or absolute paths outside the project (RULE-M-01).
- Memory files are plain JSON and human-readable; the researcher can edit or delete them.
- Writes are atomic (write to a temp file, then rename) and guarded by the `filelock` library (dependency recorded as decision D-14 in `tracker.md`).
- A corrupt memory file is moved aside as `*.corrupt-<timestamp>`; the agent starts with empty memory and says so.

## B.7 Tests

| Test | Checks |
|---|---|
| T-MEM-01 | Rejected keys and secret-looking values are not stored |
| T-MEM-02 | A science-critical value with origin `memory` or `inferred` forces a confirmation step |
| T-MEM-03 | Playbook counters update after a successful and a failed retry |
| T-MEM-04 | Corrupt file recovery |
| T-MEM-05 | "Forget" removes a learned entry from disk and, for a shipped entry, adds its pattern to `disabled_shipped`, which the diagnostician then honors |
| T-MEM-06 | Concurrent writers do not lose updates |
| T-MEM-07 | Entries unused for 180 days are listed, not deleted |
| T-MEM-08 | An explicit current instruction overrides a stored preference (stub-LLM conversation) |
| T-MEM-09 | A learned entry with a non-whitelisted `fix` key is rejected; a learned entry starts with `auto_allowed: no` |
| T-MEM-10 | At most 5 memory items are injected per turn |
| T-MEM-11 | A stub-agent attempt to mark a job passed from memory has no effect; only gates decide (RULE-M-02) |
| T-MEM-12 | Every L3 and L4 entry has `source`, `last_used`, and `confidence`, every L5 entry has `source` and `created`, and entries missing a field are rejected on write (RULE-M-05) |
| T-MEM-13 | The 7-day review check runs at session start, the L5 entry is written at job end, and a missing L5 file is rebuilt from `list_jobs` and `get_job` |
