"""Postprocess a completed coordinate-structure run without repeating MD."""

from __future__ import annotations

import json
import math
import re
import statistics
from pathlib import Path
from typing import Any

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from simagent.analysis.xvg import parse_xvg
from simagent.config import Config
from simagent.core.gmx import RunnerLike


class StructureAnalysisError(Exception):
    """A completed run could not be analyzed."""


_THERMO = {
    "Potential": ("Potential energy", "kJ/mol"),
    "Total Energy": ("Total energy", "kJ/mol"),
    "Temperature": ("Temperature", "K"),
    "Pressure": ("Pressure", "bar"),
    "Density": ("Density", "kg/m³"),
}
_ORDER = ("potential", "total_energy", "temperature", "pressure", "density",
          "backbone_rmsd", "radius_of_gyration")
_LABELS = {"potential": "potential energy", "total_energy": "total energy",
           "temperature": "temperature", "pressure": "pressure", "density": "density",
           "backbone_rmsd": "backbone RMSD", "radius_of_gyration": "radius of gyration"}
_GROMACS_TERMS = {"potential": "Potential", "total_energy": "Total-Energy",
                  "temperature": "Temperature", "pressure": "Pressure", "density": "Density"}
_LEGEND_KEYS = {"Potential": "potential", "Total Energy": "total_energy",
                "Temperature": "temperature", "Pressure": "pressure", "Density": "density"}
_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (r"\bpotential(?:\s+energy)?\b", ("potential",)),
    (r"\btotal[\s-]*energy\b", ("total_energy",)),
    (r"\bbackbone[\s-]*rmsd\b", ("backbone_rmsd",)),
    (r"\broot[\s-]+mean[\s-]+square[\s-]+deviation\b", ("backbone_rmsd",)),
    (r"\bradius[\s-]+of[\s-]+gyration\b", ("radius_of_gyration",)),
    (r"\brg\b|\bgyration\b", ("radius_of_gyration",)),
    (r"\brmsd\b", ("backbone_rmsd",)),
    (r"\btemperatures?\b|\btemp\b", ("temperature",)),
    (r"\bpressures?\b", ("pressure",)),
    (r"\bdensit(?:y|ies)\b", ("density",)),
    (r"\benergy\b", ("potential", "total_energy")),
)
_FILLER = re.compile(
    r"\b(?:please|analy[sz]e|analysis|calculate|compute|show|plot|report|"
    r"provide|me|the|a|an|of|and|for|protein|structure|my|i|want|to|only|"
    r"also|include|with|from|run|on|do)\b|[,;.:/&+!?()\[\]-]",
    re.IGNORECASE)


def default_analyses(stage: str) -> tuple[str, ...]:
    """Measurements available from the final MD stage by default."""
    return tuple(name for name in _ORDER if stage != "nvt" or name != "density")


def analysis_labels(names: tuple[str, ...]) -> str:
    return ", ".join(_LABELS[name] for name in names)


def validate_analyses(names: tuple[str, ...], stage: str) -> None:
    if not names or len(set(names)) != len(names) or set(names) - set(_ORDER):
        raise StructureAnalysisError("analyses must name one or more supported measurements")
    if stage == "nvt" and "density" in names:
        raise StructureAnalysisError(
            "density is unavailable for an NVT-only run; select NPT or production")


def parse_analysis_text(text: str, stage: str) -> tuple[str, ...]:
    """Resolve a short plain-language request to supported GROMACS measurements."""
    remaining = text.strip()
    if not remaining:
        return default_analyses(stage)
    selected: set[str] = set()
    for pattern, names in _ALIASES:
        if re.search(pattern, remaining, flags=re.IGNORECASE):
            selected.update(names)
            remaining = re.sub(pattern, " ", remaining, flags=re.IGNORECASE)
    unknown = " ".join(_FILLER.sub(" ", remaining).split())
    if unknown:
        raise StructureAnalysisError(
            f"unsupported analysis request: {unknown}. Supported: "
            f"{analysis_labels(default_analyses(stage))}")
    if not selected:
        raise StructureAnalysisError("specify at least one analysis: "
                                     f"{analysis_labels(default_analyses(stage))}")
    names = tuple(name for name in _ORDER if name in selected)
    validate_analyses(names, stage)
    return names


def _run_xvg(runner: RunnerLike, run_dir: Path, analysis_dir: Path,
             name: str, args: list[str], *, stdin: str | None = None,
             timeout: float | None = None) -> Path:
    output = analysis_dir / f"{name}.xvg"
    result = runner.run(args, cwd=run_dir, stage="analysis", tool=args[0],
                        base=run_dir, stdin=stdin, timeout=timeout)
    (analysis_dir / f"{name}.stdout.log").write_text(result.stdout, encoding="utf-8")
    (analysis_dir / f"{name}.stderr.log").write_text(result.stderr, encoding="utf-8")
    if result.exit_code != 0 or not output.is_file():
        detail = " | ".join(result.stderr.strip().splitlines()[-5:])
        raise StructureAnalysisError(
            f"{name}: gmx {args[0]} failed (exit {result.exit_code}): "
            f"{detail or 'expected output missing'}; see analysis/{name}.stderr.log")
    return output


