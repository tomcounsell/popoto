"""Tests for the ``popoto-transfer`` CLI (``src/popoto/transfer/cli.py``).

Everything here runs against live Redis, isolated on the pytest plugin's test
database (``POPOTO_TEST_DB``, non-zero). No mocks: ``main()`` is called
in-process for most cases, and a handful of subprocess tests exercise the
database-0 guard and the ``python -m`` entry point directly, per
``docs/plans/transfer_cli.md`` Step 3 and the "Failure Path Test Strategy"
section.

Conventions follow ``tests/test_integrations_cli.py``: assert on exit code,
on ``stdout``/``stderr`` separation via ``capsys``, and on the absence of the
string ``"Traceback"`` for every refusal path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager

import pytest

import popoto
from popoto.fields.write_filter import WriteFilterMixin
from popoto.redis_db import POPOTO_REDIS_DB, sibling_client_kwargs
from popoto.transfer.cli import main
from popoto.transfer.format import build_manifest, dump_line

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Fixture models -- module scope, resolvable as "tests.test_transfer_cli:Name"
# ---------------------------------------------------------------------------


class TransferCliItem(popoto.Model):
    """Plain model for round-trip, filter, conflict, and empty-input tests."""

    name = popoto.UniqueKeyField()
    payload = popoto.StringField(default="")


class TransferCliGateItem(WriteFilterMixin, popoto.Model):
    """Write-gated model used to produce a genuine REJECTED (exit 3) outcome.

    ``_wf_min_threshold`` is a plain class attribute so tests can raise it on
    the "destination" side with :func:`gate_threshold`, modelling a migration
    into a model whose write gate has since tightened -- the only reachable
    path to exit 3 without also raising ``ModelException`` mid-loop.
    """

    _wf_min_threshold = 0.1

    name = popoto.UniqueKeyField()
    importance = popoto.FloatField(default=0.0)

    def compute_filter_score(self):
        return self.importance or 0.0


@contextmanager
def gate_threshold(model_class, threshold):
    """Temporarily raise a model's write-gate minimum threshold."""
    missing = object()
    previous = model_class.__dict__.get("_wf_min_threshold", missing)
    model_class._wf_min_threshold = threshold
    try:
        yield
    finally:
        if previous is missing:
            delattr(model_class, "_wf_min_threshold")
        else:
            model_class._wf_min_threshold = previous


MODEL_SPEC = "tests.test_transfer_cli:TransferCliItem"
GATE_MODEL_SPEC = "tests.test_transfer_cli:TransferCliGateItem"


def wipe(model_class):
    for instance in model_class.query.all():
        instance.delete()
    assert model_class.query.all() == []


@pytest.fixture(autouse=True)
def _clean_models():
    wipe(TransferCliItem)
    wipe(TransferCliGateItem)
    yield
    wipe(TransferCliItem)
    wipe(TransferCliGateItem)


# ---------------------------------------------------------------------------
# --help / basic smoke
# ---------------------------------------------------------------------------


def test_console_script_help():
    """``main(["--help"])`` exits 0 via argparse's own SystemExit."""
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0


