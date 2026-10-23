from simagent.config import GatesConfig
from simagent.core.gates import (
    GateCheck,
    common_checks,
    gate_analysis,
    gate_build,
    gate_em,
    gate_npt,
    gate_nvt,
    gate_prod,
    gate_result,
)

G = GatesConfig()
LOG = "Steepest Descents converged to Fmax < 1000 in 120 steps\nFinished mdrun on rank 0\n"
TIMES = [float(t) for t in range(0, 101)]  # 0..100 ps


def by_name(checks):
    return {c.name: c for c in checks}


def flat(value, n=101):
    return [value] * n


def ramp(start, per_ps):
    return [start + per_ps * t for t in TIMES]


def test_gate_em():
    ok = by_name(gate_em(LOG, 1000.0, 800.0, -41.0, G))
    assert ok["converged"].status == "pass" and ok["epot_finite_negative"].status == "pass"
    assert ok["epot_range"].status == "pass"
    bad = by_name(gate_em("did not converge", 1000.0, 5000.0, -41.0, G))
    assert bad["converged"].status == "fail"
    assert by_name(gate_em(LOG, 1000.0, 800.0, -50.0, G))["epot_range"].status == "warn"
    assert by_name(gate_em(LOG, 1000.0, 800.0, float("nan"), G))[
        "epot_finite_negative"].status == "fail"
    assert by_name(gate_em(LOG, 1000.0, 800.0, 5.0, G))["epot_finite_negative"].status == "fail"


def test_gate_nvt():
    ok = by_name(gate_nvt(LOG, flat(300.5), TIMES, 300.0, G))
    assert ok["completion_marker"].status == "pass" and ok["mean_temperature"].status == "pass"
    assert ok["temperature_drift"].status == "pass"
    assert by_name(gate_nvt(LOG, flat(303.0), TIMES, 300.0, G))["mean_temperature"].status == "fail"
    assert by_name(gate_nvt(LOG, ramp(300.0, 0.007), TIMES, 300.0, G))[
        "temperature_drift"].status == "warn"
    assert by_name(gate_nvt(LOG, ramp(300.0, 0.012), TIMES, 300.0, G))[
        "temperature_drift"].status == "fail"
    assert by_name(gate_nvt("no marker", flat(300.0), TIMES, 300.0, G))[
        "completion_marker"].status == "fail"


def npt(density=0.998, temp=300.0, pressure=1.0, ref_t=300.0, ref_p=1.0, times=TIMES):
    n = len(times)
    return gate_npt(LOG, flat(temp, n), flat(density, n), flat(pressure, n), times, ref_t, ref_p, G)


def test_gate_npt():
    assert by_name(npt())["mean_density"].status == "pass"
    assert by_name(npt(density=0.95))["mean_density"].status == "warn"
    assert by_name(npt(density=0.89))["mean_density"].status == "fail"
    assert by_name(npt(temp=303.0))["mean_temperature"].status == "fail"
    assert by_name(npt(pressure=400.0))["mean_pressure"].status == "warn"
    assert by_name(npt(pressure=1.0))["mean_pressure"].status == "pass"


def test_density_bands_only_near_ambient():
    checks = by_name(npt(density=0.80, ref_t=400.0, temp=400.0))
    assert checks["mean_density"].status == "warn"
    assert checks["mean_density"].threshold is None


def prod(frames=2001, last=1000.0, temp=300.0, density=0.998, drift=0.001):
    return gate_prod(
        LOG, nsteps=500000, dt=0.002, interval_ps=0.5, n_frames=frames, last_frame_ps=last,
        temps=flat(temp), density=flat(density), ref_t=300.0, conserved_drift=drift, gates=G)


def test_gate_prod():
    ok = by_name(prod())
    assert all(c.status == "pass" for c in ok.values())
    assert by_name(prod(frames=2000))["frame_count"].status == "fail"
    assert by_name(prod(last=998.0))["trajectory_length"].status == "fail"
    assert by_name(prod(temp=301.5))["mean_temperature"].status == "fail"
    assert by_name(prod(drift=0.01))["conserved_drift"].status == "warn"
    assert by_name(prod(density=0.95))["mean_density"].status == "warn"
    assert by_name(prod(density=0.85))["mean_density"].status == "fail"


def build(atoms=4000, hash_ok=True, mol_top=1000, info_ok=True, dry_ok=True, density=0.997):
    return gate_build(hash_matches_plan=hash_ok, atoms_in_gro=atoms, n_molecules=1000,
                      molecules_in_topology=mol_top, build_info_matches_plan=info_ok,
                      dry_grompp_ok=dry_ok, density=density, gates=G)


def test_gate_build():
    assert gate_result(build())
    assert not gate_result(build(atoms=3999))
    assert not gate_result(build(hash_ok=False))
    assert not gate_result(build(mol_top=999))
    assert not gate_result(build(dry_ok=False))
    assert by_name(build(density=0.97))["density"].status == "pass"
    assert by_name(build(density=0.95))["density"].status == "warn"
    assert by_name(build(density=0.80))["density"].status == "fail"


def test_gate_analysis():
    ok = gate_analysis(["rdf_OO", "temperature"], {"rdf_OO": True, "temperature": True},
                       nan_found=False, summary_valid=True)
    assert gate_result(ok)
    missing = gate_analysis(["rdf_OO"], {"rdf_OO": False}, nan_found=False, summary_valid=True)
    assert by_name(missing)["output_rdf_OO"].status == "fail"
    assert not gate_result(gate_analysis([], {}, nan_found=True, summary_valid=True))
    assert not gate_result(gate_analysis([], {}, nan_found=False, summary_valid=False))


def test_all_checks_receipt_and_process():
    good = by_name(common_checks("finished", receipts_ok=True, nan_found=False,
                                 unacknowledged_warnings=0))
    assert all(c.status == "pass" for c in good.values())
    died = by_name(common_checks("died", True, False, 0))
    assert died["process_died"].status == "fail"
    assert by_name(common_checks("timeout", True, False, 0))["timeout"].status == "fail"
    assert by_name(common_checks("finished", False, False, 0))["receipts"].status == "no_receipt"
    assert by_name(common_checks(None, None, None, 0))["process_state"].status == "not_run"
    assert by_name(common_checks("finished", True, True, 0))["no_nan"].status == "fail"
    assert by_name(common_checks("finished", True, False, 2))["grompp_warnings"].status == "fail"


def test_simulated_profile_skips_physics_but_keeps_structure():
    checks = by_name(gate_em(LOG, 1000.0, 800.0, -41.0, G, simulated=True))
    assert checks["converged"].status == "skipped_simulated"
    assert checks["epot_range"].status == "skipped_simulated"
    structural = by_name(gate_build(
        hash_matches_plan=True, atoms_in_gro=3999, n_molecules=1000, molecules_in_topology=1000,
        build_info_matches_plan=True, dry_grompp_ok=True, density=0.5, gates=G, simulated=True))
    assert structural["atoms_per_molecule"].status == "fail"
    assert structural["density"].status == "skipped_simulated"
    assert gate_result(gate_nvt(LOG, flat(500.0), TIMES, 300.0, G, simulated=True))


def test_gate_result_rule():
    pass_c, warn_c = GateCheck("a", "pass"), GateCheck("b", "warn")
    assert gate_result([pass_c, warn_c, GateCheck("c", "skipped_simulated")])
    for bad in ("fail", "no_receipt", "not_run"):
        assert not gate_result([pass_c, GateCheck("x", bad)])
