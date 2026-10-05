"""Recipes drive Redis through the field and model layers only (#630).

A recipe composes field primitives (a ``DecayingSortedField`` index,
``AccessTrackerMixin`` staging, the model's own persistence). Reaching past
them to the raw client bakes key layout and hook ordering into the recipe,
which then breaks silently when either changes. This is the issue's
acceptance grep as a test: each listed recipe source contains no reference to
``POPOTO_REDIS_DB`` or ``run_lua``.

PRs 2-4 of #630 append their recipe to ``FIELD_LAYER_RECIPES`` as each is
migrated.
"""

import os

import pytest

# Backend conformance (#759 M4, plan §5 M4 gate (b)): this module asserts
# recipe *source*, not storage, so both legs run the same scan. It is in M4's
# gate (b) list because what it pins -- a recipe reaches storage only through
# the field and model layers -- is what lets an unchanged recipe run on a
# Postgres-bound model.
pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]

RECIPES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "src",
    "popoto",
    "recipes",
)

FIELD_LAYER_RECIPES = [
    "default_memory.py",
]

FORBIDDEN = ["POPOTO_REDIS_DB", "run_lua"]


@pytest.mark.parametrize("recipe", FIELD_LAYER_RECIPES)
@pytest.mark.parametrize("token", FORBIDDEN)
def test_recipe_has_no_direct_client_reference(recipe, token):
    path = os.path.join(RECIPES_DIR, recipe)
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    offending = [
        f"{recipe}:{lineno}: {line.strip()}"
        for lineno, line in enumerate(source.splitlines(), start=1)
        if token in line
    ]
    assert not offending, "\n".join(offending)
