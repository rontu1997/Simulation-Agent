import json

import pytest

from simagent.config import Config
from simagent.core.gmx import RunResult
from simagent.structure_analysis import (
    StructureAnalysisError,
    analyze_structure,
    parse_analysis_text,
)


class FakeAnalysisGmx:
    def __init__(self, fail: str | None = None) -> None:
        self.commands: list[list[str]] = []
        self.stdin: list[str | None] = []
        self.fail = fail

    def run(self, args, *, cwd, **kwargs):
        command = list(args)
        self.commands.append(command)
        self.stdin.append(kwargs.get("stdin"))
        if command[0] == self.fail:
            return RunResult(1, "", "selection unavailable")
        output = cwd / command[command.index("-o") + 1]
        if command[0] == "energy":
            values = {"Potential": (-100, -102), "Total Energy": (-90, -92),
                      "Temperature": (298, 302), "Pressure": (0, 2),
                      "Density": (1000, 1002)}
            selected = [name for name in values
                        if name.replace(" ", "-") in kwargs["stdin"].splitlines()]
            header = "".join(f'@ s{i} legend "{name}"\n'
                             for i, name in enumerate(selected))
            rows = "".join(f"{frame} " + " ".join(str(values[name][frame])
                           for name in selected) + "\n" for frame in (0, 1))
            output.write_text(header + rows, encoding="utf-8")
        elif command[0] == "rms":
            output.write_text("0 0.10\n1 0.20\n", encoding="utf-8")
        elif command[0] == "gyrate":
            output.write_text("0 1.0 0.7 0.8 0.9\n1 1.2 0.8 0.9 1.0\n",
                              encoding="utf-8")
        return RunResult(0, "", "")


def finished_run(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "structure_run.json").write_text(json.dumps({
        "status": "done", "stages": ["em", "nvt", "npt", "prod"],
        "force_field": "oplsaa", "water_model": "tip4p",
        "settings": {"temperature_K": 300.0, "pressure_bar": 1.0},
    }), encoding="utf-8")
    for name in ("prod.edr", "prod.tpr", "prod.xtc"):
        (run / name).write_text("data", encoding="utf-8")
    return run


def test_existing_run_analysis_writes_report_and_does_not_rerun_md(tmp_path):
    run = finished_run(tmp_path)
    fake = FakeAnalysisGmx()
    report = analyze_structure(run, Config(), fake)
    assert report == run / "report.md"
    assert [cmd[0] for cmd in fake.commands] == ["energy", "rms", "gyrate"]
    assert fake.stdin == ["Potential\nTotal-Energy\nTemperature\nPressure\nDensity\n0\n",
                          "Backbone\nBackbone\n", None]
    assert "group \"Protein\"" in fake.commands[2]
    summary = json.loads((run / "analysis" / "summary.json").read_text())
    assert summary["stage"] == "prod"
    assert summary["metrics"]["Temperature"]["mean"] == 300.0
    assert summary["metrics"]["backbone_rmsd"]["mean"] == pytest.approx(0.15)
    assert "300" in report.read_text()
    assert "analysis/overview.png" in report.read_text()
    assert (run / "analysis" / "overview.png").is_file()
    assert json.loads((run / "structure_run.json").read_text())["analysis_status"] == "done"
    assert analyze_structure(run, Config(), fake) == report
    assert len(fake.commands) == 3


def test_analysis_failure_preserves_completed_simulation_status(tmp_path):
    run = finished_run(tmp_path)
    with pytest.raises(StructureAnalysisError, match="energy"):
        analyze_structure(run, Config(), FakeAnalysisGmx(fail="energy"))
    manifest = json.loads((run / "structure_run.json").read_text())
    assert manifest["status"] == "done"
    assert manifest["analysis_status"] == "failed"


def test_missing_protein_group_is_reported_without_losing_thermo(tmp_path):
    run = finished_run(tmp_path)
    report = analyze_structure(run, Config(), FakeAnalysisGmx(fail="rms"))
    summary = json.loads((run / "analysis" / "summary.json").read_text())
    assert summary["analysis_status"] == "partial"
    assert summary["metrics"]["Temperature"]["mean"] == 300.0
    assert "Backbone RMSD unavailable" in report.read_text()


def test_plain_text_analysis_selection_and_unsupported_terms():
    assert parse_analysis_text(
        "Please analyze backbone RMSD, radius of gyration and temperature", "prod"
    ) == ("temperature", "backbone_rmsd", "radius_of_gyration")
    assert parse_analysis_text("energy and Rg", "prod") == (
        "potential", "total_energy", "radius_of_gyration")
    with pytest.raises(StructureAnalysisError, match="RMSF"):
        parse_analysis_text("RMSF", "prod")
    with pytest.raises(StructureAnalysisError, match="density.*NVT"):
        parse_analysis_text("density", "nvt")


def test_selected_metrics_only_and_reanalysis_does_not_rerun_md(tmp_path):
    run = finished_run(tmp_path)
    fake = FakeAnalysisGmx()
    analyze_structure(run, Config(), fake, requested=("backbone_rmsd",))
    assert [cmd[0] for cmd in fake.commands] == ["rms"]
    assert set(json.loads((run / "analysis" / "summary.json").read_text())["metrics"]) == {
        "backbone_rmsd"}
    analyze_structure(run, Config(), fake, requested=("temperature",))
    assert [cmd[0] for cmd in fake.commands] == ["rms", "energy"]
    summary = json.loads((run / "analysis" / "summary.json").read_text())
    assert summary["requested"] == ["temperature"]
    assert set(summary["metrics"]) == {"Temperature"}
    assert "Backbone RMSD" not in (run / "report.md").read_text()
