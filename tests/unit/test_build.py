import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from simagent.config import load_config
from simagent.core.build import (
    build_water_box,
    count_sol,
    find_edge,
    initial_edge,
    number_density,
    topology_text,
    trim_gro,
)
from simagent.core.gmx import RunResult
from simagent.core.jobs import JobStore
from simagent.core.paths import sha256_file, sha256_text
from simagent.errors import ErrorCode, SimAgentError

REQUEST = {
    "system": {"kind": "water", "n_molecules": 1000, "water_model": "tip4p"},
    "force_field": "oplsaa",
    "thermo": {"temperature_K": 300.0, "pressure_bar": 1.0},
    "protocol": {"prod_ns": 1.0},
    "seed": 1,
}


def gro_text(n_mol: int, edge: float) -> str:
    lines = ["water", str(4 * n_mol)]
    atom = 0
    for mol in range(n_mol):
        for name in ("OW", "HW1", "HW2", "MW"):
            atom += 1
            lines.append("%5d%-5s%5s%5d%8.3f%8.3f%8.3f" % (mol + 1, "SOL", name, atom % 100000,
                                                         0.1, 0.2, 0.3))
    lines.append("%10.5f%10.5f%10.5f" % (edge, edge, edge))
    return "\n".join(lines) + "\n"


class StubRunner:
    """Stands in for GmxRunner: writes what solvate and editconf would write."""

    def __init__(self, density: float = 33.5, fail_grompp: bool = False) -> None:
        self.density = density
        self.fail_grompp = fail_grompp
        self.calls: list[list[str]] = []

    def run(self, args, *, cwd, **kwargs):
        self.calls.append(list(args))
        cwd = Path(cwd)
        sub = args[0]
        if sub == "solvate":
            edge = float(args[args.index("-box") + 1])
            n = round(self.density * edge ** 3)
            (cwd / args[args.index("-o") + 1]).write_text(gro_text(n, edge), encoding="utf-8")
        elif sub == "editconf":
            edge = float(args[args.index("-box") + 1])
            lines = (cwd / args[args.index("-f") + 1]).read_text(encoding="utf-8").splitlines()
            lines[-1] = "%10.5f%10.5f%10.5f" % (edge, edge, edge)
            (cwd / args[args.index("-o") + 1]).write_text("\n".join(lines) + "\n",
                                                          encoding="utf-8")
        elif sub == "grompp":
            if self.fail_grompp:
                return RunResult(1, "", "Fatal error: dry check failed")
            (cwd / args[args.index("-o") + 1]).write_text("tpr", encoding="utf-8")
        return RunResult(0, "", "")


def make_job(tmp_path):
    cfg = load_config(env={})
    cfg = replace(cfg, paths=replace(cfg.paths, runs_dir=tmp_path / "runs",
                                     sim_runs_dir=tmp_path / "sim"))
    store = JobStore(cfg, clock=lambda: datetime(2026, 10, 23, tzinfo=UTC))
    return store.create(REQUEST, "real", {"sha256": "a" * 64})


def test_number_density_and_edge():
    assert abs(number_density(0.997) - 33.33) < 0.01
    assert abs(initial_edge(1000, 0.997) - 3.107) < 0.002


def test_count_sol():
    assert count_sol(gro_text(7, 2.0)) == 7


def test_find_edge_returns_smallest_tested_edge_reaching_the_target():
    tested: dict[float, int] = {}

    def count_fn(edge: float) -> int:
        tested[edge] = round(33.5 * edge ** 3)
        return tested[edge]

    edge = find_edge(count_fn, 1000, 0.997)
    assert tested[edge] >= 1000
    assert edge == min(e for e, c in tested.items() if c >= 1000)
    assert len(tested) <= 12


def test_find_edge_raises_when_never_reached():
    with pytest.raises(SimAgentError) as exc:
        find_edge(lambda edge: 10, 1000, 0.997)
    assert exc.value.code == ErrorCode.E_BUILD_COUNT


def test_trim_gro_rewrites_header_and_keeps_whole_molecules():
    trimmed = trim_gro(gro_text(1010, 3.2), 1000)
    lines = trimmed.splitlines()
    assert lines[1] == "4000"
    assert len(lines) == 4000 + 3
    assert count_sol(trimmed) == 1000
    assert lines[-1].split()[0] == "3.20000"


