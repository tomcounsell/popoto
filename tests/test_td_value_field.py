"""``TDValueField``: the atomic TD(0) update, owned by the field (#647).

Until #647 the script behind this update lived in
``popoto.recipes.policy_cache`` and ran through a direct ``run_lua`` call on
the shared client. These tests pin the properties that made the move safe:
the field is reusable outside the recipe, the saved-instance guard costs zero
Redis commands (an extra probe would change the wire sequence the relocation
promises to preserve), the pipeline branch queues instead of executing, and
the recipe still re-exports the script object itself.
"""

import os
import sys
import uuid
from decimal import Decimal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402
from src import popoto  # noqa: E402
from src.popoto.fields import td_value_field  # noqa: E402
from src.popoto.fields.shortcuts import DecimalField  # noqa: E402
from src.popoto.fields.td_value_field import TDValueField  # noqa: E402
from src.popoto.recipes import policy_cache  # noqa: E402


class Bandit(popoto.Model):
    """A model that is not PolicyEntry, to prove the field is a primitive."""

    arm_id = popoto.KeyField()
    q_value = TDValueField(default=Decimal("0"))


def test_field_is_a_decimal_field():
    """The encoding contract: DecayingSortedField reads this column out of the
    model hash in Lua and falls back to 1.0 for anything it cannot decode, so
    the field must keep DecimalField's wire format."""
    assert issubclass(TDValueField, DecimalField)
    field = Bandit._meta.fields["q_value"]
    assert field.type is Decimal


def test_td_update_works_on_a_model_that_is_not_policy_entry():
    bandit = Bandit(arm_id=f"arm-{uuid.uuid4().hex[:8]}")
    bandit.save()

    td_error = TDValueField.td_update(bandit, "q_value", reward=1.0)

    # First update from Q=0: td_error == reward, new Q == alpha * reward.
    assert td_error == pytest.approx(1.0)
    reloaded = Bandit.query.filter(arm_id=bandit.arm_id)[0]
    assert reloaded.q_value == pytest.approx(Decimal("0.1"))


def test_underivable_key_raises_value_error_and_issues_no_commands(monkeypatch):
    """The guard is deliberately not ConfidenceField's: that one adds an EXISTS
    round trip and raises TypeError. Either change would be visible in a
    command capture, so both halves are asserted here.

    Note the guard's real reach, unchanged from the code this replaced
    (``policy_cache._get_redis_key``): it fires only when no key can be
    *derived*, not when the record is merely absent from Redis. An instance
    that was never saved but whose key fields resolve has a derivable key and
    proceeds — that was true before #647 and is preserved deliberately.
    """
    bandit = Bandit(arm_id=f"arm-{uuid.uuid4().hex[:8]}")
    # Reaching the guard takes contrivance, which is itself the finding: an
    # instance caches ``_redis_key`` at construction, so the ValueError branch
    # is defensive rather than a routine unsaved-instance rejection. Clear the
    # cache and make derivation fail to exercise it.
    bandit._redis_key = None

    def explode(self):
        raise RuntimeError("no key")

    monkeypatch.setattr(type(bandit), "db_key", property(explode))

    commands = []
    original_execute = td_value_field.redis_db.POPOTO_REDIS_DB.execute_command

    def spy(*args, **kwargs):
        commands.append(args[0] if args else None)
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(td_value_field.redis_db.POPOTO_REDIS_DB, "execute_command", spy)

    with pytest.raises(ValueError):
        TDValueField.td_update(bandit, "q_value", reward=1.0)

    assert commands == []


def test_td_update_queues_on_a_pipeline_and_applies_on_execute():
    bandit = Bandit(arm_id=f"arm-{uuid.uuid4().hex[:8]}")
    bandit.save()

    pipe = popoto.batch()
    try:
        queued = TDValueField.td_update(bandit, "q_value", reward=1.0, pipeline=pipe)
        assert queued is None

        # Nothing applied yet.
        pending = Bandit.query.filter(arm_id=bandit.arm_id)[0]
        assert pending.q_value == pytest.approx(Decimal("0"))

        results = pipe.execute()
    finally:
        pipe.reset()

    assert float(results[-1]) == pytest.approx(1.0)
    applied = Bandit.query.filter(arm_id=bandit.arm_id)[0]
    assert applied.q_value == pytest.approx(Decimal("0.1"))


def test_wrong_field_name_is_refused_rather_than_writing_the_wrong_column():
    """The script names the 'q_value' hash field directly; parameterizing it
    would change the script text and its SHA."""
    bandit = Bandit(arm_id=f"arm-{uuid.uuid4().hex[:8]}")
    bandit.save()

    with pytest.raises(ValueError):
        TDValueField.td_update(bandit, "arm_id", reward=1.0)


def test_recipe_reexports_the_same_script_object():
    """Downstream code and the recipe guide import TD_UPDATE_LUA from
    policy_cache; the move must not silently drop the name."""
    assert policy_cache.TD_UPDATE_LUA is td_value_field.TD_UPDATE_LUA
