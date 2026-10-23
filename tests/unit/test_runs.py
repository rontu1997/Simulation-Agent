import pytest

from simagent.core.runs import (
    ProcSnapshot,
    RunFiles,
    decide_process_state,
    estimate_rate,
    parse_last_step,
    parse_performance,
)

NOW = 1000.0
WRAPPER_CMD = "python -m simagent.core.mdrun_wrapper /x/05_prod"


def run_json(**over):
    base = {"state": "running", "pid": 4242, "start_time_ticks": 777, "launched_at": NOW - 100}
    base.update(over)
    return base


def files(run=None, exit_code=None, cancelled=False, timeout=False):
    return RunFiles(run_json=run, has_exit=exit_code is not None, exit_code=exit_code,
                    cancelled=cancelled, timeout=timeout)


ALIVE = ProcSnapshot(alive=True, start_ticks=777, cmdline=WRAPPER_CMD)


@pytest.mark.parametrize("f,proc,expected", [
    (files(None), None, "not_started"),
    (files(run_json(state="launching", launched_at=NOW - 5)), None, "running"),
    (files(run_json(state="launching", launched_at=NOW - 40)), None, "died"),
    (files(run_json(), exit_code=0, cancelled=True), None, "cancelled"),
    (files(run_json(), exit_code=0), None, "finished"),
    (files(run_json(), exit_code=1), None, "died"),
    (files(run_json(), exit_code=-9), None, "died"),
    (files(run_json(), exit_code=0, timeout=True), None, "died"),
    (files(run_json(), timeout=True), ALIVE, "died"),
    (files(run_json()), ALIVE, "running"),
    (files(run_json()), ProcSnapshot(True, 999, WRAPPER_CMD), "died"),  # pid reused
    (files(run_json()), ProcSnapshot(True, 777, "python other.py"), "died"),
    (files(run_json()), ProcSnapshot(False, None, None), "died"),
    (files(run_json()), None, "died"),
    (files(run_json(start_time_ticks=None)), ProcSnapshot(True, None, None), "running"),
])
def test_process_state_table(f, proc, expected):
    assert decide_process_state(f, proc, NOW) == expected


def test_estimate_rate_from_two_samples_at_least_30_s_apart():
    rate, eta = estimate_rate([(0.0, 0.0), (60.0, 5.0)], remaining_ps=38.0)
    assert rate == pytest.approx(7.2)
    assert eta == pytest.approx(456.0)


def test_estimate_rate_uses_latest_sample_and_an_earlier_one_far_enough_back():
    samples = [(0.0, 0.0), (50.0, 4.0), (70.0, 5.6)]
    rate, eta = estimate_rate(samples, remaining_ps=10.0)
    # latest (70, 5.6) against the latest earlier sample at least 30 s back: (0, 0) is 70 s away,
    # (50, 4.0) is only 20 s away, so the pair is (0, 0) and (70, 5.6)
    assert rate == pytest.approx(5.6 / 70 * 86.4)
    assert eta == pytest.approx(10.0 / (5.6 / 70))


@pytest.mark.parametrize("samples", [[], [(0.0, 0.0)], [(0.0, 0.0), (10.0, 1.0)]])
def test_estimate_rate_needs_two_samples_far_enough_apart(samples):
    assert estimate_rate(samples, remaining_ps=10.0) == (None, None)


LOG = """           Step           Time
              0        0.00000

           Step           Time
          10000       20.00000

               Core t (s)   Wall t (s)        (%)
       Time:       99.0         12.4      800.0
                 (ns/day)    (hour/ns)
Performance:      139.375        0.172
Finished mdrun on rank 0 Fri Oct 23 12:00:00 2026
"""


def test_parse_last_step_takes_the_last_block():
    assert parse_last_step(LOG) == (10000, 20.0)
    assert parse_last_step("nothing here") is None


def test_parse_performance_reads_ns_per_day():
    assert parse_performance(LOG) == pytest.approx(139.375)
    assert parse_performance("no performance line") is None