def test_trim_gro_too_few_molecules():
    with pytest.raises(SimAgentError) as exc:
        trim_gro(gro_text(900, 3.0), 1000)
    assert exc.value.code == ErrorCode.E_BUILD_COUNT


def test_topology_text_exact():
    assert topology_text(1000, 3.107) == (
        '#include "oplsaa.ff/forcefield.itp"\n'
        '#include "oplsaa.ff/tip4p.itp"\n'
        "\n"
        "[ system ]\n"
        "1000 TIP4P water, box 3.1070 nm\n"
        "\n"
        "[ molecules ]\n"
        "SOL  1000\n")


def test_build_with_stub_runner(tmp_path):
    job = make_job(tmp_path)
    result = build_water_box(job, StubRunner(), n_molecules=1000, water_model="tip4p")
    build_dir = job.stage_dir("build")
    gro = (build_dir / "water.gro").read_text(encoding="utf-8")
    assert count_sol(gro) == 1000 and gro.splitlines()[1] == "4000"
    top = (build_dir / "topol.top").read_text(encoding="utf-8")
    assert "SOL  1000" in top
    info = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
    expected_density = 1000 * 18.01528 / (602.2140857 * info["box_nm"] ** 3)
    assert info["density_g_cm3"] == pytest.approx(expected_density, rel=1e-9)
    assert info["n_atoms"] == 4000 and info["n_molecules"] == 1000
    assert info["build_sha256"] == sha256_text(
        sha256_file(build_dir / "water.gro") + sha256_file(build_dir / "topol.top"))
    assert result["build_sha256"] == info["build_sha256"]
    for rel in ("01_build/water_raw.gro", "01_build/water.gro", "01_build/topol.top",
                "01_build/build.json"):
        assert job.ledger.has_receipt(rel, sha256_file(job.dir / rel))
    assert job.status("build") == "GATING"
    assert not (build_dir / "_dry").exists()  # scratch directory removed


def test_build_trims_surplus_molecules_before_editconf(tmp_path):
    job = make_job(tmp_path)
    runner = StubRunner(density=34.5)  # solvate yields more than 1000 molecules
    build_water_box(job, runner, n_molecules=1000, water_model="tip4p")
    raw = (job.stage_dir("build") / "water_raw.gro").read_text(encoding="utf-8")
    assert count_sol(raw) == 1000 and raw.splitlines()[1] == "4000"
    subs = [c[0] for c in runner.calls]
    assert subs.index("editconf") > max(i for i, s in enumerate(subs) if s == "solvate")


def test_dry_grompp_failure_marks_build_failed(tmp_path):
    job = make_job(tmp_path)
    with pytest.raises(SimAgentError) as exc:
        build_water_box(job, StubRunner(fail_grompp=True), n_molecules=1000,
                        water_model="tip4p")
    assert exc.value.code == ErrorCode.E_GROMPP_FAILED
    assert "dry check failed" in exc.value.message
    assert job.status("build") == "FAILED"


def test_build_after_plan_invalidates_and_clears_approval(tmp_path):
    job = make_job(tmp_path)
    build_water_box(job, StubRunner(), n_molecules=1000, water_model="tip4p")
    job.update(lambda d: d.update(plan={"stages": []}, plan_sha256="p" * 64,
                                  approval={"plan_sha256": "p" * 64}))
    job.update(lambda d: d["stage_status"].update({"build": "PASSED", "em": "PASSED"}))
    build_water_box(job, StubRunner(), n_molecules=1000, water_model="tip4p")
    data = job.read()
    assert data["approval"] is None
    assert data["stage_status"]["em"] == "PENDING"
    assert data["stage_status"]["build"] == "GATING"
    assert data["plan"] is not None  # the plan stays; its build hash no longer matches


def test_build_refused_while_running(tmp_path):
    job = make_job(tmp_path)
    job.update(lambda d: d["stage_status"].update({"nvt": "RUNNING"}))
    with pytest.raises(SimAgentError) as exc:
        build_water_box(job, StubRunner(), n_molecules=1000, water_model="tip4p")
    assert exc.value.code == ErrorCode.E_MDRUN_RUNNING
