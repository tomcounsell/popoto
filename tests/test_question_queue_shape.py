"""Source-shape guards for the M7 question queue (#566).

The queue's contract ends at "the next question to ask, if any": nothing in
the library addresses the person, and every source of ambiguity writes a
``QuestionCandidate`` instead of asking. Neither property is observable from a
behavioral test -- a module that also printed a question, or a recipe that
quietly called ``next_question()`` on its own and so spent the human-attention
budget behind the host's back, would still pass every assertion in
``tests/test_question_queue.py``. So, in the ``tests/test_type_checking_guard.py``
idiom, these assertions read the source with ``ast``:

- ``question_queue.py`` imports no transport and calls no console/IO builtin.
- No module in ``src/`` other than ``question_queue.py`` imports it: the
  assembler exposes ``refused_keys`` as metadata and never calls ``propose()``,
  and only the host drives ``next_question()`` / ``record_answer()``.
- ``question_queue.py`` binds Redis through ``get_REDIS_DB()`` and never names
  ``POPOTO_REDIS_DB`` (a plain import is a stale snapshot; see CLAUDE.md).

The paths are resolved from this checkout rather than from the imported
package, so the guard reads the tree under test even if the installed package
resolves elsewhere. Never touches a database, so it names none.
"""

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "popoto"
QUEUE_SOURCE = SRC_ROOT / "recipes" / "question_queue.py"

#: Top-level modules that reach a person or a network. Importing any of them
#: would give the queue a way to address someone directly.
TRANSPORT_MODULES = frozenset(
    {
        "sys",  # sys.stdout / sys.stderr
        "socket",
        "smtplib",
        "email",
        "http",
        "urllib",
        "requests",
        "httpx",
        "aiohttp",
        "websockets",
        "subprocess",
        "telegram",
        "slack_sdk",
        "twilio",
    }
)

#: Builtins that write to or read from a console or file.
IO_BUILTINS = frozenset({"print", "input", "open", "breakpoint"})


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _imported_modules(tree: ast.Module) -> set:
    """Fully qualified names of every module imported anywhere in ``tree``,
    including function-local imports (``level`` > 0 imports keep their dots
    so a relative ``from . import x`` is distinguishable)."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            names.add(base)
            names.update(f"{base}.{alias.name}".lstrip(".") for alias in node.names)
    return names


def test_question_queue_source_exists():
    """Guard against a vacuous pass from a moved or renamed file."""
    assert QUEUE_SOURCE.is_file(), QUEUE_SOURCE


def test_question_queue_imports_no_transport():
    imported = _imported_modules(_tree(QUEUE_SOURCE))
    roots = {name.lstrip(".").split(".")[0] for name in imported}
    assert not roots & TRANSPORT_MODULES, sorted(roots & TRANSPORT_MODULES)


def test_question_queue_calls_no_io_builtin_or_console_stream():
    offenders = []
    for node in ast.walk(_tree(QUEUE_SOURCE)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in IO_BUILTINS
        ):
            offenders.append(f"{node.func.id}() at line {node.lineno}")
        if isinstance(node, ast.Attribute) and node.attr in {"stdout", "stderr"}:
            offenders.append(f".{node.attr} at line {node.lineno}")
    assert offenders == []


def test_question_queue_never_names_popoto_redis_db():
    offenders = [
        node.lineno
        for node in ast.walk(_tree(QUEUE_SOURCE))
        if (isinstance(node, ast.alias) and node.name == "POPOTO_REDIS_DB")
        or (isinstance(node, ast.Name) and node.id == "POPOTO_REDIS_DB")
        or (isinstance(node, ast.Attribute) and node.attr == "POPOTO_REDIS_DB")
    ]
    assert offenders == []


def test_no_other_src_module_drives_the_queue():
    """Only the host calls propose*/next_question/record_answer.

    Asserted as "nothing else in ``src/`` imports ``question_queue``", which
    covers ``from .question_queue import next_question``, ``from . import
    question_queue`` and ``import popoto.recipes.question_queue`` alike.
    A comment or docstring mentioning the module (the assembler's points a
    host at ``propose_from_gate``) is not an import and is allowed.
    """
    offenders = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path == QUEUE_SOURCE:
            continue
        for name in _imported_modules(_tree(path)):
            if name.split(".")[-1] == "question_queue":
                offenders.append(f"{path.relative_to(SRC_ROOT)}: {name}")
    assert offenders == []


def test_the_import_detector_is_not_vacuous():
    """The import walker must see the import shapes the guard relies on."""
    tree = ast.parse(
        "from . import question_queue\n"
        "from .question_queue import next_question\n"
        "def f():\n"
        "    import popoto.recipes.question_queue as qq\n"
        "    import sys\n"
    )
    names = _imported_modules(tree)
    assert "question_queue" in {n.split(".")[-1] for n in names}
    assert ".question_queue" in names
    assert "popoto.recipes.question_queue" in names
    assert "sys" in names
