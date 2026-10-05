"""The seeded two-leg validity/supersession probe, CI-sized (#759 M3).

``scripts/probe_validity_parity.py`` runs the same random sequences of saves,
supersedes, invalidations, direct ``execute_supersede`` calls,
``save_and_*`` and deletes on Redis (the ``SUPERSEDE_LUA`` oracle) and on
Postgres, and compares every return value and exception, the interval state,
links, pointers and chains, and every gated read at the interval ends,
``±inf``, ``1e308`` and NaN. The PR ran it at 600 shapes over three seeds;
this runs one seed, 25 shapes, on every Postgres CI job, any undocumented
mismatch a failure.
"""

import importlib.util
from pathlib import Path

PROBE = Path(__file__).resolve().parents[2] / "scripts" / "probe_validity_parity.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_validity_parity", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_a_seeded_probe_finds_no_undocumented_mismatch(pg):
    probe = _load_probe().run(pg, seeds=[759], shapes=25)
    assert probe.shapes == 25
    assert sum(probe.checks.values()) > 1000
    assert probe.checks["op"] > 100
    assert not probe.mismatches, probe.report()
