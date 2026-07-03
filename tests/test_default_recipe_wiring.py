"""Default recipe BM25 wiring check (query-sensitivity regression guard).

Guards issue #445: the *shipped* default agent-memory recipe — the model
users are steered toward by `docs/guides/agent-memory-quickstart.md` — must
declare a `BM25Field` so `ContextAssembler(retrieval_mode="auto")` resolves
to a query-sensitive mode (`lexical` or `hybrid`), never the query-blind
`composite` path.

The CSR harness (#418, `tests/benchmarks/csr/`) validates the mode-resolution
*mechanism* with per-case model classes that hardcode `BM25Field` — lexical
mode is guaranteed by construction there. This test instead parses the real
quickstart doc and materializes the recipe it actually ships, so a doc edit
that drops `BM25Field` fails this test the way it would silently regress
production retrieval otherwise.

This is a test-only file: it reads `docs/guides/agent-memory-quickstart.md`
and performs no writes of its own (class definitions do not touch Redis).
"""

import ast
import os
import re
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src.popoto import BM25Field  # noqa: E402
from src.popoto.recipes.context_assembler import ContextAssembler  # noqa: E402

QUICKSTART_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "guides"
    / "agent-memory-quickstart.md"
)

# The quickstart's own Level 5 score_weights, used unchanged so the assembler
# is constructed exactly as the doc constructs it (harmless for lexical/hybrid
# paths, which ignore score_weights for the pull path).
LEVEL5_SCORE_WEIGHTS = {"relevance": 0.6, "confidence": 0.3}


def _extract_pre_level5_memory_classes():
    """Parse the quickstart doc and materialize each `Memory` class defined
    in a fenced python block before the `## Level 5` heading.

    Returns a list of `Memory` classes in document order (one per fenced
    python block that defines `class Memory`). Only `Import`/`ImportFrom`/
    `ClassDef` AST nodes are executed — no doc runtime statements (saves,
    LLM/embedding calls) ever run.

    Level 5 is identified **solely** by matching `^## Level 5\\b` against the
    heading line — never by a substring match against "ContextAssembler" in
    the heading text, since the real heading
    ("## Level 5: Cognition — LLM-Ready Context Assembly") contains no such
    substring.
    """
    text = QUICKSTART_PATH.read_text()
    lines = text.splitlines()

    level5_line_idx = None
    for i, line in enumerate(lines):
        if re.match(r"^## Level 5\b", line):
            level5_line_idx = i
            break

    assert level5_line_idx is not None, (
        f"Structural guard failed: no '## Level 5' heading found in "
        f"{QUICKSTART_PATH}. The quickstart doc may have been restructured; "
        f"update the extractor's heading anchor if Level 5 was renamed."
    )

    # Semantic anchor: "ContextAssembler" must appear in the Level 5 section
    # BODY (never required in the heading itself).
    next_heading_idx = len(lines)
    for i in range(level5_line_idx + 1, len(lines)):
        if lines[i].startswith("## "):
            next_heading_idx = i
            break
    level5_body = "\n".join(lines[level5_line_idx + 1 : next_heading_idx])
    assert "ContextAssembler" in level5_body, (
        "Structural guard failed: the Level 5 section body no longer "
        "mentions ContextAssembler. The quickstart's Cognition level may "
        "have been restructured."
    )

    pre_level5_text = "\n".join(lines[:level5_line_idx])
    code_blocks = re.findall(r"```python\n(.*?)```", pre_level5_text, re.DOTALL)

    namespace = {}
    memory_classes = []
    for block in code_blocks:
        tree = ast.parse(block)
        keep_nodes = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef))
        ]
        if not keep_nodes:
            continue
        filtered_module = ast.Module(body=keep_nodes, type_ignores=[])
        ast.fix_missing_locations(filtered_module)
        code = compile(filtered_module, filename=str(QUICKSTART_PATH), mode="exec")
        exec(code, namespace)  # noqa: S102 - controlled doc-derived AST, defs only

        for node in keep_nodes:
            if isinstance(node, ast.ClassDef) and node.name == "Memory":
                memory_classes.append(namespace["Memory"])

    return memory_classes


@pytest.fixture(scope="module")
def pre_level5_memory_classes():
    return _extract_pre_level5_memory_classes()


@pytest.fixture(scope="module")
def default_recipe(pre_level5_memory_classes):
    """The recipe in effect when the quickstart reaches ContextAssembler:
    the last `Memory` defined before the Level 5 heading."""
    return pre_level5_memory_classes[-1]


def _has_bm25_field(model_class):
    return any(
        isinstance(field, BM25Field) for field in model_class._meta.fields.values()
    )


class TestQuickstartStructure:
    def test_quickstart_has_context_assembler_level(self, pre_level5_memory_classes):
        """Structural guard: the Level 5 heading was found (see fixture setup
        assertions) and at least 2 `Memory` definitions precede it. Prevents
        a doc restructure from causing this suite to pass vacuously."""
        assert len(pre_level5_memory_classes) >= 2, (
            f"Expected at least 2 'Memory' class definitions before the "
            f"'## Level 5' heading in {QUICKSTART_PATH}, found "
            f"{len(pre_level5_memory_classes)}. The quickstart's progressive "
            f"levels (1-4) may have been restructured."
        )


