"""Static safety properties of the #756 migration tool.

These need no server, so they run on every CI job: the tool's source names
no live Redis endpoint, its CLI has no way to name one, and its read-only
allowlist holds no command that writes. The behaviour behind them (refusals
before any command is sent, a write that never reaches the server) is pinned
end to end in ``tests/postgres/test_migrate_redis_to_postgres.py``.
"""

import ast
import json
import inspect
import re
from pathlib import Path

import pytest

from popoto import migrate_redis_to_postgres as mig

SOURCE = inspect.getsource(mig)
PACKAGE = Path(mig.__file__).parent


def test_the_watchdog_imports_only_the_standard_library():
    """The watchdog outlives the tool by design; it must never be able to
    open a connection to anything, so it imports neither popoto nor a client
    library."""
    tree = ast.parse((PACKAGE / "_watchdog.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert "subprocess" in imported  # non-vacuity: the file was parsed
    assert imported <= {
        "__future__",
        "json",
        "os",
        "select",
        "shutil",
        "signal",
        "subprocess",
        "sys",
        "typing",
    }, imported


def test_the_report_checksum_is_not_tamper_evidence_but_the_hmac_is(tmp_path):
    """The #792 review flipped a ``mismatch`` report to ``clean``, recomputed
    the sha256 and it verified. That is still true of the checksum, which is
    now named as one; the HMAC under an operator key is what catches it."""
    key_file = tmp_path / "report.key"
    key_file.write_text("0123456789abcdef0123456789abcdef\n")
    key = mig.read_report_key(key_file)
    data = {"verdict": "mismatch", "sign_off": {"operator": "op", "at": "t"}}
    data["sign_off"].update(mig.seal_report(data, key))
    assert mig.verify_report(data) and mig.verify_report(data, key)

    forged = json.loads(json.dumps(data))
    forged["verdict"] = "clean"
    assert not mig.verify_report(forged)  # the checksum catches an edit...
    forged["sign_off"]["checksum_sha256"] = mig.seal_report(forged)["checksum_sha256"]
    assert mig.verify_report(forged)  # ...but anyone can recompute it
    assert not mig.verify_report(forged, key)  # the HMAC cannot be
    assert not mig.verify_report(
        {"verdict": "clean", "sign_off": mig.seal_report({"verdict": "clean"})}, key
    )  # a report without an HMAC fails a keyed check

    # The sign-off's operator and time are covered too.
    renamed = json.loads(json.dumps(data))
    renamed["sign_off"]["operator"] = "someone else"
    assert not mig.verify_report(renamed)

    short = tmp_path / "short.key"
    short.write_text("abc")
    with pytest.raises(mig.MigrationRefused, match="at least 16"):
        mig.read_report_key(short)
    with pytest.raises(mig.MigrationRefused):
        mig.read_report_key(tmp_path / "missing.key")


def test_the_cli_entry_only_delegates_to_the_package():
    """``python -m`` runs ``__main__.py`` as ``__main__``. If the tool's
    logic lived there it would be a second copy of the module, and an
    operator's ``ModelMapping`` (imported from the package) would fail the
    CLI's isinstance check -- the #792 review's first blocker."""
    tree = ast.parse((PACKAGE / "__main__.py").read_text())
    assert not [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    assert any(
        isinstance(n, ast.ImportFrom)
        and n.module == "popoto.migrate_redis_to_postgres"
        and [a.name for a in n.names] == ["main"]
        for n in tree.body
    )


def test_the_source_names_no_redis_endpoint():
    assert "6379" not in SOURCE
    assert "DEFAULT_URL" not in SOURCE
    assert "redis://" not in SOURCE
    assert "from_url" not in SOURCE


def test_the_source_never_names_a_flush():
    assert not re.search(r"flushdb|flushall", SOURCE, re.IGNORECASE)


def test_the_cli_has_no_option_that_names_a_source_server():
    options = [
        option
        for action in mig.build_parser()._actions
        for option in action.option_strings
    ]
    assert "--rdb" in options  # non-vacuity: the parser was read
    assert "--report-key" in options  # "report" holds "port": words are checked
    for option in options:
        words = set(re.split(r"[-_]+", option.lower()))
        assert not words & {"url", "uri", "host", "port", "dsn", "socket"}, option


def test_the_read_only_allowlist_holds_no_write_command():
    writes = {
        "SET",
        "DEL",
        "UNLINK",
        "HSET",
        "HDEL",
        "SADD",
        "SREM",
        "ZADD",
        "ZREM",
        "RPUSH",
        "LPUSH",
        "EXPIRE",
        "PEXPIRE",
        "RENAME",
        "RESTORE",
        "FLUSHDB",
        "FLUSHALL",
        "CONFIG",
        "SHUTDOWN",
        "EVAL",
        "EVALSHA",
        "FCALL",
        "SCRIPT",
        "SAVE",
        "BGSAVE",
        "REPLICAOF",
        "SLAVEOF",
        "MIGRATE",
        "MULTI",
        "XADD",
        "XTRIM",
    }
    assert not (writes & mig.READ_ONLY_COMMANDS)
    assert {"SCAN", "HGETALL", "INFO"} <= mig.READ_ONLY_COMMANDS
