import pytest

from simagent.config import load_config
from simagent.core.envcheck import check_environment


@pytest.mark.real_gmx
def test_real_environment_check_passes():
    """T-INT-05: the real binary passes every check including the probe run."""
    result = check_environment(load_config())
    assert result["execution_mode"] == "real"
    assert result["version"].startswith("2025.3")
    assert result["probe"]["ok"] is True
    assert result["has_oplsaa"] and result["has_tip4p_itp"] and result["has_tip4p_gro"]