def _series(path: Path, column: int) -> dict[str, float | int]:
    _, rows = parse_xvg(path.read_text(encoding="utf-8"))
    if not rows or any(len(row) <= column or not math.isfinite(row[column])
                       for row in rows):
        raise StructureAnalysisError(f"{path.name}: missing or non-finite data")
    values = [row[column] for row in rows]
    return {"mean": statistics.fmean(values),
            "std_dev": statistics.stdev(values) if len(values) > 1 else 0.0,
            "minimum": min(values), "maximum": max(values),
            "first": values[0], "last": values[-1], "n_frames": len(values),
            "time_start": rows[0][0], "time_end": rows[-1][0]}


def _plot_overview(analysis_dir: Path, metrics: dict[str, dict[str, Any]]) -> None:
    names = [name for name in ("Temperature", "Pressure", "Density", "Potential",
                               "Total Energy", "backbone_rmsd", "radius_of_gyration")
             if name in metrics]
    columns = 2 if len(names) > 1 else 1
    rows_count = math.ceil(len(names) / columns)
    figure = Figure(figsize=(6 * columns, 3 * rows_count))
    FigureCanvasAgg(figure)
    axes = list(figure.subplots(rows_count, columns, squeeze=False).flat)
    for axis, name in zip(axes, names):
        metric = metrics[name]
        legends, rows = parse_xvg((analysis_dir.parent / metric["file"]).read_text(
            encoding="utf-8"))
        column = metric.get("column", legends.index(name) + 1 if name in legends else 1)
        axis.plot([row[0] for row in rows],
                  [row[column] for row in rows], linewidth=0.8)
        axis.set_title(metric["label"])
        axis.set_ylabel(metric["unit"])
        axis.set_xlabel("Time (ns)" if name in ("backbone_rmsd", "radius_of_gyration")
                        else "Time (ps)")
        axis.grid(alpha=0.2)
    for axis in axes[len(names):]:
        axis.set_visible(False)
    figure.tight_layout()
    figure.savefig(analysis_dir / "overview.png", dpi=150)