def test_no_subcommand_prints_help_and_exits_zero(capsys):
    exit_code = main([])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "usage" in out.lower()


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_round_trip_export_then_import(tmp_path, capsys):
    for i in range(5):
        TransferCliItem(name=f"item{i}", payload=f"payload-{i}").save()
    out_path = tmp_path / "export.jsonl"

    exit_code = main(["export", "--model", MODEL_SPEC, "--out", str(out_path)])
    assert exit_code == 0
    capsys.readouterr()

    wipe(TransferCliItem)
    assert TransferCliItem.query.all() == []

    exit_code = main(
        [
            "import",
            "--model",
            MODEL_SPEC,
            "--in",
            str(out_path),
            "--on-conflict",
            "overwrite",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr()
    assert "Traceback" not in out.err
    assert "Traceback" not in out.out

    landed = {obj.name: obj.payload for obj in TransferCliItem.query.all()}
    expected = {f"item{i}": f"payload-{i}" for i in range(5)}
    assert landed == expected


# ---------------------------------------------------------------------------
# --filter
# ---------------------------------------------------------------------------


def test_filter_narrows_export_and_summary_names_it(tmp_path, capsys):
    TransferCliItem(name="keep1", payload="ai").save()
    TransferCliItem(name="keep2", payload="ai").save()
    TransferCliItem(name="drop1", payload="other").save()
    out_path = tmp_path / "filtered.jsonl"

    exit_code = main(
        [
            "export",
            "--model",
            MODEL_SPEC,
            "--filter",
            "payload=ai",
            "--out",
            str(out_path),
        ]
    )
    assert exit_code == 0
    err = capsys.readouterr().err
    assert "filter:" in err
    assert "payload" in err
    assert "ai" in err

    lines = [line for line in out_path.read_text().splitlines() if line.strip()]
    # first line is the manifest; the rest are records
    assert len(lines) - 1 == 2
    for line in lines[1:]:
        record = json.loads(line)
        assert record["values"]["payload"] == "ai"


# ---------------------------------------------------------------------------
# --json
# ---------------------------------------------------------------------------


def test_json_counts_sum_to_records_read(tmp_path, capsys):
    for i in range(4):
        TransferCliItem(name=f"jitem{i}", payload="x").save()
    out_path = tmp_path / "export.jsonl"
    main(["export", "--model", MODEL_SPEC, "--out", str(out_path)])
    capsys.readouterr()
    wipe(TransferCliItem)

    exit_code = main(
        [
            "import",
            "--model",
            MODEL_SPEC,
            "--in",
            str(out_path),
            "--json",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert sum(payload["counts"].values()) == 4
    assert payload["counts"]["landed"] == 4


# ---------------------------------------------------------------------------
# --out -
# ---------------------------------------------------------------------------


def test_out_dash_streams_jsonl_on_stdout_summary_on_stderr(capsys):
    for i in range(3):
        TransferCliItem(name=f"stditem{i}", payload="y").save()

    exit_code = main(["export", "--model", MODEL_SPEC, "--out", "-"])
    assert exit_code == 0
    out = capsys.readouterr()

    lines = [line for line in out.out.splitlines() if line.strip()]
    assert len(lines) - 1 == 3
    manifest = json.loads(lines[0])
    assert manifest["model"] == "TransferCliItem"

    assert "ExportResult for TransferCliItem" in out.err
    assert "matched:" in out.err
    # stdout must carry ONLY jsonl -- no summary text leaking in
    assert "ExportResult" not in out.out


def test_out_dash_and_json_together_exits_one(capsys):
    exit_code = main(["export", "--model", MODEL_SPEC, "--out", "-", "--json"])
    assert exit_code == 1
    out = capsys.readouterr()
    assert "Traceback" not in out.err
    assert "Traceback" not in out.out


# ---------------------------------------------------------------------------
# Exit 3: write-gate rejection (NOT an on-conflict=error collision)
# ---------------------------------------------------------------------------


def test_write_gate_rejection_exits_three(tmp_path, capsys):
    """Records saved on the source with a low importance clear the source's
    gate but are refused by a tightened destination gate -- REJECTED, not an
    exception. This is the only reachable path to exit 3 per the plan's
    library-path-to-exit-code table.
    """
    # 0.3 clears the source's default gate (_wf_min_threshold=0.1) so both
    # records land and export, but is refused once the destination's gate is
    # raised to 0.5 below.
    TransferCliGateItem(name="low1", importance=0.3).save()
    TransferCliGateItem(name="low2", importance=0.3).save()
    out_path = tmp_path / "gate.jsonl"

    exit_code = main(["export", "--model", GATE_MODEL_SPEC, "--out", str(out_path)])
    assert exit_code == 0
    capsys.readouterr()
    wipe(TransferCliGateItem)

    with gate_threshold(TransferCliGateItem, 0.5):
        exit_code = main(["import", "--model", GATE_MODEL_SPEC, "--in", str(out_path)])

    assert exit_code == 3
    out = capsys.readouterr()
    assert "Traceback" not in out.err
    assert "rejected" in out.err.lower()
    assert TransferCliGateItem.query.all() == []


# ---------------------------------------------------------------------------
# Exit 1: on-conflict=error collision (raises mid-loop, NOT exit 3)
# ---------------------------------------------------------------------------


def test_on_conflict_error_collision_exits_one(tmp_path, capsys):
    TransferCliItem(name="dup", payload="source-value").save()
    out_path = tmp_path / "collide.jsonl"
    exit_code = main(["export", "--model", MODEL_SPEC, "--out", str(out_path)])
    assert exit_code == 0
    capsys.readouterr()

    # Do NOT wipe -- the destination already holds "dup", so this is a
    # collision under the default on_conflict="error".
    exit_code = main(["import", "--model", MODEL_SPEC, "--in", str(out_path)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "already written" in err
    assert "on_conflict='overwrite'" in err


# ---------------------------------------------------------------------------
# --on-conflict skip: deliberate skip is not a failure to land
# ---------------------------------------------------------------------------


def test_on_conflict_skip_exits_zero(tmp_path, capsys):
    TransferCliItem(name="skipme", payload="source-value").save()
    out_path = tmp_path / "skip.jsonl"
    exit_code = main(["export", "--model", MODEL_SPEC, "--out", str(out_path)])
    assert exit_code == 0
    capsys.readouterr()

    # destination already holds "skipme" -- collision, but skip mode.
    exit_code = main(
        [
            "import",
            "--model",
            MODEL_SPEC,
            "--in",
            str(out_path),
            "--on-conflict",
            "skip",
        ]
    )

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "skipped:      1" in err or "skipped" in err.lower()
    obj = TransferCliItem.query.get(name="skipme")
    assert obj.payload == "source-value"


# ---------------------------------------------------------------------------
# Empty inputs
# ---------------------------------------------------------------------------


def test_zero_byte_import_file_exits_one_no_traceback(tmp_path, capsys):
    empty_path = tmp_path / "empty.jsonl"
    empty_path.write_text("")

    exit_code = main(["import", "--model", MODEL_SPEC, "--in", str(empty_path)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "manifest" in err.lower()


def test_manifest_only_import_file_is_a_valid_zero_record_run(tmp_path, capsys):
    manifest = build_manifest(
        model_name="TransferCliItem",
        filter_repr=None,
        filter_kwargs={},
        matched_count=0,
        fields={},
        mixins={},
        embedding_provenance={},
    )
    path = tmp_path / "manifest_only.jsonl"
    path.write_text(dump_line(manifest))

    exit_code = main(["import", "--model", MODEL_SPEC, "--in", str(path)])

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "records read:  0" in err


def test_zero_match_export_is_a_valid_run(tmp_path, capsys):
    TransferCliItem(name="present", payload="something").save()
    out_path = tmp_path / "zero.jsonl"

    # "name" is the (indexed) UniqueKeyField, so an unmatched value resolves
    # to zero keys directly rather than going through the client-side filter
    # path that "payload" (an unindexed field) would take.
    exit_code = main(
        [
            "export",
            "--model",
            MODEL_SPEC,
            "--filter",
            "name=nonexistent-value-xyz",
            "--out",
            str(out_path),
        ]
    )

    assert exit_code == 0
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "matched:       0" in err
    assert "written:       0" in err
    lines = [line for line in out_path.read_text().splitlines() if line.strip()]
    assert len(lines) == 1  # manifest only


# ---------------------------------------------------------------------------
# Model resolution failures -- distinct message each
# ---------------------------------------------------------------------------


def test_resolution_no_colon_exits_one(capsys):
    exit_code = main(["export", "--model", "no_colon_here", "--out", "-"])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "one colon" in err


def test_resolution_empty_half_exits_one(capsys):
    exit_code = main(["export", "--model", ":ClassName", "--out", "-"])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "must name both" in err


def test_resolution_empty_class_half_exits_one(capsys):
    exit_code = main(["export", "--model", "some.module:", "--out", "-"])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "must name both" in err


def test_resolution_missing_module_exits_one(capsys):
    exit_code = main(
        ["export", "--model", "definitely_not_a_real_module_xyz:Thing", "--out", "-"]
    )
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "could not import module" in err
    assert "definitely_not_a_real_module_xyz" in err


def test_resolution_missing_attribute_exits_one(capsys):
    exit_code = main(
        ["export", "--model", "os:DefinitelyNotAnAttributeXyz", "--out", "-"]
    )
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "no attribute" in err
    assert "DefinitelyNotAnAttributeXyz" in err


def test_resolution_attribute_not_a_model_exits_one(capsys):
    # collections.OrderedDict is a real class, but not a Popoto Model.
    exit_code = main(["export", "--model", "collections:OrderedDict", "--out", "-"])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "not a Popoto Model" in err


def test_resolution_via_cwd_insertion(tmp_path, monkeypatch, capsys):
    """A helper module written to ``tmp_path`` resolves once CWD is on
    ``sys.path`` -- proving the console-script CWD gap fix (Technical
    Approach, resolve_model step 1).
    """
    module_file = tmp_path / "transfer_cli_helper_mod.py"
    module_file.write_text(
        "import popoto\n\n\n"
        "class HelperModel(popoto.Model):\n"
        "    name = popoto.UniqueKeyField()\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        exit_code = main(
            [
                "export",
                "--model",
                "transfer_cli_helper_mod:HelperModel",
                "--out",
                "-",
            ]
        )
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "Traceback" not in err
    finally:
        sys.modules.pop("transfer_cli_helper_mod", None)


# ---------------------------------------------------------------------------
# Failed export preserves the destination
# ---------------------------------------------------------------------------


def test_failed_export_preserves_pre_existing_destination(tmp_path, capsys):
    out_path = tmp_path / "existing.jsonl"
    sentinel = "this file must survive a failed export untouched\n"
    out_path.write_bytes(sentinel.encode())

    exit_code = main(
        [
            "export",
            "--model",
            MODEL_SPEC,
            "--filter",
            "no_such_field_xyz=1",
            "--out",
            str(out_path),
        ]
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert out_path.read_bytes() == sentinel.encode()

    part_path = tmp_path / "existing.jsonl.part"
    assert not part_path.exists()
    remaining = list(tmp_path.iterdir())
    assert all(not str(p).endswith(".part") for p in remaining)


# ---------------------------------------------------------------------------
# Subprocess tests
# ---------------------------------------------------------------------------


def _child_env(db: int) -> dict:
    kwargs = sibling_client_kwargs(
        POPOTO_REDIS_DB.connection_pool.connection_kwargs, db=db
    )
    host = kwargs.get("host", "localhost")
    port = kwargs.get("port", 6379)
    env = dict(os.environ)
    env["REDIS_URL"] = f"redis://{host}:{port}/{db}"
    src_dir = os.path.join(REPO_ROOT, "src")
    existing_pp = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        [REPO_ROOT, src_dir] + ([existing_pp] if existing_pp else [])
    )
    return env


def test_db0_refusal_via_subprocess_does_not_touch_db0():
    live_db = POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db", 0)
    assert live_db != 0, "test lane must run on a non-zero database"

    db0_kwargs = sibling_client_kwargs(
        POPOTO_REDIS_DB.connection_pool.connection_kwargs, db=0
    )
    import redis as _redis

    db0_client = _redis.Redis(**db0_kwargs)
    size_before = db0_client.dbsize()

    env = _child_env(db=0)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "popoto.transfer.cli",
            "export",
            "--model",
            "popoto:Model",
            "--out",
            "-",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 1, result.stderr
    assert "--allow-db0" in result.stderr
    assert "Traceback" not in result.stderr

    size_after = db0_client.dbsize()
    assert size_after == size_before


def test_help_via_subprocess_module_invocation():
    live_db = POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db", 0)
    assert live_db != 0, "test lane must run on a non-zero database"

    env = _child_env(db=live_db)
    result = subprocess.run(
        [sys.executable, "-m", "popoto.transfer.cli", "--help"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()
