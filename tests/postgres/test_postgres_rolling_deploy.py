"""Rolling deploy across real processes (#776, PR #793 review).

Process A runs a long additive migration (the DDL takes ~8 s, longer than
``PG_DDL_LOCK_TIMEOUT_MS``) while the schema's DDL advisory lock is held.
Process B's first use of an unrelated model that is already current -- and
C's first use of a model that needs its own DDL -- must wait for A and then
succeed: the 5 s DDL lock timeout bounds DDL statements, not the wait for
the schema lock.
"""

import os
import subprocess
import sys
import textwrap
import time

import pytest

psycopg = pytest.importorskip("psycopg")

SLEEP_S = 8

SCRIPT = textwrap.dedent("""
    import sys, time
    import popoto
    from popoto.backends import _swap_instance, set_backend
    from popoto.backends.postgres import PostgresBackend
    from popoto.backends.postgres.schema import TableSpec

    role, dsn, schema = sys.argv[1:4]
    be = PostgresBackend(dsn=dsn, schema=schema)
    set_backend(be)
    _swap_instance("postgres", be)

    def model(name, extra=False):
        ns = {}
        exec(
            "import popoto\\n"
            f"class {name}(popoto.Model):\\n"
            "    name = popoto.KeyField()\\n"
            + ("    extra = popoto.Field(null=True)\\n" if extra else "")
            + "    class Meta:\\n        backend = 'postgres'\\n",
            ns,
        )
        return ns[name]

    t0 = time.monotonic()
    try:
        if role == "A":
            orig = TableSpec.companion_sql
            TableSpec.companion_sql = lambda self: list(orig(self)) + [
                "SELECT pg_sleep(%d)"
            ]
            model("RdSlow", extra=True)(name="a", extra="x").save()
        elif role == "B":
            model("RdCurrent")(name="b").save()
        else:
            model("RdFresh")(name="c").save()
        print("ok %%.2f" %% (time.monotonic() - t0))
    except BaseException as exc:
        print("%%s %%.2f" %% (type(exc).__name__, time.monotonic() - t0))
    """ % SLEEP_S)


def _model(name, extra=False):
    import popoto

    ns: dict = {}
    exec(  # noqa: S102 - a test-local class definition
        "import popoto\n"
        f"class {name}(popoto.Model):\n"
        "    name = popoto.KeyField()\n"
        + ("    extra = popoto.Field(null=True)\n" if extra else "")
        + "    class Meta:\n        backend = 'postgres'\n",
        ns,
    )
    return ns[name]


@pytest.mark.slow  # ~10 s: process A's migration holds the schema lock 8 s
def test_first_use_waits_for_another_process_long_migration(pg, admin):
    _model("RdSlow")(name="seed").save()
    _model("RdCurrent")(name="seed").save()
    pg.forget_tables()  # the subprocesses start cold anyway

    env = dict(os.environ, REDIS_URL="redis://localhost:6379/9")
    path = None

    def spawn(role):
        return subprocess.Popen(
            [sys.executable, "-c", SCRIPT, role, pg.dsn, pg.schema],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    a = spawn("A")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        row = admin.execute(
            "SELECT 1 FROM pg_stat_activity WHERE query LIKE %s AND pid <> "
            "pg_backend_pid()",
            (f"%pg_sleep({SLEEP_S})%",),
        ).fetchone()
        if row:
            break
        time.sleep(0.1)
    else:
        a.kill()
        pytest.fail("process A never reached its migration")
    b, c = spawn("B"), spawn("C")
    out = {}
    for name, proc in (("A", a), ("B", b), ("C", c)):
        stdout, stderr = proc.communicate(timeout=120)
        out[name] = stdout.strip()
        assert out[name].startswith("ok"), (name, stdout, stderr)
    # B and C waited out A's migration instead of failing at the 5 s limit.
    assert float(out["B"].split()[1]) > 4, out
    assert float(out["C"].split()[1]) > 4, out