def _report(manifest: dict[str, Any], summary: dict[str, Any]) -> str:
    stage = summary["stage"]
    metrics = summary["metrics"]
    requested = tuple(summary.get("requested", default_analyses(stage)))
    lines = ["# Structure simulation report", "",
             f"- Input: `{manifest.get('input', 'not recorded')}`",
             f"- Force field: `{manifest.get('force_field', 'not recorded')}`",
             f"- Water model: `{manifest.get('water_model', 'not recorded')}`",
             f"- Completed stages: {', '.join(manifest['stages'])}",
             f"- Analyzed stage: `{stage}`",
             f"- Requested analyses: {analysis_labels(requested)}", "",
             "## Results", "",
             "| Measurement | Mean | Standard deviation | First | Last | Frames |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for item in metrics.values():
        unit = item["unit"]
        label = item["label"] + (f" ({unit})" if unit else "")
        lines.append(f"| {label} | {item['mean']:.6g} | {item['std_dev']:.6g} | "
                     f"{item['first']:.6g} | {item['last']:.6g} | {item['n_frames']} |")
    lines.extend(["", "![Simulation time series](analysis/overview.png)", "",
                  "## Methods and limits", "",
                  f"Values come from the full `{stage}` stage with no frames discarded.",
                  "Standard deviation describes sampled frame variation; it is not an "
                  "uncertainty estimate for the mean."])
    if "Pressure" in metrics:
        lines.append("Pressure can fluctuate substantially in small systems and short runs.")
    if "backbone_rmsd" in metrics:
        lines.append("Backbone RMSD is fitted to the stage TPR starting structure.")
    if "radius_of_gyration" in metrics:
        lines.append("Radius of gyration uses the Protein selection.")
    files = sorted({item["file"] for item in metrics.values()})
    lines.append("See `analysis/summary.json` for exact values. Time series: "
                 + ", ".join(f"`{name}`" for name in files) + ".")
    if summary["warnings"]:
        lines.extend(["", "## Unavailable measurements", ""])
        lines.extend(f"- {warning}" for warning in summary["warnings"])
    return "\n".join(lines) + "\n"


def analyze_structure(run_dir: Path, cfg: Config, runner: RunnerLike,
                      requested: tuple[str, ...] | None = None) -> Path:
    """Analyze the final MD stage of an existing successful structure run."""
    run_dir = run_dir.resolve()
    manifest_path = run_dir / "structure_run.json"
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StructureAnalysisError(f"cannot read {manifest_path}: {exc}") from exc
    if manifest.get("status") != "done":
        raise StructureAnalysisError("structure simulation is not marked done")
    stages = manifest.get("stages")
    if not isinstance(stages, list) or not stages or stages[-1] not in (
            "nvt", "npt", "prod"):
        raise StructureAnalysisError("manifest has no completed MD stage to analyze")
    stage = stages[-1]
    stored = manifest.get("analysis_requested")
    previous_choice = tuple(stored) if isinstance(stored, list) else default_analyses(stage)
    chosen = requested if requested is not None else previous_choice
    validate_analyses(chosen, stage)
    for suffix in ("edr", "tpr", "xtc"):
        if not (run_dir / f"{stage}.{suffix}").is_file():
            raise StructureAnalysisError(f"missing {stage}.{suffix} in {run_dir}")
    report_path = run_dir / "report.md"
    analysis_dir = run_dir / "analysis"
    if (manifest.get("analysis_status") in ("done", "partial")
            and report_path.is_file() and chosen == previous_choice):
        if not (analysis_dir / "overview.png").is_file():
            summary = json.loads((analysis_dir / "summary.json").read_text(encoding="utf-8"))
            _plot_overview(analysis_dir, summary["metrics"])
            report_path.write_text(_report(manifest, summary), encoding="utf-8")
        return report_path
    analysis_dir.mkdir(exist_ok=True)
    manifest["analysis_status"] = "running"
    manifest["analysis_requested"] = list(chosen)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    try:
        metrics: dict[str, dict[str, Any]] = {}
        thermo_names = [name for name in chosen if name in _GROMACS_TERMS]
        if thermo_names:
            selections = [_GROMACS_TERMS[name] for name in thermo_names]
            thermo = _run_xvg(
                runner, run_dir, analysis_dir, "thermo",
                ["energy", "-f", f"{stage}.edr", "-o", "analysis/thermo.xvg"],
                stdin="\n".join([*selections, "0", ""]))
            legends, rows = parse_xvg(thermo.read_text(encoding="utf-8"))
            if not rows or {_LEGEND_KEYS.get(legend) for legend in legends} != set(
                    thermo_names):
                raise StructureAnalysisError("thermo.xvg lacks requested energy series")
            for column, legend in enumerate(legends, start=1):
                label, unit = _THERMO[legend]
                metrics[legend] = {"label": label, "unit": unit, "column": column,
                                   "file": "analysis/thermo.xvg",
                                   **_series(thermo, column)}
        warnings: list[str] = []
        timeout = cfg.gmx.max_wall_hours * 3600
        if "backbone_rmsd" in chosen:
            try:
                rmsd = _run_xvg(
                    runner, run_dir, analysis_dir, "backbone_rmsd",
                    ["rms", "-s", f"{stage}.tpr", "-f", f"{stage}.xtc", "-o",
                     "analysis/backbone_rmsd.xvg", "-tu", "ns"],
                    stdin="Backbone\nBackbone\n", timeout=timeout)
                metrics["backbone_rmsd"] = {
                    "label": "Backbone RMSD", "unit": "nm", "column": 1,
                    "file": "analysis/backbone_rmsd.xvg", **_series(rmsd, 1)}
            except StructureAnalysisError as exc:
                warnings.append(f"Backbone RMSD unavailable: {exc}")
        if "radius_of_gyration" in chosen:
            try:
                rg = _run_xvg(
                    runner, run_dir, analysis_dir, "radius_of_gyration",
                    ["gyrate", "-s", f"{stage}.tpr", "-f", f"{stage}.xtc", "-o",
                     "analysis/radius_of_gyration.xvg", "-tu", "ns", "-sel",
                     'group "Protein"'], timeout=timeout)
                metrics["radius_of_gyration"] = {
                    "label": "Protein radius of gyration", "unit": "nm", "column": 1,
                    "file": "analysis/radius_of_gyration.xvg", **_series(rg, 1)}
            except StructureAnalysisError as exc:
                warnings.append(f"Protein radius of gyration unavailable: {exc}")
        if not metrics:
            raise StructureAnalysisError("none of the requested analyses produced usable data")
        status = "partial" if warnings else "done"
        summary = {"stage": stage, "analysis_status": status,
                   "time_unit_thermo": "ps", "time_unit_structure": "ns",
                   "requested": list(chosen), "metrics": metrics, "warnings": warnings}
        _plot_overview(analysis_dir, metrics)
        (analysis_dir / "summary.json").write_text(
            json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        report_path.write_text(_report(manifest, summary), encoding="utf-8")
        manifest["analysis_status"] = status
        manifest["report"] = "report.md"
        manifest.pop("analysis_error", None)
    except Exception as exc:
        manifest["analysis_status"] = "failed"
        manifest["analysis_error"] = str(exc)
        raise
    finally:
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n",
                                 encoding="utf-8")
    return report_path
