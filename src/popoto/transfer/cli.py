"""``popoto-transfer`` -- a CLI front-end for :mod:`popoto.transfer`.

Two subcommands:

``export``
    Wraps :func:`popoto.transfer.export_records`. Writes JSON Lines to
    ``--out`` (a file, or ``-`` for stdout) and a human summary to stderr.
``import``
    Wraps :func:`popoto.transfer.import_records`. Reads JSON Lines from
    ``--in`` (a file, or ``-`` for stdin) and prints the reconciliation
    report to stderr.

Both subcommands refuse to touch Redis database 0 unless ``--allow-db0`` is
passed. Database 0 is, on many machines running this ORM, a live store rather
than a test database, and an import writes to it. The guard reads the
database off the live connection pool -- not an environment variable -- so it
catches the unset-``REDIS_URL`` fallback as well as an explicit ``…/0`` URL.

The refusal applies to transfers that use Redis. A model bound to another
backend (``Meta.backend = "postgres"``, or ``POPOTO_BACKEND=postgres``) is
transferred without ``--allow-db0`` when Redis is on database 0, because the
transfer never contacts Redis. That is enforced rather than assumed: from
before the ``--model`` module is imported until the run ends, the Redis
connection pool refuses to hand out a connection (a pipeline included), so
any Redis command the model module or the transfer would issue is refused
before it reaches database 0. A Redis-bound model is refused once it is
resolved, before its first command.

The human-readable summary always goes to **stderr**, never stdout, so that
``--out -`` can stream JSON Lines on stdout without the summary corrupting
it: ``popoto-transfer export --model pkg.mod:Model --out - | gzip > b.gz``
still shows the operator a summary on their terminal. ``--json`` claims
stdout for a machine-readable summary instead, and is refused together with
``--out -`` since both would write to stdout.

Submodule imports (``redis``, the transfer drivers, the model registry) are
kept out of module scope and inside the functions that need them, so argument
parsing and the database-0 fence run before any of them is touched. This does
not make ``--help`` cheap: the console-script entry point imports
``popoto.transfer.cli``, which imports the ``popoto`` package, so the ORM is
already resolved by the time :func:`main` is called.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, List, Optional

USAGE_EPILOG = """\
examples:
  popoto-transfer export --model myapp.models:Memory --filter project_key=ai \\
      --out memories.jsonl
  popoto-transfer import --model myapp.models:Memory --in memories.jsonl \\
      --on-conflict overwrite

notes:
  --model takes 'module.path:ClassName' (one colon). The named module is
  imported, so importing this CLI runs whatever module-level code the
  operator's model module contains.

  Keys are always preserved on import, so a re-run with
  --on-conflict overwrite converges rather than duplicating. Import is not
  atomic across records: if it is interrupted, re-run with
  --on-conflict overwrite to finish.
