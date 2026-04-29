"""Generate the API reference tree under /reference/.

Walks the popoto source tree under ``src/popoto`` and emits one virtual
mkdocstrings stub page per importable module. mkdocstrings then renders each
module from its docstrings at build time, respecting ``__all__`` declarations.

Skip rules:
    * ``__pycache__`` directories
    * Dunder ``__*.py`` files (``__init__.py``, ``__main__.py``)

Notably, leading-underscore modules (e.g. ``_error_reporting.py``) are NOT
skipped: ``popoto.__all__`` exposes ``enable_error_reporting`` which lives in
that module. Public/private filtering is delegated to mkdocstrings via
``filters: ["!^_"]`` and ``__all__`` defaults — see mkdocs.yml.

Output layout (virtual files under ``reference/``):
    reference/SUMMARY.md          -- literate-nav index
    reference/popoto.md           -- top-level package
    reference/popoto/<module>.md  -- one per submodule
    reference/popoto/<sub>/<module>.md  -- nested

Each leaf page is a single mkdocstrings directive::

    ::: popoto.fields.relationship
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import mkdocs_gen_files

SRC_ROOT = Path("src")
PACKAGE = "popoto"
NAV_FILE = "SUMMARY.md"

# Modules that depend on optional extras (Django, voyage, openai, ollama, ...)
# or that live inside namespace-only subpackages without an ``__init__.py``.
# Both classes fail mkdocstrings/griffe collection on a stock docs install,
# which would abort ``mkdocs build --strict``. The docs site documents the
# public popoto surface; integration shims for optional ecosystems are
# documented in their own pages.
SKIP_PREFIXES: tuple[str, ...] = (
    "popoto.utils.django",
    "popoto.utils.mixins",
    "popoto.embeddings.voyage",
    "popoto.embeddings.openai",
    "popoto.embeddings.ollama",
)


def iter_module_paths():
    """Yield (relative_module_path, dotted_import_name) pairs."""
    package_root = SRC_ROOT / PACKAGE
    for path in sorted(package_root.rglob("*.py")):
        # Skip __pycache__ trees
        if "__pycache__" in path.parts:
            continue
        # Skip dunder files (__init__.py, __main__.py, etc.)
        if path.name.startswith("__") and path.name.endswith("__.py"):
            continue
        relative = path.relative_to(SRC_ROOT).with_suffix("")
        # relative is like Path("popoto/fields/relationship")
        dotted = ".".join(relative.parts)
        # Skip modules behind optional extras that fail to import on a stock
        # docs install. mkdocstrings would otherwise abort the strict build.
        if any(dotted == p or dotted.startswith(p + ".") for p in SKIP_PREFIXES):
            continue
        # Best-effort importability check: if the module raises ImportError,
        # skip it and surface a warning so docstring problems are obvious.
        try:
            importlib.import_module(dotted)
        except Exception as exc:  # noqa: BLE001 -- want to catch everything
            print(
                f"[gen_api_pages] skipping {dotted}: {exc!r}",
                file=sys.stderr,
            )
            continue
        yield relative, dotted


def write_module_page(relative: Path, dotted: str) -> Path:
    """Write a virtual reference page for a single module.

    Returns the doc-relative output path (e.g. ``reference/popoto/foo.md``).
    """
    out_path = Path("reference") / relative.with_suffix(".md")
    with mkdocs_gen_files.open(out_path, "w") as fd:
        fd.write(f"# `{dotted}`\n\n")
        fd.write(f"::: {dotted}\n")
    # Map the virtual page back to the source file so "Edit this page" links work.
    src_relative = SRC_ROOT / relative.with_suffix(".py")
    mkdocs_gen_files.set_edit_path(out_path, src_relative)
    return out_path


def write_package_index() -> Path:
    """Write a top-level package overview page at ``reference/popoto.md``."""
    out_path = Path("reference") / f"{PACKAGE}.md"
    with mkdocs_gen_files.open(out_path, "w") as fd:
        fd.write(f"# `{PACKAGE}`\n\n")
        fd.write(f"::: {PACKAGE}\n")
    mkdocs_gen_files.set_edit_path(
        out_path, SRC_ROOT / PACKAGE / "__init__.py"
    )
    return out_path


def write_reference_index() -> Path:
    """Write the API reference landing page at ``reference/index.md``.

    Without this, ``/reference/`` returns 404 because the literate-nav
    SUMMARY.md and the per-module pages all live one level down. Pointing
    the nav entry at ``reference/index.md`` gives the section a real URL
    while ``literate-nav`` still drives the sidebar tree.
    """
    out_path = Path("reference") / "index.md"
    with mkdocs_gen_files.open(out_path, "w") as fd:
        fd.write("# API Reference\n\n")
        fd.write(
            "Auto-generated from the popoto source tree. Every public symbol\n"
            "exported from `popoto.__all__` is documented here.\n\n"
            "Browse the full module tree in the sidebar, or jump to:\n\n"
            "- [`popoto`](popoto.md) — top-level package surface\n"
            "- [`popoto.models`](popoto/models/base.md) — `Model` base class and metaclass\n"
            "- [`popoto.fields`](popoto/fields/field.md) — field types and mixins\n"
            "- [`popoto.models.query`](popoto/models/query.md) — `Query`, filtering, expressions\n"
            "- [`popoto.pubsub`](popoto/pubsub/publisher.md) — pub/sub messaging\n\n"
            "For narrative documentation see the [Getting Started](../configuration.md),\n"
            "[Core Features](../meta.md), and [Agent Memory](../features/agent-memory.md) sections.\n"
        )
    return out_path


def write_summary(entries: list[tuple[Path, str]]) -> None:
    """Write a literate-nav SUMMARY.md indexing every emitted page.

    ``entries`` is a list of (out_path, dotted) pairs ordered as written.
    The summary lists the package index first, then every submodule sorted
    by dotted name.
    """
    summary_path = Path("reference") / NAV_FILE
    with mkdocs_gen_files.open(summary_path, "w") as fd:
        fd.write(f"* [{PACKAGE}](popoto.md)\n")
        for out_path, dotted in entries:
            # Skip the top-level index (already written above).
            if out_path.name == f"{PACKAGE}.md" and out_path.parent.name == "reference":
                continue
            # out_path is like "reference/popoto/fields/relationship.md".
            # Make it relative to the reference/ root for the nav.
            relative_to_ref = out_path.relative_to("reference")
            fd.write(f"* [{dotted}]({relative_to_ref.as_posix()})\n")


def main() -> None:
    write_reference_index()
    write_package_index()
    entries: list[tuple[Path, str]] = []
    for relative, dotted in iter_module_paths():
        out_path = write_module_page(relative, dotted)
        entries.append((out_path, dotted))
    # Sort entries by dotted name so the nav is deterministic and readable.
    entries.sort(key=lambda pair: pair[1])
    write_summary(entries)


main()
