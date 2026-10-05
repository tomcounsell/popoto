"""Static safety properties of the #756 migration tool.

These need no server, so they run on every CI job: the tool's source names
no live Redis endpoint, its CLI has no way to name one, and its read-only
allowlist holds no command that writes. The behaviour behind them (refusals
before any command is sent, a write that never reaches the server) is pinned
end to end in ``tests/postgres/test_migrate_redis_to_postgres.py``.
"""

import inspect
import re

from popoto.transfer import migrate_redis_to_postgres as mig

SOURCE = inspect.getsource(mig)


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
    for option in options:
        assert not re.search(r"url|host|port|dsn", option), option


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
