from types import SimpleNamespace

import pytest

from simagent.core.gmx import RunResult
from simagent.core.stage_checks import _series
from simagent.errors import ErrorCode, SimAgentError


class EnergyRunner:
    def __init__(self, unit: str, value: float):
        self.unit = unit
        self.value = value

    def run(self, args, **kwargs):
        kwargs["outputs"][0].write_text(
            f'@    yaxis  label "({self.unit})"\n'
            f"@ s0 legend \"Density\"\n0 {self.value}\n1 {self.value}\n",
            encoding="utf-8",
        )
        return RunResult(0, "", "")


@pytest.mark.parametrize(("unit", "value"), [("kg/m^3", 992.0), ("g/cm^3", 0.992)])
def test_density_series_is_in_g_per_cm3_for_the_gate(tmp_path, unit, value):
    job = SimpleNamespace(dir=tmp_path, stage_dir=lambda stage: tmp_path)
    times, density = _series(job, EnergyRunner(unit, value), "npt", "Density")
    assert times == [0.0, 1.0]
    assert density == pytest.approx([0.992, 0.992])


def test_density_series_rejects_unknown_units(tmp_path):
    job = SimpleNamespace(dir=tmp_path, stage_dir=lambda stage: tmp_path)
    with pytest.raises(SimAgentError) as exc:
        _series(job, EnergyRunner("unknown", 992.0), "npt", "Density")
    assert exc.value.code == ErrorCode.E_ANALYSIS_INPUT
