"""The seeded two-leg ranking/memory probe, CI-sized (#759 M2a).

``scripts/probe_memory_parity.py`` builds random corpora on Redis (the Lua
oracle) and on Postgres and compares rankings, confidence, read tracking and
composite scores; the PR ran it at 600 shapes over three seeds. This runs a
small slice of it on every Postgres CI job: one seed, 40 shapes, any mismatch
a failure. It needs both servers -- Redis through the plugin's test database,
Postgres through the ``pg`` fixture's schema.
"""

import importlib.util
from pathlib import Path

PROBE = Path(__file__).resolve().parents[2] / "scripts" / "probe_memory_parity.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_memory_parity", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_a_seeded_probe_finds_no_undocumented_mismatch(pg):
    probe = _load_probe().run(pg, seeds=[759], shapes=40)
    assert probe.shapes == 40
    assert sum(probe.checks.values()) > 300
    assert not probe.mismatches, probe.report()
