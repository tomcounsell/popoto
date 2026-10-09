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
from popoto.redis_db import get_REDIS_DB, sibling_client_kwargs
from popoto.transfer.cli import main
from popoto.transfer.format import build_manifest, dump_line

# Backend conformance (#759 M5, plan §5 M5 gate (b)): every test in this
# module runs once per configured backend, and the `backend` fixture binds
# that leg's backend for the test, so the module-level models below run on
# Redis and on Postgres from the same test code. A test whose assertion only
# holds on Redis carries `redis_only` with the reason.
pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]

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


# ---------------------------------------------------------------------------
# Backend outages (#816) -- the one place this module stubs: an outage cannot
# be produced on demand against a live server, so the transfer function is
# replaced by one that raises it.
# ---------------------------------------------------------------------------


def _outage_cases():
    import redis

    from popoto.backends import BackendUnavailableError

    return [
        # These passed before #816 too: the old handler's ``OSError`` entry
        # caught ``BackendUnavailableError``, which subclasses the builtin
        # ``ConnectionError``. The ``redis-timeout`` cases are the regression
        # tests.
        pytest.param(
            BackendUnavailableError("postgres: connection refused"),
            id="backend-unavailable",
        ),
        # redis-py's TimeoutError is not the builtin one the handler also
        # names, so before #816 it escaped as a traceback.
        pytest.param(
            redis.exceptions.TimeoutError("read timed out"), id="redis-timeout"
        ),
    ]


def _raiser(exc):
    def _raise(*args, **kwargs):
        raise exc

    return _raise


