"""Coordinate-structure simulations using installed GROMACS residue topologies."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from simagent.config import Config
from simagent.core.gmx import RunnerLike
from simagent.core.mdp import render_mdp
from simagent.core.paths import sha256_file
from simagent.structure_analysis import (
    StructureAnalysisError,
    analyze_structure,
    default_analyses,
    validate_analyses,
)


class StructureRunError(Exception):
    """A structure could not be prepared or a selected stage failed."""


@dataclass(frozen=True)
class StructureRun:
    input_path: Path
    output_dir: Path
    force_field: str
    water_model: str
    stages: tuple[str, ...]
    temperature_K: float = 300.0
    pressure_bar: float = 1.0
    dt_ps: float = 0.002
    nvt_ps: float = 100.0
    npt_ps: float = 100.0
    prod_ns: float = 1.0
    padding_nm: float = 1.0
    seed: int = 42
    neutralize: bool = False
    analyses: tuple[str, ...] | None = None


_STAGE_SEQUENCES = {("em", "nvt"), ("em", "nvt", "npt"),
                    ("em", "nvt", "npt", "prod")}
_WATER_MODELS = {"spc", "spce", "tip3p", "tip4p", "tip5p", "tips3p"}


def _check_options(options: StructureRun) -> None:
    if (not options.input_path.is_file()
            or options.input_path.suffix.lower() not in {".pdb", ".gro"}):
        raise StructureRunError("input must be an existing PDB or GRO coordinate file")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", options.force_field):
        raise StructureRunError("force field must be the name of an installed .ff directory")
    if options.water_model not in _WATER_MODELS:
        raise StructureRunError(f"unsupported water model: {options.water_model}")
    if options.stages not in _STAGE_SEQUENCES:
        raise StructureRunError("stages must be em,nvt or em,nvt,npt or em,nvt,npt,prod")
    limits = (("temperature_K", options.temperature_K, 200, 450),
              ("pressure_bar", options.pressure_bar, 0.1, 5000),
              ("dt_ps", options.dt_ps, 0.0005, 0.002),
              ("nvt_ps", options.nvt_ps, 10, 10000),
              ("npt_ps", options.npt_ps, 10, 10000),
              ("prod_ns", options.prod_ns, 0.01, 100),
              ("padding_nm", options.padding_nm, 1.0, 20.0))
    for name, value, low, high in limits:
        if not low <= value <= high:
            raise StructureRunError(f"{name} must be between {low} and {high}")
    if options.seed < 0 or options.seed >= 2**31:
        raise StructureRunError("seed must be between 0 and 2**31 - 1")
    if options.analyses is not None:
        try:
            validate_analyses(options.analyses, options.stages[-1])
        except StructureAnalysisError as exc:
            raise StructureRunError(str(exc)) from exc


def _plan(options: StructureRun) -> dict[str, object]:
    dt = options.dt_ps
    wet = {"dt": dt, "ref_t": options.temperature_K, "tau_t": 0.1}
    pressure = {**wet, "ref_p": options.pressure_bar, "tau_p": 2.0}
    params = {
        "em": {"emtol": 1000.0, "nsteps": 50000},
        "nvt": {**wet, "nsteps": round(options.nvt_ps / dt), "seed": options.seed},
        "npt": {**pressure, "nsteps": round(options.npt_ps / dt)},
        "prod": {**pressure, "nsteps": round(options.prod_ns * 1000 / dt)},
    }
    return {"stages": [{"name": name, "params": params[name]} for name in options.stages]}


def _run_command(runner: RunnerLike, output: Path, stage: str, args: list[str],
                 expected: Path, timeout: float | None = None,
                 stdin: str | None = None) -> None:
    result = runner.run(args, cwd=output, stage=stage, tool=args[0], base=output,
                        timeout=timeout, stdin=stdin)
    prefix = stage if stage == args[0] else f"{stage}.{args[0]}"
    (output / f"{prefix}.stdout.log").write_text(result.stdout, encoding="utf-8")
    (output / f"{prefix}.stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.exit_code != 0 or not expected.is_file():
        detail = " | ".join(result.stderr.strip().splitlines()[-5:])
        log = output / f"{prefix}.stderr.log"
        raise StructureRunError(f"{stage}: gmx {args[0]} failed (exit {result.exit_code}): "
                                f"{detail or 'expected output missing'}; see {log}")


def run_structure(options: StructureRun, cfg: Config, runner: RunnerLike) -> Path:
    """Prepare a solvated structure, then run only the selected contiguous stages."""
    _check_options(options)
    output = options.output_dir.resolve()
    if output.exists():
        raise StructureRunError(f"output directory already exists: {output}")
    output.mkdir(parents=True)
    source = options.input_path.resolve()
    target = output / f"input{source.suffix.lower()}"
    shutil.copyfile(source, target)
    manifest = {"input": str(source), "input_sha256": sha256_file(target),
                "force_field": options.force_field, "water_model": options.water_model,
                "stages": list(options.stages), "status": "preparing",
                "analysis_requested": list(options.analyses or
                                           default_analyses(options.stages[-1])),
                "settings": {"temperature_K": options.temperature_K,
                             "pressure_bar": options.pressure_bar, "dt_ps": options.dt_ps,
                             "nvt_ps": options.nvt_ps, "npt_ps": options.npt_ps,
                             "prod_ns": options.prod_ns, "padding_nm": options.padding_nm,
                             "seed": options.seed, "neutralize": options.neutralize}}
    status_path = output / "structure_run.json"

    def save() -> None:
        status_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    save()
    try:
        _run_command(runner, output, "pdb2gmx",
                     ["pdb2gmx", "-f", target.name, "-o", "processed.gro", "-p", "topol.top",
                      "-ff", options.force_field, "-water", options.water_model],
                     output / "processed.gro")
        if not (output / "topol.top").is_file():
            raise StructureRunError("pdb2gmx did not write topol.top")
        _run_command(runner, output, "editconf",
                     ["editconf", "-f", "processed.gro", "-o", "boxed.gro", "-c", "-d",
                      str(options.padding_nm), "-bt", "cubic"], output / "boxed.gro")
        solvent = "tip4p.gro" if options.water_model == "tip4p" else (
            "tip5p.gro" if options.water_model == "tip5p" else "spc216.gro")
        _run_command(runner, output, "solvate",
                     ["solvate", "-cp", "boxed.gro", "-cs", solvent, "-o", "solvated.gro",
                      "-p", "topol.top"], output / "solvated.gro")
        previous = "solvated"
        if options.neutralize:
            # This short TPR is only an input to genion. Cut-off electrostatics
            # permit a charged starting structure without suppressing warnings
            # in the actual PME-based EM and MD stages.
            (output / "ions.mdp").write_text(
                "integrator = steep\n"
                "nsteps = 0\n"
                "cutoff-scheme = Verlet\n"
                "pbc = xyz\n"
                "coulombtype = Cut-off\n"
                "rcoulomb = 1.0\n"
                "vdwtype = Cut-off\n"
                "rvdw = 1.0\n", encoding="utf-8")
            _run_command(runner, output, "ions",
                         ["grompp", "-f", "ions.mdp", "-c", "solvated.gro",
                          "-p", "topol.top", "-o", "ions.tpr"], output / "ions.tpr")
            _run_command(runner, output, "ions",
                         ["genion", "-s", "ions.tpr", "-o", "ionized.gro",
                          "-p", "topol.top", "-pname", "NA", "-nname", "CL",
                          "-neutral", "-seed", str(options.seed)],
                         output / "ionized.gro", stdin="SOL\n")
            previous = "ionized"
        plan = _plan(options)
        for stage in options.stages:
            (output / f"{stage}.mdp").write_text(render_mdp(stage, plan, {}),
                                                 encoding="utf-8")
            args = ["grompp", "-f", f"{stage}.mdp", "-c", f"{previous}.gro",
                    "-p", "topol.top", "-o", f"{stage}.tpr"]
            if previous in ("nvt", "npt"):
                args.extend(["-t", f"{previous}.cpt"])
            _run_command(runner, output, stage, args, output / f"{stage}.tpr")
            _run_command(runner, output, stage,
                         ["mdrun", "-s", f"{stage}.tpr", "-deffnm", stage],
                         output / f"{stage}.gro", cfg.gmx.max_wall_hours * 3600)
            previous = stage
            manifest["status"] = f"{stage} finished"
            save()
        manifest["status"] = "done"
        save()
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        save()
        raise
    try:
        analyze_structure(output, cfg, runner, requested=options.analyses)
    except StructureAnalysisError as exc:
        raise StructureRunError(
            f"simulation stages completed, but analysis failed: {exc}; "
            f"retry with simagent analyze-structure --run-dir {output}") from exc
    return output
