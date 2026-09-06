"""TDValueField — a Decimal column with an atomic TD(0) update.

Stores a learned value (a Q-value in reinforcement-learning terms) in the
model hash exactly as :class:`~popoto.fields.shortcuts.DecimalField` does, and
adds one operation the ordinary save path cannot express: an atomic
read-modify-write of that single hash field under the temporal-difference
rule

    Q(s,a) <- Q(s,a) + alpha * [r + gamma * max Q(s',a') - Q(s,a)]

The read and the write happen inside one Lua script, so concurrent updates to
the same instance serialize at the server rather than racing through a
client-side ``HGET`` / compute / ``HSET`` round trip.

This field was extracted from ``popoto.recipes.policy_cache``, which owned the
script directly (issue #647, part of the #630 series). A recipe composes
primitives; it should not own one.

Storage:
    The value lives in the model hash under the field's own name, encoded as
    the ``__Decimal__`` tagged dict every ``DecimalField`` uses. Nothing is
    stored outside the hash. This matters beyond tidiness:
    :class:`~popoto.fields.decaying_sorted_field.DecayingSortedField` reads a
    ``base_score_field`` straight out of the member's hash in Lua and falls
    back to ``1.0`` for any encoding it does not recognize, so the encoding
    must stay exactly what ``DecimalField`` writes.

Valkey Compatibility:
    Core commands only — ``HGET``, ``HSET``, and ``cmsgpack`` inside the
    script. No Redis-module commands (no ``BF.``, ``CMS.``, ``TOPK.``,
    ``TS.``, ``JSON.``), so this field works identically on Redis and Valkey.

Example:
    from decimal import Decimal
    from popoto.fields.td_value_field import TDValueField

    class Policy(Model):
        agent_id = KeyField()
        q_value = TDValueField(default=Decimal("0"))

    policy = Policy(agent_id="agent-1")
    policy.save()
    td_error = TDValueField.td_update(policy, "q_value", reward=1.0)
"""

from typing import Any, Optional

import redis

from .. import redis_db
from ..redis_db import run_lua
from .constants import Defaults
from .shortcuts import DecimalField

# ---------------------------------------------------------------------------
# Lua Scripts
# ---------------------------------------------------------------------------

# NOTE: this script is moved verbatim from popoto.recipes.policy_cache (#647).
# `lua_script()` caches the registered Script object keyed by the exact script
# text, so any edit here — including a comment — changes the SHA and the
# EVALSHA payload. Do not reformat it. The Valkey-compatibility note for this
# script lives in the module docstring above, deliberately outside the string
# literal.
TD_UPDATE_LUA = """
-- td_update.lua: Temporal difference Q-value update
-- KEYS[1] = model hash key (instance redis_key, e.g. PolicyEntry:agent-1:fp:action)
-- ARGV[1] = reward (float)
-- ARGV[2] = alpha (learning rate)
-- ARGV[3] = gamma (discount factor)
-- ARGV[4] = max_future_q (float)
--
-- Q-value is stored in the model hash under field "q_value" as a
-- cmsgpack-encoded __Decimal__ tagged dict (byte-interchangeable with
-- Python msgpack encoding of Decimal).
--
-- Returns: td_error as string

local hash_key = KEYS[1]
local reward = tonumber(ARGV[1])
local alpha = tonumber(ARGV[2])
local gamma = tonumber(ARGV[3])
local max_future_q = tonumber(ARGV[4])

-- Read current Q from model hash
local current_q = 0
local raw = redis.call('HGET', hash_key, 'q_value')
if raw then
    local ok, decoded = pcall(cmsgpack.unpack, raw)
    if ok and type(decoded) == 'number' then
        current_q = decoded
    elseif ok and type(decoded) == 'table' and decoded['as_encodable'] then
        -- __Decimal__ tagged dict encoding
        current_q = tonumber(decoded['as_encodable']) or 0
    end
end

local td_error = reward + gamma * max_future_q - current_q
local new_q = current_q + alpha * td_error

-- Write new Q as __Decimal__ tagged dict (byte-interchangeable with Python msgpack Decimal)
local encoded = cmsgpack.pack({['__Decimal__']=true, ['as_encodable']=tostring(new_q)})
redis.call('HSET', hash_key, 'q_value', encoded)
return tostring(td_error)
"""

