"""Guard: no test may flush a Redis database it picked by literal number (#806).

``tests/test_db0_flush_guard.py`` once built a client on ``db=4`` and ran a real
``FLUSHDB``. It ignored ``POPOTO_TEST_DB`` and the pytest plugin's isolation, so
every full-suite run wiped DB 4 on a machine shared with other lanes (a #801
probe lost 30300 of 60000 records; #791 saw DBs 4 and 12 emptied). A test may
flush only the database the plugin bound it to -- obtain it with
``popoto.get_redis()`` / ``POPOTO_TEST_DB``, never a literal.

The check is static and deliberately coarse. Per function (or module top level),
it flags a scope that contains *both*

* a client/pool built with a literal database: ``db=<int>`` keyword on a call
  (``ConnectionPool``, ``Redis``, ``GuardedRedis``, ``set_REDIS_DB_settings``...)
  or a ``from_url("redis://host:port/<n>")`` literal; and
* a flush: ``.flushdb(`` / ``.flushall(`` or ``"FLUSHDB"``/``"FLUSHALL"`` passed
  to ``execute_command``.

Not covered: a database number reaching the client through a variable or a
constant defined elsewhere, or a flush issued from a different function than the
construction. Those need a runtime check; this one catches the shape that
caused #806.

``ALLOWLIST`` names the never-dispatch refusal tests: they build a client on a
literal db only to prove the guard *refuses*, and monkeypatch the transport so
no command can reach a server. Adding to it needs the same justification.
"""

import ast
import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent

# (file relative to tests/, enclosing function qualname) -- never-dispatch tests.
ALLOWLIST = {
    (
        "test_db0_flush_guard.py",
        "TestRealDb4Client.test_flushall_refused_on_db4_client_never_reaches_server",
    ),
    (
        "test_db0_flush_guard.py",
        "TestAsyncDb0Refusal.test_async_flushdb_refused_never_reaches_server",
    ),
    (
        "test_db0_flush_guard.py",
        "TestAsyncDb0Refusal.test_async_pipeline_flushall_refused_at_queue_time",
    ),
}

_FLUSH_ATTRS = {"flushdb", "flushall"}
_FLUSH_NAMES = {"FLUSHDB", "FLUSHALL"}
_URL_DB = re.compile(r"^rediss?://[^/]*/\d+$")


def _is_literal_db_call(node):
    if not isinstance(node, ast.Call):
        return False
    for kw in node.keywords:
        if (
            kw.arg == "db"
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, int)
        ):
            return True
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name == "from_url" and node.args:
        arg = node.args[0]
        return (
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and bool(_URL_DB.match(arg.value))
        )
    return False


def _is_flush(node):
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute) and node.func.attr in _FLUSH_ATTRS:
        return True
    for a in node.args:
        if isinstance(a, ast.Constant) and isinstance(a.value, (str, bytes)):
            text = (
                a.value.decode("utf-8", "replace")
                if isinstance(a.value, bytes)
                else a.value
            )
            if text.upper() in _FLUSH_NAMES:
                return True
    return False


def _scopes(tree):
    """Yield (qualname, nodes directly in that scope, excluding nested defs)."""

    def walk(node, prefix):
        own, nested = [], []
        stack = list(ast.iter_child_nodes(node))
        while stack:
            child = stack.pop()
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                nested.append(child)
                continue
            own.append(child)
            stack.extend(ast.iter_child_nodes(child))
        yield prefix, own
        for child in nested:
            qual = f"{prefix}.{child.name}" if prefix else child.name
            yield from walk(child, qual)

    yield from walk(tree, "")


def find_violations(source, filename="<src>"):
    """Return ``[(qualname, lineno)]`` of scopes pairing a literal db with a flush."""
    tree = ast.parse(source, filename)
    found = []
    for qual, nodes in _scopes(tree):
        flushes = sorted(n.lineno for n in nodes if _is_flush(n))
        literals = [n for n in nodes if _is_literal_db_call(n)]
        if flushes and literals:
            found.append((qual or "<module>", flushes[0]))
    return found


def test_no_test_flushes_a_literal_database():
    offenders = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        rel = path.relative_to(TESTS_DIR).as_posix()
        for qual, lineno in find_violations(path.read_text(), rel):
            if (rel, qual) in ALLOWLIST:
                continue
            offenders.append(f"{rel}:{lineno} ({qual})")
    assert not offenders, (
        "A test builds a Redis client on a literal db number and flushes it. "
        "Other lanes share this machine's databases, so that wipes their data "
        "(#806). Flush only the plugin's isolated db -- "
        "popoto.get_redis().connection_pool.connection_kwargs['db'] -- or add a "
        "never-dispatch refusal test to ALLOWLIST with a justification:\n  "
        + "\n  ".join(offenders)
    )


def test_scanner_flags_the_806_shape():
    bad = (
        "def test_x():\n"
        "    pool = redis.ConnectionPool(host='h', port=6379, db=4)\n"
        "    GuardedRedis(connection_pool=pool).flushdb()\n"
    )
    assert find_violations(bad) == [("test_x", 3)]
    url = "def t():\n    r = redis.from_url('redis://h:6379/12')\n    r.flushall()\n"
    assert find_violations(url) == [("t", 3)]
    raw = "def t():\n    r = redis.Redis(db=3)\n    r.execute_command('FLUSHDB')\n"
    assert find_violations(raw) == [("t", 3)]


def test_scanner_ignores_bound_db_flush_and_literal_only():
    ok_flush = "def t():\n    popoto.get_redis().flushdb()\n"
    ok_literal = "def t():\n    redis.Redis(db=14).get('k')\n"
    assert find_violations(ok_flush) == []
    assert find_violations(ok_literal) == []