@pytest.mark.parametrize("outage", _outage_cases())
def test_export_reports_an_outage_in_one_line(outage, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("popoto.transfer.export.export_records", _raiser(outage))
    out_path = tmp_path / "out.jsonl"

    exit_code = main(["export", "--model", MODEL_SPEC, "--out", str(out_path)])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert err == f"popoto-transfer export: {outage}\n"
    assert not out_path.exists()
    assert not (tmp_path / "out.jsonl.part").exists()


@pytest.mark.parametrize("outage", _outage_cases())
def test_import_reports_an_outage_in_one_line(outage, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("popoto.transfer.import_.import_records", _raiser(outage))
    in_path = tmp_path / "in.jsonl"
    in_path.write_text("")

    exit_code = main(["import", "--model", MODEL_SPEC, "--in", str(in_path)])

    assert exit_code == 1
    assert capsys.readouterr().err == f"popoto-transfer import: {outage}\n"


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
        get_REDIS_DB().connection_pool.connection_kwargs, db=db
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
    live_db = get_REDIS_DB().connection_pool.connection_kwargs.get("db", 0)
    assert live_db != 0, "test lane must run on a non-zero database"

    db0_kwargs = sibling_client_kwargs(
        get_REDIS_DB().connection_pool.connection_kwargs, db=0
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
    live_db = get_REDIS_DB().connection_pool.connection_kwargs.get("db", 0)
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


# ---------------------------------------------------------------------------
# The database-0 fence applies only to transfers that use Redis
# ---------------------------------------------------------------------------


@pytest.fixture
def fenced(monkeypatch):
    """Retarget the database-0 fence at the test lane's own database.

    The fence arms on every run without ``--allow-db0``; this only moves the
    database it fences to the test database, so a command the fence fails to
    stop still never reaches database 0. Any connection checkout to that
    database while armed trips it, whichever client the caller built.
    """
    from popoto.transfer import cli

    original_init = cli._Db0Fence.__init__

    def armed_init(self, allow_db0, verb):
        original_init(self, allow_db0, verb)
        self.fenced_db = self.db

    monkeypatch.setattr(cli._Db0Fence, "__init__", armed_init)


def test_db0_fence_refuses_only_a_redis_bound_transfer(
    fenced, backend, tmp_path, capsys
):
    """Redis on database 0 refuses a transfer that uses Redis, before any
    command, and lets a Postgres-bound transfer run with no Redis command at
    all: the fence would trip on the first connection checkout."""
    TransferCliItem.create(name="a", payload="x")
    out = tmp_path / "out.jsonl"

    code = main(["export", "--model", MODEL_SPEC, "--out", str(out)])
    err = capsys.readouterr().err

    if backend.name == "redis":
        assert code == 1, err
        assert "refusing to read from Redis database" in err
        assert "--allow-db0" in err
        assert "Traceback" not in err
        assert not out.exists()
    else:
        assert code == 0, err
        assert "refusing" not in err
        assert out.read_text().count("\n") >= 2  # manifest + one record

        TransferCliItem.query.get(name="a").delete()
        code = main(["import", "--model", MODEL_SPEC, "--in", str(out)])
        err = capsys.readouterr().err
        assert code == 0, err
        assert TransferCliItem.query.get(name="a").payload == "x"


def test_db0_fence_passes_with_allow_db0(fenced, tmp_path, capsys):
    TransferCliItem.create(name="a")
    out = tmp_path / "out.jsonl"
    code = main(["export", "--model", MODEL_SPEC, "--out", str(out), "--allow-db0"])
    assert code == 0, capsys.readouterr().err


@pytest.mark.redis_only(reason="fences the Redis pools directly")
def test_db0_fence_refuses_every_client_and_restores_the_pools():
    """Commands, pipelines, a client built after the fence went up, and an
    async client are all refused; a pool on another database is not; leaving
    the fence restores every pool class."""
    import asyncio

    import redis
    import redis.asyncio

    from popoto.transfer.cli import CLIError, _Db0Fence, _pool_classes

    originals = {cls: vars(cls)["get_connection"] for cls in _pool_classes()}
    live = get_REDIS_DB().connection_pool.connection_kwargs
    fence = _Db0Fence(allow_db0=False, verb="read from")
    fence.fenced_db = fence.db
    fence.active = True
    with fence:
        with pytest.raises(CLIError, match="--allow-db0"):
            get_REDIS_DB().ping()
        assert fence.tripped
        pipe = get_REDIS_DB().pipeline()
        pipe.get("x")
        with pytest.raises(CLIError):
            pipe.execute()
        rebound = redis.Redis(**sibling_client_kwargs(live, db=fence.db))
        with pytest.raises(CLIError):
            rebound.ping()

        async def async_ping():
            client = redis.asyncio.Redis(**sibling_client_kwargs(live, db=fence.db))
            try:
                await client.ping()
            finally:
                await client.aclose()

        with pytest.raises(CLIError):
            asyncio.run(async_ping())
        other_db = 14 if fence.db != 14 else 13
        assert redis.Redis(**sibling_client_kwargs(live, db=other_db)).ping()
    assert {cls: vars(cls)["get_connection"] for cls in originals} == originals
    assert get_REDIS_DB().ping()


def _model_module(tmp_path, monkeypatch, name, body):
    (tmp_path / f"{name}.py").write_text(body)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, name, raising=False)
    return f"{name}:TransferCliItem"


def test_db0_fence_refuses_after_a_trip_the_model_module_caught(
    fenced, backend, tmp_path, monkeypatch, capsys
):
    """A model module that touches Redis on import and swallows the refusal
    still gets the run refused, before any record is written."""
    if backend.name != "postgres":
        pytest.skip("a Redis-bound model is refused on resolution anyway")
    TransferCliItem.create(name="a", payload="x")
    out = tmp_path / "out.jsonl"
    assert (
        main(["export", "--model", MODEL_SPEC, "--out", str(out), "--allow-db0"]) == 0
    )
    TransferCliItem.query.get(name="a").delete()
    capsys.readouterr()

    spec = _model_module(
        tmp_path,
        monkeypatch,
        "db0_caught_trip_models",
        "import popoto\n"
        "try:\n"
        "    popoto.get_redis().ping()\n"
        "except Exception:\n"
        "    pass\n"
        "from tests.test_transfer_cli import TransferCliItem\n",
    )
    code = main(["import", "--model", spec, "--in", str(out)])
    err = capsys.readouterr().err
    assert code == 1, err
    assert "refusing to write to Redis database" in err
    assert "partway" not in err
    assert TransferCliItem.query.filter(name="a") == []


def test_db0_fence_refuses_a_client_the_model_module_builds(
    fenced, tmp_path, monkeypatch, capsys
):
    """A model module that builds its own client on the fenced database is
    refused too: the fence is on the pool classes, not one pool."""
    live = get_REDIS_DB().connection_pool.connection_kwargs
    kwargs = sibling_client_kwargs(live, db=live.get("db", 0))
    spec = _model_module(
        tmp_path,
        monkeypatch,
        "db0_own_client_models",
        "import redis\n"
        f"redis.Redis(**{kwargs!r}).set('db0-fence-probe', '1')\n"
        "from tests.test_transfer_cli import TransferCliItem\n",
    )
    code = main(["export", "--model", spec, "--out", str(tmp_path / "o.jsonl")])
    err = capsys.readouterr().err
    assert code == 1, err
    assert "refusing to read from Redis database" in err
    assert get_REDIS_DB().get("db0-fence-probe") is None


def test_postgres_only_process_on_db0_transfers_without_allow_db0(backend, tmp_path):
    """End to end, no test fence: a child process whose Redis binding is
    database 0 (``REDIS_URL=…/0``) and whose default backend is Postgres
    exports and imports without ``--allow-db0``. The real fence is armed in
    the child. The binding names a port nothing listens on, so exit 0 also
    proves the transfer never dialed Redis -- and this test never touches a
    real database 0, which on a developer machine may be a live store."""
    if backend.name != "postgres":
        pytest.skip("the Postgres-only deployment shape")
    from popoto.backends import get_backend

    pg = get_backend(TransferCliItem)
    TransferCliItem.create(name="a", payload="x")

    env = _child_env(db=0)
    env.update(
        REDIS_URL="redis://127.0.0.1:1/0",
        POPOTO_BACKEND="postgres",
        POPOTO_POSTGRES_URL=pg.dsn,
        POPOTO_POSTGRES_SCHEMA=pg.schema,
    )
    out = tmp_path / "out.jsonl"
    for argv in (
        ["export", "--model", MODEL_SPEC, "--out", str(out)],
        ["import", "--model", MODEL_SPEC, "--in", str(out), "--on-conflict", "skip"],
    ):
        result = subprocess.run(
            [sys.executable, "-m", "popoto.transfer.cli", *argv],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert "refusing" not in result.stderr

    assert out.read_text().count("\n") >= 2


# ---------------------------------------------------------------------------
# The fence arms on intent, not on the startup binding (#837)
# ---------------------------------------------------------------------------


@pytest.mark.redis_only(reason="the Redis leg is what the fence guards")
@pytest.mark.parametrize("verb", ["export", "import"])
def test_unfenced_redis_transfer_on_test_db_is_not_refused(verb, tmp_path, capsys):
    """Step-2 witness: with the fence armed on every run, a Redis transfer on
    the non-zero test database must still run. Without the live-binding
    conjunct on the post-resolution check this is refused."""
    assert get_REDIS_DB().connection_pool.connection_kwargs.get("db", 0) != 0
    TransferCliItem.create(name="a", payload="x")
    out = tmp_path / "out.jsonl"
    if verb == "export":
        argv = ["export", "--model", MODEL_SPEC, "--out", str(out)]
    else:
        assert main(["export", "--model", MODEL_SPEC, "--out", str(out)]) == 0
        TransferCliItem.query.get(name="a").delete()
        argv = ["import", "--model", MODEL_SPEC, "--in", str(out)]
    capsys.readouterr()
    code = main(argv)
    err = capsys.readouterr().err
    assert code == 0, err
    assert "refusing to" not in err


def test_db0_fence_arms_on_every_run_without_allow_db0():
    from popoto.transfer.cli import _Db0Fence

    assert get_REDIS_DB().connection_pool.connection_kwargs.get("db", 0) != 0
    assert _Db0Fence(False, "read from").active is True
    assert _Db0Fence(True, "read from").active is False


_REBIND_MODULE = """\
import popoto
from popoto.redis_db import set_REDIS_DB_settings

set_REDIS_DB_settings(host="127.0.0.1", port=1, db={target})


class RebindItem(popoto.Model):
    class Meta:
        backend = "redis"

    name = popoto.UniqueKeyField()
"""

_BACKEND_ENV = (
    "POPOTO_BACKEND",
    "POSTGRES_URL",
    "POPOTO_POSTGRES_URL",
    "POPOTO_POSTGRES_SCHEMA",
    "POPOTO_POSTGRES_LISTEN_URL",
    "POPOTO_POSTGRES_MAINTENANCE_URL",
)


def _rebind_child(tmp_path, startup_db, target_db, argv_for, redis_pinned=True):
    """Run the CLI in a child whose model module rebinds the global client to
    ``target_db`` on the dead port 127.0.0.1:1, so a fence miss is a
    connection error and never a write to a real database."""
    (tmp_path / "rebind_models.py").write_text(_REBIND_MODULE.format(target=target_db))
    env = _child_env(db=startup_db)
    env["REDIS_URL"] = f"redis://127.0.0.1:1/{startup_db}"
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), env["PYTHONPATH"]])
    if redis_pinned:
        for key in _BACKEND_ENV:
            env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-m", "popoto.transfer.cli", *argv_for],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.redis_only(reason="the rebound client is a Redis client")