# The hash field name is hard-coded as 'q_value' inside the script above.
# Parameterizing it would change the script text, hence the SHA, hence the
# on-wire payload — which is precisely what the #647 relocation promises not to
# do. `td_update` therefore refuses any other field name rather than silently
# updating the wrong column.
TD_UPDATE_HASH_FIELD = "q_value"


class TDValueField(DecimalField):
    """A ``Decimal`` column supporting atomic temporal-difference updates.

    Declares no storage of its own and takes no extra constructor arguments —
    it is a ``DecimalField`` in every respect except that it carries the
    :meth:`td_update` operation. The learning rate and discount factor are
    call-time arguments of that method, not field-level configuration: they are
    experimental tuning constants (see
    :class:`popoto.fields.constants.Defaults`), and field-declared defaults
    would let two processes disagree about the gain schedule with no runtime
    detection.

    Note:
        The field must be named ``q_value`` on the model for :meth:`td_update`
        to work; see :data:`TD_UPDATE_HASH_FIELD`.
    """

    @classmethod
    def td_update(
        cls,
        model_instance: Any,
        field_name: str,
        *,
        reward: float,
        max_future_q: float = 0.0,
        alpha: float = Defaults.TD_ALPHA,
        gamma: float = Defaults.TD_GAMMA,
        pipeline: Optional["redis.client.Pipeline"] = None,
    ) -> Optional[float]:
        """Apply one TD(0) update to this instance's value, atomically.

        The read-modify-write happens server-side in a single script
        invocation, so two concurrent calls on the same instance serialize
        rather than losing an update.

        Note:
            The in-memory attribute is **not** refreshed. The script returns
            the TD error only; it does not return the new value, and neither
            recomputing it client-side nor re-reading it is acceptable (the
            first duplicates the arithmetic and encoding this field exists to
            own, the second adds a command). Reload the instance to observe the
            new value.

        Note:
            When ``pipeline`` is given the update is queued on it and this
            method returns ``None``; the TD error is available in that
            pipeline's results after ``execute()``.

        Args:
            model_instance: A saved model instance carrying a TDValueField.
            field_name: Name of the TDValueField on the model. Must be
                ``q_value``.
            reward: Observed reward signal.
            max_future_q: Maximum value for the next state's best action.
                Default 0.0 (terminal — no future state).
            alpha: Learning rate. Defaults to ``Defaults.TD_ALPHA``.
            gamma: Discount factor. Defaults to ``Defaults.TD_GAMMA``.
            pipeline: Optional Redis pipeline to queue the update on.

        Returns:
            float: The TD error — positive when the outcome beat the current
            estimate, negative when it fell short. ``None`` when queued on a
            pipeline.

        Raises:
            ValueError: If the instance is unsaved, or ``field_name`` is not a
                TDValueField named ``q_value``.
        """
        if field_name != TD_UPDATE_HASH_FIELD:
            raise ValueError(
                f"td_update() operates on the '{TD_UPDATE_HASH_FIELD}' hash "
                f"field, which the Lua script names directly; got "
                f"'{field_name}'"
            )

        field = model_instance._meta.fields.get(field_name)
        if not isinstance(field, TDValueField):
            raise ValueError(f"{field_name} is not a TDValueField")

        # Saved-instance guard. Deliberately identical to the guard this
        # replaced (policy_cache._get_redis_key): attribute or db_key lookup,
        # raising ValueError, and NO extra round trip. ConfidenceField's
        # equivalent adds an EXISTS probe; copying that here would put a new
        # command in the wire sequence this relocation must leave untouched.
        redis_key = getattr(model_instance, "_redis_key", None)
        if not redis_key:
            try:
                redis_key = model_instance.db_key.redis_key
            except Exception:
                raise ValueError("Cannot operate on unsaved PolicyEntry")

        # Client resolved at call time (module attribute, not an import-time
        # binding) so a rebound connection or a test spy still intercepts.
        client = pipeline if pipeline is not None else redis_db.POPOTO_REDIS_DB

        td_error = run_lua(
            client,
            TD_UPDATE_LUA,
            1,  # num keys
            redis_key,  # KEYS[1] — model hash key
            str(reward),  # ARGV[1]
            str(alpha),  # ARGV[2]
            str(gamma),  # ARGV[3]
            str(max_future_q),  # ARGV[4]
        )

        if pipeline is not None:
            return None

        return float(td_error)
