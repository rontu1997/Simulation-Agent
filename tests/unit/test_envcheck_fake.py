import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

FAKE = Path(__file__).resolve().parents[1] / "fakes" / "fake_gmx"


def run_fake(*args, cwd, env=None):
    return subprocess.run([sys.executable, str(FAKE), *args], cwd=cwd, capture_output=True,
                          text=True, env={**os.environ, **(env or {})})


def test_fake_version(tmp_path):
    out = run_fake("--version", cwd=tmp_path)
    assert out.returncode == 0 and "GROMACS version:" in out.stdout and "FAKE-0.0" in out.stdout


def test_fake_solvate_writes_valid_gro(tmp_path):
    out = run_fake("solvate", "-cs", "tip4p.gro", "-box", "3.1", "3.1", "3.1", "-o", "w.gro",
                   cwd=tmp_path)
    assert out.returncode == 0
    lines = (tmp_path / "w.gro").read_text(encoding="utf-8").splitlines()
    n_atoms = int(lines[1])
    assert n_atoms == 4 * round(33.3 * 3.1 ** 3)
    assert len(lines) == n_atoms + 3
    assert n_atoms % 4 == 0 and "SOL" in lines[2]
    assert [float(x) for x in lines[-1].split()] == [3.1, 3.1, 3.1]


def test_fake_editconf_sets_box(tmp_path):
    run_fake("solvate", "-box", "2.0", "2.0", "2.0", "-o", "w.gro", cwd=tmp_path)
    out = run_fake("editconf", "-f", "w.gro", "-o", "e.gro", "-box", "2.5", "2.5", "2.5", "-c",
                   cwd=tmp_path)
    assert out.returncode == 0
    last = (tmp_path / "e.gro").read_text(encoding="utf-8").splitlines()[-1].split()
    assert [float(x) for x in last] == [2.5, 2.5, 2.5]


def test_fake_mdrun_outputs_marker_and_finish_line(tmp_path):
    out = run_fake("mdrun", "-deffnm", "em", cwd=tmp_path)
    assert out.returncode == 0
    assert "Finished mdrun on rank 0" in (tmp_path / "em.log").read_text(encoding="utf-8")
    assert (tmp_path / "em.edr").read_text(encoding="utf-8").startswith("FAKE-GMX OUTPUT")


def test_fake_grompp_warnings_need_maxwarn(tmp_path):
    base = ("grompp", "-f", "a.mdp", "-c", "b.gro", "-p", "t.top", "-o", "x.tpr")
    env = {"FAKE_GMX_WARNINGS": "2"}
    assert run_fake(*base, cwd=tmp_path, env=env).returncode != 0
    assert run_fake(*base, "-maxwarn", "1", cwd=tmp_path, env=env).returncode != 0
    assert run_fake(*base, "-maxwarn", "2", cwd=tmp_path, env=env).returncode == 0
    bad = run_fake(*base, cwd=tmp_path, env=env)
    assert "WARNING 1 [file" in bad.stderr and "There were 2 warnings" in bad.stderr


@pytest.mark.posix
def test_fake_is_executable_through_shebang(tmp_path):
    exe = tmp_path / "gmx"
    shutil.copy(FAKE, exe)
    exe.chmod(0o755)
    out = subprocess.run([str(exe), "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and "FAKE-0.0" in out.stdout
