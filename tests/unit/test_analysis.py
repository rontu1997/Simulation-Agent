import math

import pytest

from simagent.analysis.stats import block_average, linear_drift
from simagent.analysis.xvg import parse_xvg

XVG = """# GROMACS energy
@    title "Temperature"
@    xaxis  label "Time (ps)"
@TYPE xy
@ s0 legend "Temperature"
@ s1 legend "Pressure"
    0.000000   300.1   1.5
    1.000000   299.9  -2.5
"""


def test_parse_xvg_skips_comments_and_reads_legends():
    legends, rows = parse_xvg(XVG)
    assert legends == ["Temperature", "Pressure"]
    assert rows == [[0.0, 300.1, 1.5], [1.0, 299.9, -2.5]]


def test_parse_xvg_empty_data():
    assert parse_xvg("# nothing\n@TYPE xy\n") == ([], [])


def test_block_average_constant_series():
    mean, sem = block_average([4.0] * 20, 5)
    assert mean == 4.0 and sem == 0.0


def test_block_average_known_series():
    mean, sem = block_average([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 5)
    assert mean == pytest.approx(5.5)
    # block means are 1.5, 3.5, 5.5, 7.5, 9.5: sample std 3.1623, sem = std / sqrt(5)
    assert sem == pytest.approx(math.sqrt(10) / math.sqrt(5))


def test_block_average_too_short_raises():
    with pytest.raises(ValueError):
        block_average([1.0, 2.0], 5)


def test_linear_drift_known_slope():
    times = [float(t) for t in range(0, 100)]
    slope, half_width = linear_drift(times, [3 * t + 2 for t in times])
    assert slope == pytest.approx(3.0) and half_width < 1e-9


def test_linear_drift_flat_noise_is_not_significant():
    times = [float(t) for t in range(200)]
    values = [300 + (1 if t % 2 else -1) * 0.5 for t in range(200)]
    slope, half_width = linear_drift(times, values)
    assert abs(slope) <= half_width
