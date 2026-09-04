"""``popoto-transfer`` -- a CLI front-end for :mod:`popoto.transfer`.

Two subcommands:

``export``
    Wraps :func:`popoto.transfer.export_records`. Writes JSON Lines to
    ``--out`` (a file, or ``-`` for stdout) and a human summary to stderr.
``import``
    Wraps :func:`popoto.transfer.import_records`. Reads JSON Lines from
    ``--in`` (a file, or ``-`` for stdin) and prints the reconciliation
    report to stderr.

Both subcommands refuse to run against Redis database 0 unless ``--allow-db0``
is passed. Database 0 is, on many machines running this ORM, a live store
rather than a test database, and an import writes to it. The guard reads the
database off the live connection pool -- not an environment variable -- so it
catches the unset-``REDIS_URL`` fallback as well as an explicit ``…/0`` URL,
and it runs before the operator's ``--model`` module is imported and before
any Redis command is issued.

The human-readable summary always goes to **stderr**, never stdout, so that
``--out -`` can stream JSON Lines on stdout without the summary corrupting
it: ``popoto-transfer export --model pkg.mod:Model --out - | gzip > b.gz``
still shows the operator a summary on their terminal. ``--json`` claims
stdout for a machine-readable summary instead, and is refused together with
``--out -`` since both would write to stdout.

Heavy imports (``popoto``, ``redis``) are kept out of module scope and inside
the functions that need them, so ``popoto-transfer --help`` and argument
parsing do not pay for importing the ORM.
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


def _check_db0(allow_db0: bool, verb: str) -> "int | None":
    """Refuse to proceed against Redis database 0 unless opted in.

    Reads the database off the live connection pool rather than an
    environment variable, so this catches both an explicit
    ``REDIS_URL=…/0`` and the unset-``REDIS_URL`` fallback (which also binds
    database 0). Runs before any Redis command is issued and before the
    operator's ``--model`` module is imported.

    Args:
        allow_db0: Whether ``--allow-db0`` was passed.
        verb: ``"read from"`` for export or ``"write to"`` for import,
            naming the consequence in the refusal message.

    Returns:
        ``1`` if the run must be refused, ``None`` if it may proceed.
    """
    from popoto.redis_db import POPOTO_REDIS_DB

    db = int(POPOTO_REDIS_DB.connection_pool.connection_kwargs.get("db", 0) or 0)
    if db != 0 or allow_db0:
        return None
    sys.stderr.write(
        f"popoto-transfer: refusing to {verb} database {db} -- this is "
        "often a live store, not a test database.\n"
        "  Pass --allow-db0 to proceed anyway, or point at a different "
        f"database, e.g. {DB0_ALTERNATIVE}\n"
    )
    return 1


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
    refusal = _check_db0(args.allow_db0, "read from")
    if refusal is not None:
        return refusal

    if args.json and args.out == "-":
        sys.stderr.write(
            "popoto-transfer export: --json and --out - both write to "
            "stdout; pick one (write JSON Lines to a file with --out, or "
            "drop --json)\n"
        )
        return 1

    try:
        model_class = resolve_model(args.model)
        filters = _parse_filters(args.filter)
    except CLIError as exc:
        sys.stderr.write(f"popoto-transfer export: {exc}\n")
        return 1

    from redis import exceptions as redis_exceptions

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

    try:
        result = export_records(
            model_class, stream=stream, chunk_size=args.chunk_size, **filters
        )
    except (
        ModelException,
        QueryException,
        redis_exceptions.ConnectionError,
        TimeoutError,
        OSError,
        KeyboardInterrupt,
    ) as exc:
        message = "interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
        sys.stderr.write(f"popoto-transfer export: {message}\n")
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
    refusal = _check_db0(args.allow_db0, "write to")
    if refusal is not None:
        return refusal

    try:
        model_class = resolve_model(args.model)
    except CLIError as exc:
        sys.stderr.write(f"popoto-transfer import: {exc}\n")
        return 1

    in_path = args.in_path
    try:
        stream = sys.stdin if in_path == "-" else open(in_path, "r")
    except OSError as exc:
        sys.stderr.write(f"popoto-transfer import: could not open {in_path!r}: {exc}\n")
        return 1

    from redis import exceptions as redis_exceptions

    from ..exceptions import ModelException
    from ..models.query import QueryException
    from .import_ import import_records

    try:
        report = import_records(
            model_class,
            stream,
            on_conflict=args.on_conflict,
            on_write_gate=args.on_write_gate,
            on_embedding_mismatch=args.on_embedding_mismatch,
        )
    except (
        ModelException,
        QueryException,
        redis_exceptions.ConnectionError,
        TimeoutError,
        OSError,
    ) as exc:
        # ModelException also covers an on_conflict="error" collision, which
        # raises from inside the per-record loop after earlier records in
        # this run have already been written. Its own message says so; it
        # is printed verbatim rather than replaced with a message implying
        # the run was a no-op.
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
