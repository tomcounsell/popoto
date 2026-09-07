"""The PEP 562 hook must stay hidden from type checkers and live at runtime.

#651 made ``popoto.POPOTO_REDIS_DB`` a module ``__getattr__`` so it resolves the
current connection on every access. That fixed the staleness but cost static
checking of the whole package namespace: a module ``__getattr__`` annotated
``-> Any`` tells mypy every attribute exists, so ``popoto.Modle`` stopped being
an ``attr-defined`` error.

#659 restores it by putting the hook in the ``else`` of an ``if TYPE_CHECKING``
and declaring the one dynamic name in the ``if``. The two halves are load-bearing
in opposite directions and neither is observable from behavior alone:

- The declaration must NOT bind at runtime. A bare annotation binds nothing and
  the branch never executes, so ``vars(popoto)`` stays clear and the hook still
  fires. Turning it into ``from .redis_db import POPOTO_REDIS_DB`` would silently
  restore #651 while leaving ``__init__.py`` itself error-free — the only static
  signal is ``no_implicit_reexport`` firing at a *consumer*'s site, and only once
  the package resolves at all — which it now does, since #663.
  ``tests/test_popoto_redis_db_rebind.py`` is what fails then.
- The hook must NOT be visible to the checker. Lifting it out of the ``else``
  breaks nothing at runtime; it only re-disables ``attr-defined``. No behavioral
  test can catch that, which is why this file asserts the source *shape* via
  ``ast`` rather than only the behavior.

**Do not retire this file now that #663 has landed.** It is tempting to read
#663 — which made ``mypy src/`` resolve popoto's own imports, so the gate finally
sees the package namespace — as the thing that makes these assertions redundant.
It is the opposite. #663 is what gives the guard its payoff, and it is *still*
blind to the guard's removal: measured under the resolving config, ``mypy src/``
reports the same total with the hook inside the ``else`` and with it lifted back
out, because a regression here removes error *reports* rather than adding any.
The ratchet is a ceiling, so a silent drop to zero package-namespace checking
passes it. These ``ast`` assertions are the only detector, permanently.

Never touches a database, so it names none.
"""

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import popoto

INIT_SOURCE = Path(popoto.__file__).resolve()


def _declares(node: ast.stmt, name: str) -> bool:
    """True if ``node`` is a bare annotation declaring ``name``."""
    return (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == name
    )


def _type_checking_guard() -> ast.If:
    """Return the module-level ``if TYPE_CHECKING:`` that declares the name.

    Deliberately tolerant of spelling and of company. ``if typing.TYPE_CHECKING:``
    is an ``ast.Attribute`` rather than an ``ast.Name`` and means exactly the same
    thing, so matching only one form would fail a legal refactor for the wrong
    reason. And a second, unrelated ``TYPE_CHECKING`` block added later is no
    business of this test — the guard is identified by what it declares, not by
    being the only one.
    """
    tree = ast.parse(INIT_SOURCE.read_text())
    guards = [
        node
        for node in tree.body
        if isinstance(node, ast.If)
        and (
            (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING")
            or (
                isinstance(node.test, ast.Attribute)
                and node.test.attr == "TYPE_CHECKING"
            )
        )
        and any(_declares(stmt, "POPOTO_REDIS_DB") for stmt in node.body)
    ]
    assert len(guards) == 1, (
        "expected exactly one module-level `if TYPE_CHECKING:` declaring "
        f"`POPOTO_REDIS_DB` in {INIT_SOURCE}, found {len(guards)}"
    )
    return guards[0]


def test_the_hook_is_hidden_from_type_checkers():
    """``__getattr__`` must be defined in the ``else``, never at module level.

    At module level the checker sees it and falls back to ``Any`` for every
    unknown attribute of the package, which is the regression #659 fixes.
    """
    guard = _type_checking_guard()

    in_else = [
        node.name
        for node in guard.orelse
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
    ]
    assert in_else == ["__getattr__"], (
        "`__getattr__` must be defined inside the `else` of `if TYPE_CHECKING:` "
        "so mypy never sees it (#659)"
    )

    tree = ast.parse(INIT_SOURCE.read_text())
    at_module_level = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "__getattr__"
    ]
    assert not at_module_level, (
        "`__getattr__` is visible to the type checker at module level; it must "
        "stay in the `else` branch (#659)"
    )


def test_the_declaration_is_an_annotation_not_an_import():
    """The ``if`` branch must declare the name, not bind it.

    An import here would shadow the hook permanently at runtime and restore #651
    in full, with no error raised at this file — ``no_implicit_reexport`` only
    speaks up at a consumer's site, and only once the package resolves.
    """
    # The locator already required the declaration to exist — it is how the
    # guard is identified. What is left to check is that it stays a *bare*
    # annotation: a value would bind the name, and binding is the bug.
    guard = _type_checking_guard()

    for node in guard.body:
        if _declares(node, "POPOTO_REDIS_DB"):
            assert isinstance(node, ast.AnnAssign) and node.value is None, (
                "`POPOTO_REDIS_DB` must be annotated, never assigned — a value "
                "here binds the name and reintroduces #651"
            )

    bound = [
        alias.asname or alias.name
        for node in guard.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    ]
    assert "POPOTO_REDIS_DB" not in bound, (
        "importing `POPOTO_REDIS_DB` — even under `if TYPE_CHECKING:` — is the "
        "binding that shadows the hook and restores #651"
    )


def test_the_hook_still_fires_at_runtime():
    """The guard must not have taken the hook out of circulation.

    ``TYPE_CHECKING`` is ``False`` at runtime, so the ``else`` executes and the
    ``if`` branch's annotation binds nothing.
    """
    assert TYPE_CHECKING is False

    assert "__getattr__" in vars(
        popoto
    ), "the `else` branch did not execute — `popoto.__getattr__` is missing"
    assert "POPOTO_REDIS_DB" not in vars(popoto), (
        "the `TYPE_CHECKING` declaration bound the name at runtime; it must "
        "remain a bare annotation in a branch that never executes (#659)"
    )

    # The hook is what answers, and it answers with the live client.
    from popoto import redis_db

    assert popoto.__getattr__("POPOTO_REDIS_DB") is redis_db.get_REDIS_DB()
    assert popoto.POPOTO_REDIS_DB is redis_db.get_REDIS_DB()
