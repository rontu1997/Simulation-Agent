import json
from dataclasses import replace

import pytest

from simagent.config import load_config
from simagent.core.build import build_water_box, count_sol
from simagent.core.gmx import GmxRunner
from simagent.core.jobs import JobStore

REQUEST = {
    "system": {"kind": "water", "n_molecules": 1000, "water_model": "tip4p"},
    "force_field": "oplsaa",
    "thermo": {"temperature_K": 300.0, "pressure_bar": 1.0},
    "protocol": {"prod_ns": 1.0},
    "seed": 1,
}


@pytest.mark.real_gmx
def test_real_build_exactly_1000_sol(tmp_path):
    """T-INT-07: real solvate and a dry grompp give exactly 1,000 SOL and 4,000 atoms."""
    cfg = load_config()
    cfg = replace(cfg, paths=replace(cfg.paths, runs_dir=tmp_path / "runs"))
    job = JobStore(cfg).create(REQUEST, "real", {"sha256": "x"})
    build_water_box(job, GmxRunner(cfg, job.ledger), n_molecules=1000, water_model="tip4p")
    build_dir = job.stage_dir("build")
    gro = (build_dir / "water.gro").read_text(encoding="utf-8")
    assert count_sol(gro) == 1000 and gro.splitlines()[1] == "4000"
    assert "SOL  1000" in (build_dir / "topol.top").read_text(encoding="utf-8")
    assert json.loads((build_dir / "build.json").read_text(encoding="utf-8"))["n_atoms"] == 4000
