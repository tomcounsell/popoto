"""Concatenate hand-written narrative docs into a single ``llms-full.txt``.

Reads the nav order from ``mkdocs.yml`` (already loaded by MkDocs at the time
this script runs via ``mkdocs-gen-files``), opens each hand-written ``.md``
source under ``docs/``, prepends an H2 with the page title, and writes the
concatenation to a virtual ``llms-full.txt``.

Notably, this script does NOT embed the auto-generated API reference. Two
reasons:

1. The API tree is virtual at build time, so reading it from disk during
   ``gen_llms_full`` execution is racy/fragile.
2. Concatenating 65+ rendered modules would meaningfully bloat
   ``llms-full.txt`` (the narrative-only file is already ~580KB) without
   commensurate value — agents that need API surface follow the pointer.

Instead, the file ends with a pointer to ``/reference/`` for full API
content and to the GitHub source tree for ground truth.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import mkdocs_gen_files
import yaml

DOCS_DIR = Path("docs")
MKDOCS_CONFIG = Path("mkdocs.yml")
OUTPUT = "llms-full.txt"
SITE_URL = "https://tomcounsell.github.io/popoto/"
SOURCE_URL = "https://github.com/tomcounsell/popoto/tree/main/src/popoto/"


def _flatten_nav(nav: list) -> Iterable[tuple[str, str]]:
    """Walk a nav list and yield (title, source_path) pairs in order.

    Each nav entry is one of:
        - {"Title": "page.md"}
        - {"Title": [<sub-nav>]}
        - "page.md"

    Returns only entries that point to a hand-written ``.md`` file under
    ``docs/`` — auto-generated paths (e.g. ``reference/SUMMARY.md``) are
    skipped because their content does not exist on disk.
    """
    for entry in nav:
        if isinstance(entry, str):
            yield entry, entry
            continue
        if not isinstance(entry, dict):
            continue
        for title, value in entry.items():
            if isinstance(value, str):
                yield title, value
            elif isinstance(value, list):
                yield from _flatten_nav(value)


def _is_handwritten(rel_path: str) -> bool:
    """True iff ``docs/<rel_path>`` exists on disk and is a markdown file."""
    if not rel_path.endswith(".md"):
        return False
    return (DOCS_DIR / rel_path).is_file()


def main() -> None:
    # Read mkdocs.yml directly. MkDocs' Python YAML loader chokes on the
    # custom !!python tags some configs use; this site uses plain YAML so a
    # SafeLoader is sufficient. Fall back to gracefully handling unknown
    # tags by stripping them.
    class _IgnoreTagsLoader(yaml.SafeLoader):
        pass

    def _ignore_unknown(loader, tag_suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node)
        return None

    _IgnoreTagsLoader.add_multi_constructor("!", _ignore_unknown)
    _IgnoreTagsLoader.add_multi_constructor("tag:", _ignore_unknown)

    config = yaml.load(MKDOCS_CONFIG.read_text(), Loader=_IgnoreTagsLoader)
    nav = config.get("nav") or []

    parts: list[str] = []
    site_name = config.get("site_name", "Popoto")
    parts.append(f"# {site_name}\n")
    parts.append(
        "> Single-file concatenation of hand-written documentation for batch\n"
        "> ingestion by AI agents. Generated at build time. For the full\n"
        "> auto-generated API reference, see /reference/ on the site.\n\n"
    )

    for title, rel_path in _flatten_nav(nav):
        if not _is_handwritten(rel_path):
            continue
        page = (DOCS_DIR / rel_path).read_text()
        parts.append(f"## {title}\n\n")
        parts.append(page.rstrip())
        parts.append("\n\n---\n\n")

    parts.append("## API Reference\n\n")
    parts.append(
        f"See {SITE_URL}reference/ for the full auto-generated API reference,\n"
        f"or fetch each module's source directly from {SOURCE_URL}.\n"
    )

    with mkdocs_gen_files.open(OUTPUT, "w") as fd:
        fd.write("".join(parts))


main()
