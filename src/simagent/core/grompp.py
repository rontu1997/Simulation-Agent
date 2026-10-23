"""run_grompp (design 4.3 items 6 and 9, 4.4): input checks, two-pass warning acknowledgement."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from simagent.config import Config
from simagent.core.gmx import RunnerLike
from simagent.core.jobs import PROCESS_STAGES, JobHandle
from simagent.core.mdp import render_mdp, stage_params
from simagent.core.paths import sha256_file, sha256_text
from simagent.core.provenance import record_internal
from simagent.errors import ErrorCode, SimAgentError

ORDER = ("build", "em", "nvt", "npt", "prod")
TOPOL = "../01_build/topol.top"
IN_GRO = {
    "em": "../01_build/water.gro", "nvt": "../02_em/em.gro",
    "npt": "../03_nvt/nvt.gro", "prod": "../04_npt/npt.gro",
}
CPT = {"npt": "../03_nvt/nvt.cpt", "prod": "../04_npt/npt.cpt"}

# [verify TASK-034] the block layout of grompp warnings and notes
_BLOCK = r"^{label} (\d+)(?: \[([^\]]*)\])?:[ \t]*\n((?:[ \t]+\S.*\n?)+)"
_WARNING = re.compile(_BLOCK.format(label="WARNING"), re.MULTILINE)
_NOTE = re.compile(_BLOCK.format(label="NOTE"), re.MULTILINE)


def _normalize(text: str) -> str:
    return " ".join(part.strip() for part in text.strip().splitlines() if part.strip())


def _source(raw: str | None) -> str:
    if not raw:
        return ""
    match = re.match(r"file ([^,]+), line (\d+)", raw)
    return f"{match.group(1)}:{match.group(2)}" if match else raw


def parse_warnings(text: str) -> list[dict[str, str]]:
    found = []
    for match in _WARNING.finditer(text):
        message = _normalize(match.group(3))
        source = _source(match.group(2))
        digest = sha256_text(f"{source}|{message}")[:12]
        found.append({"id": digest, "text": message, "source": source})
    return found


def parse_notes(text: str) -> list[str]:
    return [_normalize(m.group(3)) for m in _NOTE.finditer(text)]


def check_binary(job: JobHandle, cfg: Config) -> None:
    """E_BINARY_CHANGED if the binary's SHA-256 differs from the one recorded for the job."""
    environment = job.read()["environment"]
    path = Path(cfg.gmx.bin)
    try:
        stat = path.stat()
    except OSError as exc:
        raise SimAgentError(ErrorCode.E_BINARY_CHANGED, f"the GROMACS binary is gone: {path}",
                            hint="restore the binary this job was created with") from exc
    recorded = environment.get("bin_stat") or {}
    if recorded.get("size") == stat.st_size and recorded.get("mtime_ns") == stat.st_mtime_ns:
        return
    if sha256_file(path) != environment.get("sha256"):
        raise SimAgentError(
            ErrorCode.E_BINARY_CHANGED, "the GROMACS binary differs from the one the job was "
            "created with", hint="finish the job with the original binary or start a new job")


def _modified(what: str) -> SimAgentError:
    return SimAgentError(ErrorCode.E_INPUT_MODIFIED, f"{what} differs from what was approved",
                         hint="rebuild or re-render through the tools, then plan and approve again")


def check_inputs(job: JobHandle, stage: str, plan: dict[str, Any], request: dict[str, Any]) -> None:
    build_dir = job.stage_dir("build")
    for name in ("water.gro", "topol.top"):
        if not (build_dir / name).is_file():
            raise _modified(f"build file {name} is missing; it")
    actual = sha256_text(sha256_file(build_dir / "water.gro")
                         + sha256_file(build_dir / "topol.top"))
    if actual != stage_params(plan, "build")["build_sha256"]:
        raise _modified("the build (water.gro and topol.top)")
    needed = [TOPOL, IN_GRO[stage], *([CPT[stage]] if stage in CPT else [])]
    for rel in needed:
        job_rel = rel.replace("../", "")
        path = job.dir / job_rel
        if not path.is_file() or not job.ledger.has_receipt(job_rel, sha256_file(path)):
            raise _modified(f"{job_rel} (no matching receipt)")
    data = job.read()
    expected = render_mdp(stage, plan, request, data["retries"])
    mdp = job.stage_dir(stage) / f"{stage}.mdp"
    if not mdp.is_file() or mdp.read_text(encoding="utf-8") != expected:
        raise _modified(f"{stage}.mdp")
    if data["mdp_expected_sha256"].get(stage) != sha256_text(expected):
        raise _modified(f"the recorded expected hash of {stage}.mdp")