"""

DB0_ALTERNATIVE = "REDIS_URL=redis://localhost:6379/1"


class CLIError(Exception):
    """A diagnosed, user-facing CLI failure.

    Raised by :func:`resolve_model` and the flag-validation helpers. Callers
    catch it, print ``str(exc)`` to stderr, and return exit code 1 -- never a
    traceback.
    """


def build_parser() -> argparse.ArgumentParser:
    """Construct the argument parser.

    Returns:
        The configured :class:`argparse.ArgumentParser`. Subparser names are
        the strings ``"export"`` and ``"import"``; ``import`` is a Python
        keyword, so dispatch in :func:`main` reads ``args.command`` rather
        than an attribute named ``import``.
    """
    from .export import DEFAULT_CHUNK_SIZE

    parser = argparse.ArgumentParser(
        prog="popoto-transfer",
        description=(
            "Move one Popoto model's records between Redis/Valkey "
            "instances, with a reconciliation report and an exit code a "
            "script can act on."
        ),
        epilog=USAGE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    export = sub.add_parser(
        "export",
        help="export a model's records to JSON Lines",
        description=(
            "Exports a model's records as JSON Lines: one manifest line "
            "followed by one line per record."
        ),
    )
    _add_shared_arguments(export)
    export.add_argument(
        "--out",
        default="-",
        help="destination file, or '-' for stdout (default: -)",
    )
    export.add_argument(
        "--filter",
        action="append",
        default=None,
        metavar="KEY=VALUE",
        help=(
            "equality filter, repeatable; the value is parsed as JSON "
            "first (so 0.5, true, null work), falling back to a raw "
            "string. Q objects and lookup operators are not expressible "
            "here; use the Python API for those."
        ),
    )
    export.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"keys hydrated per round trip (default: {DEFAULT_CHUNK_SIZE})",
    )

    imp = sub.add_parser(
        "import",
        help="import a model's records from JSON Lines",
        description=(
            "Imports records from a JSON Lines export produced by "
            "'popoto-transfer export'. Keys are always preserved, so a "
            "re-run with --on-conflict overwrite converges rather than "
            "duplicating. Import is not atomic across records: if it is "
            "interrupted, re-run with --on-conflict overwrite to finish."
        ),
    )
    _add_shared_arguments(imp)
    imp.add_argument(
        "--in",
        dest="in_path",
        default="-",
        help="source file, or '-' for stdin (default: -)",
    )
    imp.add_argument(
        "--on-conflict",
        choices=["error", "skip", "overwrite"],
        default="error",
        help=(
            "what to do when the destination already holds a key "
            "(default: error, which refuses and cannot clobber)"
        ),
    )
    imp.add_argument(
        "--on-write-gate",
        choices=["reject", "bypass"],
        default="reject",
        help=(
            "honor the destination model's write gate " "(default: reject) or bypass it"
        ),
    )
    imp.add_argument(
        "--regenerate-keys",
        action="store_true",
        help=(
            "mint a new key for every record instead of preserving the "
            "exported one, remapping Relationship references onto the new "
            "keys. NOT idempotent -- a second run creates a second copy of "
            "every record. Only fields that declare a reference are remapped; "
            "a key stored in a plain string field is never rewritten and will "
            "dangle. Requires the destination model's key to be exactly one "
            "AutoKeyField"
        ),
    )
    imp.add_argument(
        "--on-embedding-mismatch",
        choices=["error", "carry", "regenerate"],
        default="error",
        help=(
            "what to do when an exported embedding's provider fingerprint "
            "differs from the destination's (default: error)"
        ),
    )
    return parser


def _add_shared_arguments(subparser: argparse.ArgumentParser) -> None:
    """Add the flags common to both subcommands."""
    subparser.add_argument(
        "--model",
        required=True,
        metavar="module.path:ClassName",
        help="dotted module path and class name of the Popoto Model",
    )
    subparser.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable JSON summary on stdout instead",
    )
    subparser.add_argument(
        "--allow-db0",
        action="store_true",
        help="allow running against Redis database 0",
    )


def resolve_model(spec: str) -> Any:
    """Resolve a ``"module.path:ClassName"`` spec into a Model subclass.

    Prepends the current working directory to ``sys.path`` first, since a
    console script does not get the CWD on ``sys.path`` the way
    ``python -m`` does, and the single most likely first invocation is from
    the operator's own project root.

    Args:
        spec: A colon-separated model spec, e.g. ``"myapp.models:Memory"``.

    Returns:
        The resolved :class:`popoto.Model` subclass.

    Raises:
        CLIError: If ``spec`` does not have exactly one colon, either half
            is empty, the module cannot be imported, the module has no such
            attribute, or the attribute is not a ``Model`` subclass. Each
            failure carries a distinct message naming what went wrong.
    """
    import importlib

    from popoto import Model

    if spec.count(":") != 1:
        raise CLIError(
            f"--model {spec!r} must be 'module.path:ClassName' (exactly " "one colon)"
        )
    module_path, _, class_name = spec.partition(":")
    if not module_path or not class_name:
        raise CLIError(
            f"--model {spec!r} must name both a module and a class, e.g. "
            "'myapp.models:Memory'"
        )

    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise CLIError(
            f"--model: could not import module {module_path!r}: {exc}"
        ) from exc

    try:
        obj = getattr(module, class_name)
    except AttributeError:
        raise CLIError(
            f"--model: module {module_path!r} has no attribute " f"{class_name!r}"
        ) from None

    if not isinstance(obj, type) or not issubclass(obj, Model):
        raise CLIError(
            f"--model: {module_path}:{class_name} is not a Popoto Model " "subclass"
        )
    return obj


class _Db0Fence:
    """Refuse every Redis command on database 0 unless opted in.

    Reads the database off the live connection pool rather than an
    environment variable, so this catches both an explicit
    ``REDIS_URL=…/0`` and the unset-``REDIS_URL`` fallback (which also binds
    database 0). While the fence is entered and :attr:`active`, every
    redis-py connection pool, sync or async, refuses to hand out a
    connection to database 0: ``get_connection`` raises on the pool classes
    themselves, so a client the ``--model`` module builds or rebinds is
    fenced too. Every command, pipelines and pub/sub included, checks a
    connection out first, so nothing reaches the server. A pool bound to
    another database is unaffected. Entering issues no Redis command.

    Args:
        allow_db0: Whether ``--allow-db0`` was passed.
        verb: ``"read from"`` for export or ``"write to"`` for import,
            naming the consequence in the refusal message.
    """

    def __init__(self, allow_db0: bool, verb: str) -> None:
        from popoto.redis_db import get_REDIS_DB

        self.verb = verb
        self.db = _pool_db(get_REDIS_DB().connection_pool)
        #: The database no pool may hand out a connection to while armed.
        self.fenced_db = 0
        self.active = self.db == 0 and not allow_db0
        #: Set when something asked a fenced pool for a connection.
        self.tripped = False
        #: Set once the transfer itself begins, after the model resolved.
        self.started = False
        self._saved: "list[tuple[type, Any]]" = []

    def __enter__(self) -> "_Db0Fence":
        if self.active:
            for cls in _pool_classes():
                original = vars(cls)["get_connection"]
                self._saved.append((cls, original))
                setattr(cls, "get_connection", self._guard(original))
        return self

    def __exit__(self, *exc_info: Any) -> None:
        while self._saved:
            cls, original = self._saved.pop()
            setattr(cls, "get_connection", original)

    def _guard(self, original: Any) -> Any:
        fence = self

        def get_connection(pool: Any, *args: Any, **kwargs: Any) -> Any:
            if _pool_db(pool) == fence.fenced_db:
                fence.tripped = True
                raise CLIError(fence.message())
            return original(pool, *args, **kwargs)

        return get_connection

    def message(self) -> str:
        return (
            f"refusing to {self.verb} Redis database {self.db} -- this is "
            "often a live store, not a test database.\n"
            "  Pass --allow-db0 to proceed anyway, or point at a different "
            f"database, e.g. {DB0_ALTERNATIVE}"
        )

    def refuse(self) -> int:
        """Print the refusal and return the exit code for it."""
        sys.stderr.write(f"popoto-transfer: {self.message()}\n")
        if self.started:
            sys.stderr.write(
                "popoto-transfer: the refused command came partway through "
                "the run, so records handled before it may already have been "
                "written or exported\n"
            )
        return 1


def _pool_db(pool: Any) -> int:
    return int(getattr(pool, "connection_kwargs", {}).get("db", 0) or 0)


def _pool_classes() -> "list[type]":
    """Every redis-py pool class that defines its own ``get_connection``,
    sync and async. Fencing the classes rather than one pool instance covers
    a client the model module builds or rebinds after the fence is up."""
    import redis.asyncio.connection as async_connection
    import redis.connection as sync_connection

    found = []
    for module in (sync_connection, async_connection):
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and "get_connection" in vars(value)
                and not value.__name__.endswith("Interface")
            ):
                found.append(value)
    return found


def _uses_redis(model_class: Any) -> bool:
    """Whether a transfer of ``model_class`` reads or writes Redis. Resolving
    the backend issues no Redis command."""
    from ..backends.routing import non_redis_backend

    return non_redis_backend(model_class) is None


def _parse_filters(pairs: "Optional[List[str]]") -> "dict[str, Any]":
    """Parse repeated ``--filter KEY=VALUE`` flags into a kwargs dict.

    Each value is parsed as JSON first (so ``0.5``, ``true``, ``null`` carry
    their type), falling back to the raw string when JSON parsing fails.

    Raises:
        CLIError: If a pair has no ``=`` or an empty key.
    """
    import json

    filters: "dict[str, Any]" = {}
    for pair in pairs or []:
        key, sep, raw_value = pair.partition("=")
        if not sep or not key:
            raise CLIError(f"--filter {pair!r} must be 'key=value'")
        try:
            value = json.loads(raw_value)
        except ValueError:
            value = raw_value
        filters[key] = value
    return filters


def _unlink_quietly(path: str) -> None:
    """Remove ``path`` if present. Never raises."""
    try:
        os.unlink(path)
    except OSError:
        pass


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point for the ``popoto-transfer`` console script.

    Args:
        argv: Argument list, defaulting to ``sys.argv[1:]``.

    Returns:
        A process exit code: ``0`` on a clean run, ``1`` on an operational
        failure (bad ``--model``, the database-0 refusal, an unreadable
        file, a manifest mismatch, a query error, a connection error, or an
        ``on_conflict="error"`` collision -- which may have written earlier
        records before raising), ``2`` on an argparse usage error (argparse's
        own convention), or ``3`` when the run completed but at least one
        record did not land (any ``rejected``/``errored``/``partial``
        import outcome, or any export error; a ``skipped`` import outcome is
        clean and does not trigger this).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "export":
        return _cmd_export(args)
    if args.command == "import":
        return _cmd_import(args)
    parser.print_help()
    return 0


def _cmd_export(args: Any) -> int:
    """Run the ``export`` subcommand."""
    if args.json and args.out == "-":
        sys.stderr.write(
            "popoto-transfer export: --json and --out - both write to "
            "stdout; pick one (write JSON Lines to a file with --out, or "
            "drop --json)\n"
        )
        return 1

    with _Db0Fence(args.allow_db0, "read from") as fence:
        code = _run_export(args, fence)
    return fence.refuse() if fence.tripped else code


def _run_export(args: Any, fence: _Db0Fence) -> int:
    """The ``export`` subcommand inside the database-0 fence."""
    try:
        model_class = resolve_model(args.model)
        filters = _parse_filters(args.filter)
    except CLIError as exc:
        if fence.tripped:
            return 1
        sys.stderr.write(f"popoto-transfer export: {exc}\n")
        return 1
    # A trip the model module caught while importing still refuses the run,
    # before any record is read or written.
    if fence.tripped or (fence.active and _uses_redis(model_class)):
        fence.tripped = True
        return 1
    fence.started = True

    from ..backends.types import OUTAGE_ERRORS
    from ..exceptions import ModelException
    from ..models.query import QueryException
    from .export import export_records

    out_path = args.out
    part_path = None
    if out_path == "-":
        stream = sys.stdout
    else:
        part_path = out_path + ".part"
        try:
            stream = open(part_path, "w")
        except OSError as exc:
            sys.stderr.write(
                f"popoto-transfer export: could not open {part_path!r}: " f"{exc}\n"
            )
            return 1

    # OUTAGE_ERRORS covers a store outage on either backend (#816), including
    # redis-py's TimeoutError, which is not the builtin one listed here.
    failures: tuple[type[BaseException], ...] = (
        ModelException,
        QueryException,
        TimeoutError,
        OSError,
        KeyboardInterrupt,
        CLIError,
    ) + OUTAGE_ERRORS
    try:
        result = export_records(
            model_class, stream=stream, chunk_size=args.chunk_size, **filters
        )
    except failures as exc:
        message = "interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
        if not fence.tripped:
            sys.stderr.write(f"popoto-transfer export: {message}\n")
        if part_path is not None:
            stream.close()
            _unlink_quietly(part_path)
        return 1

    if fence.tripped:
        # A record's Redis read was refused and recorded as an export error.
        if part_path is not None:
            stream.close()
            _unlink_quietly(part_path)
        return 1

    if part_path is not None:
        stream.close()
        try:
            os.replace(part_path, out_path)
        except OSError as exc:
            sys.stderr.write(
                f"popoto-transfer export: could not write {out_path!r}: " f"{exc}\n"
            )
            _unlink_quietly(part_path)
            return 1

    sys.stderr.write(result.summary() + "\n")
    if args.json:
        _render_export_json(result)

    if result.errors:
        return 3
    return 0


def _cmd_import(args: Any) -> int:
    """Run the ``import`` subcommand."""
    with _Db0Fence(args.allow_db0, "write to") as fence:
        code = _run_import(args, fence)
    return fence.refuse() if fence.tripped else code


def _run_import(args: Any, fence: _Db0Fence) -> int:
    """The ``import`` subcommand inside the database-0 fence."""
    try:
        model_class = resolve_model(args.model)
    except CLIError as exc:
        if fence.tripped:
            return 1
        sys.stderr.write(f"popoto-transfer import: {exc}\n")
        return 1
    # A trip the model module caught while importing still refuses the run,
    # before any record is read or written.
    if fence.tripped or (fence.active and _uses_redis(model_class)):
        fence.tripped = True
        return 1
    fence.started = True

    in_path = args.in_path
    try:
        stream = sys.stdin if in_path == "-" else open(in_path, "r")
    except OSError as exc:
        sys.stderr.write(f"popoto-transfer import: could not open {in_path!r}: {exc}\n")
        return 1

    from ..backends.types import OUTAGE_ERRORS
    from ..exceptions import ModelException
    from ..models.query import QueryException
    from .import_ import import_records

    # OUTAGE_ERRORS: a store outage on either backend (#816), as on export.
    failures: tuple[type[BaseException], ...] = (
        ModelException,
        QueryException,
        TimeoutError,
        OSError,
        CLIError,
    ) + OUTAGE_ERRORS
    try:
        report = import_records(
            model_class,
            stream,
            on_conflict=args.on_conflict,
            on_write_gate=args.on_write_gate,
            on_embedding_mismatch=args.on_embedding_mismatch,
            preserve_keys=not args.regenerate_keys,
        )
    except failures as exc:
        # ModelException also covers an on_conflict="error" collision, which
        # raises from inside the per-record loop after earlier records in
        # this run have already been written. Its own message says so; it
        # is printed verbatim rather than replaced with a message implying
        # the run was a no-op.
        if not fence.tripped:
            sys.stderr.write(f"popoto-transfer import: {exc}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write(
            "popoto-transfer import: interrupted; import is not atomic "
            "across records -- re-run with --on-conflict overwrite to "
            "converge\n"
        )
        return 1
    finally:
        if in_path != "-":
            stream.close()

    sys.stderr.write(report.summary() + "\n")
    if args.json:
        _render_import_json(report)

    if report.rejected or report.errored or report.partial:
        return 3
    return 0


def _render_export_json(result: Any) -> None:
    """Write ``result`` as indented JSON on stdout."""
    import dataclasses
    import json

    sys.stdout.write(
        json.dumps(dataclasses.asdict(result), indent=2, default=str) + "\n"
    )


def _render_import_json(report: Any) -> None:
    """Write ``report`` as indented JSON on stdout, plus a ``counts`` roll-up.

    ``ImportReport``'s five category counts (``total``, ``landed``, …) are
    computed properties, not dataclass fields, so ``dataclasses.asdict``
    does not include them. An explicit ``counts`` object is added so a
    consumer does not have to re-tally ``outcomes`` itself.
    """
    import dataclasses
    import json

    from .results import CATEGORIES

    payload = dataclasses.asdict(report)
    payload["counts"] = {category: report.count(category) for category in CATEGORIES}
    sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
