# Simulation Agent GitHub Preparation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sanitize and clean the existing Simulation Agent repository for publication as a private GitHub repository.

**Architecture:** Retain the existing repository history and source while removing ignored local environments, caches, editor configuration, runtime outputs, and local workflow state. Blank the API credential in the ignored `.env` file and validate the tracked tree before any remote publication.

**Tech Stack:** Python 3.11+, uv, GROMACS, MCP, Git

**Spec:** User-approved three-private-repository cleanup arrangement from 2026-10-27.

## Global Constraints

- Never expose or print secret values.
- Preserve `.env.example`; keep `.env` ignored.
- Preserve source, tests, configuration templates, documentation, `uv.lock`, and existing Git history.
- Do not change simulation behavior.

## Review Focus

- `LUNA_API_KEY` must have an empty value after cleanup.
- Large trajectories and all `runs/` data must be absent from the Git index.
- Local `.venv`, `.venv-wsl`, cache, editor, and workflow-state directories must be absent.
- Existing tracked changes must be preserved and reviewed rather than overwritten.
- No credential-shaped file may be staged.

---

### Task 1: Sanitize and clean ignored local data

**Files:**
- Modify: `.env`
- Delete: `.venv/`, `.venv-wsl/`, `.mypy_cache/`, `.pytest_cache/`, `.ruff_cache/`, `.superpowers/`, `.vscode/`, `runs/`, and nested Python cache directories

**Interfaces:**
- Consumes: existing `.gitignore` rules and repository history
- Produces: a clean working tree without credentials or generated simulation data

- [ ] Blank `LUNA_API_KEY` without displaying its prior value.
- [ ] Delete the listed ignored/generated directories.
- [ ] Confirm `.env`, environments, caches, and `runs/` are ignored and untracked.

### Task 2: Verify and prepare the existing repository

**Files:**
- Modify only files already changed by the approved cleanup or required ignore-rule corrections

**Interfaces:**
- Consumes: the existing Git repository
- Produces: a verified `main` branch ready for a private GitHub remote

- [ ] Inspect status, branch, remotes, and tracked file list using a per-command safe-directory setting.
- [ ] Run the relevant test and lint checks after cleanup using the existing environment before its removal or a recreated locked environment.
- [ ] Stage only repository-preparation changes and create a commit without overwriting prior work.