def _tail(text: str, lines: int = 4) -> str:
    kept = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return " | ".join(kept[-lines:])


def run_grompp(job: JobHandle, stage: str, runner: RunnerLike, cfg: Config) -> dict[str, Any]:
    if stage not in IN_GRO:
        raise SimAgentError(ErrorCode.E_PARAM_OUT_OF_RANGE, f"cannot run grompp for {stage}",
                            hint="stage must be em, nvt, npt, or prod")
    data = job.read()
    check_binary(job, cfg)
    if any(data["stage_status"][s] == "RUNNING" for s in PROCESS_STAGES):
        raise SimAgentError(ErrorCode.E_MDRUN_RUNNING, "a stage process is running",
                            hint="wait for it or cancel it")
    plan = data["plan"]
    if plan is None or any(data["stage_status"][s] != "PASSED"
                           for s in ORDER[:ORDER.index(stage)]):
        raise SimAgentError(ErrorCode.E_STAGE_ORDER,
                            f"an earlier stage is not complete for {stage}",
                            hint="store a plan and pass the earlier stages first")
    if data["stage_status"][stage] != "PENDING":
        raise SimAgentError(ErrorCode.E_STAGE_STATE,
                            f"{stage} is {data['stage_status'][stage]}, not PENDING",
                            hint="reset the stage before running grompp again")
    check_inputs(job, stage, plan, data["request"])

    stage_dir = job.stage_dir(stage)
    argv = ["grompp", "-f", f"{stage}.mdp", "-c", IN_GRO[stage], "-p", TOPOL,
            "-o", f"{stage}.tpr", "-po", f"{stage}_out.mdp"]
    if stage in CPT:
        argv += ["-t", CPT[stage]]
    kwargs: dict[str, Any] = {
        "cwd": stage_dir, "stage": stage, "tool": "run_grompp", "base": job.dir,
        "outputs": [stage_dir / f"{stage}.tpr", stage_dir / f"{stage}_out.mdp"]}
    first = runner.run(argv, **kwargs)
    result = first
    warnings = parse_warnings(first.stderr + "\n" + first.stdout)
    acknowledged = {a["id"] for a in data["acknowledged_warnings"]}
    annotated = [{**w, "acknowledged": w["id"] in acknowledged} for w in warnings]
    if first.exit_code != 0 and warnings and all(w["acknowledged"] for w in annotated):
        result = runner.run([*argv, "-maxwarn", str(len(warnings))], **kwargs)
        annotated = [{**w, "acknowledged": True} for w in warnings]
    (stage_dir / "grompp.out").write_text(result.stdout + result.stderr, encoding="utf-8")

    if result.exit_code != 0:
        stored = [{k: w[k] for k in ("id", "text", "source")} for w in annotated]
        cause = "unacknowledged grompp warnings" if annotated else _tail(result.stderr)
        def record_failure(d: dict[str, Any]) -> None:
            d["failures"].append({"stage": stage, "cause": cause,
                                  "time": datetime.now(UTC).isoformat()})
            d["pending_warnings"] = stored

        job.update(record_failure)
        if annotated:
            raise SimAgentError(
                ErrorCode.E_GROMPP_FAILED,
                f"grompp stopped on {len(annotated)} warning(s) that need a decision",
                hint="acknowledge each warning you accept, or fix the cause",
                data={"warnings": annotated})
        raise SimAgentError(ErrorCode.E_GROMPP_FAILED, f"gmx grompp failed: {cause}",
                            hint="see grompp.out in the stage directory")
    record_internal(job.ledger, stage=stage, tool="run_grompp", base=job.dir,
                    outputs=[f"{Path(stage_dir).name}/{stage}.mdp"])
    job.set_status(stage, "READY")
    job.update(lambda d: d.update(pending_warnings=[]))
    return {"tpr": f"{Path(stage_dir).name}/{stage}.tpr", "warnings": annotated,
            "notes": parse_notes(result.stderr + "\n" + result.stdout)}