class TestDefaultRecipeWiring:
    def test_default_recipe_declares_bm25_field(self, default_recipe):
        assert _has_bm25_field(default_recipe), (
            f"The default agent-memory recipe from "
            f"{QUICKSTART_PATH} no longer declares a BM25Field. "
            f"ContextAssembler(retrieval_mode='auto') will silently fall "
            f"back to the query-blind 'composite' path for every user "
            f"following the quickstart."
        )

    def test_default_recipe_auto_mode_is_query_sensitive(self, default_recipe):
        # Construct without passing retrieval_mode, so this also guards the
        # default value of retrieval_mode itself ("auto").
        assembler = ContextAssembler(
            model_class=default_recipe,
            score_weights=LEVEL5_SCORE_WEIGHTS,
        )
        assert assembler._effective_mode in {"lexical", "hybrid"}, (
            f"Default recipe from {QUICKSTART_PATH} resolved to "
            f"'{assembler._effective_mode}' under retrieval_mode='auto' "
            f"(the default). Expected a query-sensitive mode "
            f"('lexical' or 'hybrid'). This means BM25Field is missing or "
            f"misconfigured on the default recipe."
        )
        assert assembler._effective_mode != "composite", (
            f"Default recipe from {QUICKSTART_PATH} resolved to the "
            f"query-blind 'composite' path under retrieval_mode='auto' "
            f"(the default). Production retrieval for every user following "
            f"the quickstart would rank results independent of query text "
            f"(#409-class regression: P@10 ~= random)."
        )

    def test_recipe_levels_after_attention_keep_bm25(self, pre_level5_memory_classes):
        """Every Memory definition from the second one onward (Levels 2-4,
        i.e. everything after Level 1's pure recall recipe) declares
        BM25Field. Pins the doc's 'query-sensitive from Level 2 on'
        narrative; deliberately redundant with the default-recipe check
        above since the default recipe IS one of these classes."""
        for idx, model_class in enumerate(pre_level5_memory_classes[1:], start=2):
            assert _has_bm25_field(model_class), (
                f"Memory definition #{idx} (document order) in "
                f"{QUICKSTART_PATH} does not declare a BM25Field. Levels "
                f"2-4 of the quickstart are documented as query-sensitive; "
                f"this recipe regressed that guarantee."
            )


class TestGuardTripsWhenBM25Removed:
    def test_guard_trips_when_bm25_removed(self, default_recipe):
        """Red-proof: feed the extractor a doc variant with BM25Field lines
        stripped (in-memory string, never a file edit) and confirm the mode
        assertion fails. Kept as a permanent test so the red-proof isn't a
        one-off manual check."""
        doc_text = QUICKSTART_PATH.read_text()
        stripped_text = re.sub(
            r"^\s*content_bm25\s*=\s*BM25Field\([^)]*\).*$",
            "",
            doc_text,
            flags=re.MULTILINE,
        )
        assert stripped_text != doc_text, (
            "Red-proof setup failed: stripping 'content_bm25 = BM25Field(...)' "
            "lines had no effect on the doc text, so this red-proof would not "
            "actually exercise the failure path."
        )

        lines = stripped_text.splitlines()
        level5_line_idx = next(
            i for i, line in enumerate(lines) if re.match(r"^## Level 5\b", line)
        )
        pre_level5_text = "\n".join(lines[:level5_line_idx])
        code_blocks = re.findall(r"```python\n(.*?)```", pre_level5_text, re.DOTALL)

        namespace = {}
        memory_classes = []
        for block in code_blocks:
            tree = ast.parse(block)
            keep_nodes = [
                node
                for node in tree.body
                if isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef))
            ]
            if not keep_nodes:
                continue
            filtered_module = ast.Module(body=keep_nodes, type_ignores=[])
            ast.fix_missing_locations(filtered_module)
            code = compile(filtered_module, filename=str(QUICKSTART_PATH), mode="exec")
            exec(code, namespace)  # noqa: S102 - controlled doc-derived AST, defs only
            for node in keep_nodes:
                if isinstance(node, ast.ClassDef) and node.name == "Memory":
                    memory_classes.append(namespace["Memory"])

        stripped_default_recipe = memory_classes[-1]
        assert not _has_bm25_field(stripped_default_recipe), (
            "Red-proof setup failed: BM25Field is still present on the "
            "recipe after stripping content_bm25 lines from the doc text."
        )

        assembler = ContextAssembler(
            model_class=stripped_default_recipe,
            score_weights=LEVEL5_SCORE_WEIGHTS,
        )
        assert assembler._effective_mode == "composite", (
            "Red-proof failed: expected the BM25-stripped recipe to resolve "
            "to 'composite' under retrieval_mode='auto', but it resolved to "
            f"'{assembler._effective_mode}'. The guard would not actually "
            "catch a #409-class regression."
        )