@pytest.mark.parametrize("allow_db0", [False, True])
@pytest.mark.parametrize("verb", ["export", "import"])
def test_db0_fence_rebind_to_db0(verb, allow_db0, tmp_path):
    spec = "rebind_models:RebindItem"
    if verb == "export":
        argv = ["export", "--model", spec, "--out", str(tmp_path / "o.jsonl")]
    else:
        src = tmp_path / "in.jsonl"
        manifest = build_manifest(
            model_name="RebindItem",
            filter_repr=None,
            filter_kwargs={},
            matched_count=0,
            fields={},
            mixins={},
            embedding_provenance={},
        )
        src.write_text(dump_line(manifest))
        argv = ["import", "--model", spec, "--in", str(src)]
    if allow_db0:
        argv.append("--allow-db0")
    result = _rebind_child(tmp_path, startup_db=5, target_db=0, argv_for=argv)
    err = result.stderr
    assert "Traceback" not in err, err
    if allow_db0:
        assert "refusing to" not in err, err
    else:
        assert result.returncode == 1, err
        assert "refusing to" in err and "--allow-db0" in err, err
        assert "Redis database 0" in err, err
        assert "partway" not in err, err


@pytest.mark.redis_only(reason="the rebound client is a Redis client")
def test_db0_fence_allows_rebind_from_db0_to_nonzero(tmp_path):
    """Deliberate loosening: a run that starts on 0 but is rebound away from 0
    by the model module no longer touches database 0."""
    result = _rebind_child(
        tmp_path,
        startup_db=0,
        target_db=5,
        argv_for=[
            "export",
            "--model",
            "rebind_models:RebindItem",
            "--out",
            str(tmp_path / "o.jsonl"),
        ],
    )
    assert "refusing to" not in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def test_postgres_only_rebind_to_db0_runs_without_allow_db0(backend, tmp_path):
    if backend.name != "postgres":
        pytest.skip("the Postgres-only deployment shape")
    from popoto.backends import get_backend

    pg = get_backend(TransferCliItem)
    (tmp_path / "pg_rebind_models.py").write_text(
        "import popoto\n"
        "from popoto.redis_db import set_REDIS_DB_settings\n"
        'set_REDIS_DB_settings(host="127.0.0.1", port=1, db=0)\n'
        "\n\n"
        "class PgRebindItem(popoto.Model):\n"
        "    name = popoto.UniqueKeyField()\n"
    )
    env = _child_env(db=5)
    env.update(
        REDIS_URL="redis://127.0.0.1:1/5",
        POPOTO_BACKEND="postgres",
        POPOTO_POSTGRES_URL=pg.dsn,
        POPOTO_POSTGRES_SCHEMA=pg.schema,
    )
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), env["PYTHONPATH"]])
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "popoto.transfer.cli",
            "export",
            "--model",
            "pg_rebind_models:PgRebindItem",
            "--out",
            str(tmp_path / "o.jsonl"),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "refusing" not in result.stderr
