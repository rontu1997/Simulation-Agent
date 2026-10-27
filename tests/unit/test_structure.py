import json

import pytest

from simagent.config import Config
from simagent.core.gmx import RunResult
from simagent.structure import StructureRun, StructureRunError, run_structure


class FakeGmx:
    def __init__(self, fail: str | None = None) -> None:
        self.commands: list[list[str]] = []
        self.stdin: list[str | None] = []
        self.fail = fail

    def run(self, args, *, cwd, **kwargs):
        command = list(args)
        self.commands.append(command)
        self.stdin.append(kwargs.get("stdin"))
        if command[0] == self.fail:
            return RunResult(1, "", "Residue XYZ not found in residue topology database")
        if "-o" in command:
            (cwd / command[command.index("-o") + 1]).write_text("output", encoding="utf-8")
        if command[0] == "pdb2gmx":
            (cwd / "topol.top").write_text("[ molecules ]\nProtein 1\n", encoding="utf-8")
        if command[0] == "mdrun":
            (cwd / f"{command[command.index('-deffnm') + 1]}.gro").write_text(
                "output", encoding="utf-8")
            for suffix in ("edr", "tpr", "xtc"):
                (cwd / f"{command[command.index('-deffnm') + 1]}.{suffix}").write_text(
                    "output", encoding="utf-8")
        if command[0] == "energy":
            nvt = command[command.index("-f") + 1] == "nvt.edr"
            header = (
                '@ s0 legend "Potential"\n@ s1 legend "Total Energy"\n'
                '@ s2 legend "Temperature"\n@ s3 legend "Pressure"\n')
            if not nvt:
                header += '@ s4 legend "Density"\n'
            row = '0 -100 -90 300 1' + ('' if nvt else ' 1000') + '\n'
            (cwd / command[command.index("-o") + 1]).write_text(
                header + row + row.replace('0 ', '1 ', 1),
                encoding="utf-8")
        if command[0] in ("rms", "gyrate"):
            (cwd / command[command.index("-o") + 1]).write_text(
                "0 0.1\n1 0.2\n", encoding="utf-8")
        return RunResult(0, "ok", "")


def test_structure_run_uses_pdb2gmx_force_field_and_stops_after_nvt(tmp_path):
    pdb = tmp_path / "solute.pdb"
    pdb.write_text("ATOM      1  CA  ALA A   1       0.000   0.000   0.000\nEND\n",
                   encoding="utf-8")
    fake = FakeGmx()
    output = tmp_path / "job"
    run_structure(StructureRun(pdb, output, "amber99sb-ildn", "tip3p", ("em", "nvt")),
                  Config(), fake)
    assert output.joinpath("nvt.gro").is_file()
    assert [command[0] for command in fake.commands] == [
        "pdb2gmx", "editconf", "solvate", "grompp", "mdrun", "grompp", "mdrun",
        "energy", "rms", "gyrate"]
    assert fake.commands[0][fake.commands[0].index("-ff") + 1] == "amber99sb-ildn"
    assert not output.joinpath("npt.mdp").exists()
    manifest = json.loads((output / "structure_run.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "done"
    assert manifest["analysis_status"] == "done"
    assert "density" not in manifest["analysis_requested"]
    assert manifest["settings"]["dt_ps"] == 0.002
    assert (output / "report.md").is_file()


def test_structure_run_can_include_npt_and_production(tmp_path):
    pdb = tmp_path / "solute.pdb"
    pdb.write_text("ATOM\nEND\n", encoding="utf-8")
    fake = FakeGmx()
    output = tmp_path / "job"
    run_structure(StructureRun(pdb, output, "oplsaa", "tip4p",
                               ("em", "nvt", "npt", "prod")), Config(), fake)
    assert [command[0] for command in fake.commands].count("mdrun") == 4
    assert "pcoupl                = C-rescale" in (output / "npt.mdp").read_text()
    assert output.joinpath("prod.gro").is_file()


def test_explicit_neutralization_replaces_water_before_em_without_maxwarn(tmp_path):
    pdb = tmp_path / "solute.pdb"
    pdb.write_text("ATOM\nEND\n", encoding="utf-8")
    fake = FakeGmx()
    output = tmp_path / "charged"
    run_structure(StructureRun(pdb, output, "oplsaa", "tip4p", ("em", "nvt"),
                               neutralize=True), Config(), fake)
    assert [command[0] for command in fake.commands[:6]] == [
        "pdb2gmx", "editconf", "solvate", "grompp", "genion", "grompp"]
    assert "-neutral" in fake.commands[4]
    assert fake.stdin[4] == "SOL\n"
    assert "-maxwarn" not in fake.commands[3]
    assert "ionized.gro" in fake.commands[5]
    assert "coulombtype = Cut-off" in (output / "ions.mdp").read_text(encoding="utf-8")
    manifest = json.loads((output / "structure_run.json").read_text(encoding="utf-8"))
    assert manifest["settings"]["neutralize"] is True


def test_pdb2gmx_failure_stops_before_md_and_keeps_diagnostic(tmp_path):
    pdb = tmp_path / "solute.pdb"
    pdb.write_text("ATOM\nEND\n", encoding="utf-8")
    fake = FakeGmx(fail="pdb2gmx")
    output = tmp_path / "job"
    with pytest.raises(StructureRunError, match="Residue XYZ"):
        run_structure(StructureRun(pdb, output, "oplsaa", "tip4p", ("em", "nvt")),
                      Config(), fake)
    assert [command[0] for command in fake.commands] == ["pdb2gmx"]
    assert "Residue XYZ" in (output / "pdb2gmx.stderr.log").read_text()
