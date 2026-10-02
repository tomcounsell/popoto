"""
Tag Field - Optional Multi-Value Scoping via Redis Sets
=======================================================

This module provides ``TagFieldMixin``, the multi-value generalization of
:class:`IndexedFieldMixin`. Where an indexed field maps one record to exactly
one value-Set, a tag field maps one record to *many* value-Sets at once — one
per tag value — so a record can belong to several optional scopes simultaneously
(or to none at all).

Motivation
----------
A centrally hosted Redis/Valkey may serve many agents. A memory can be scoped by
the agent it belongs to (``agent:valor``), a relevant project (``project:popoto``),
or arbitrary bare tags the agents agree on — and **every scoping dimension is
optional**. KeyField partitioning cannot express this: a partition value becomes
part of the record's identity and is required at query time. Tags are *metadata,
not identity* — a record with zero tags transparently lives in the shared pool and
is returned by unscoped queries.

Design Philosophy
-----------------
- **Convention over schema.** popoto stays agnostic about which dimensions exist.
  Agents cooperate on prefixes (``agent:``, ``project:``) or use bare tags; the
  field imposes no schema on tag namespaces.
- **Not a security boundary.** Tags are cooperative scoping between trusted agents,
  not access control. Nothing here enforces isolation — a query with the right tag
  filter can read any tagged record.
- **Valkey-safe.** Index maintenance and queries use only core Redis Set commands
  (``SADD``/``SREM``/``SMEMBERS``/``SUNION``/``SINTER``/``DEL``) — no Redis modules.

Index Structure
---------------
Per tag value, a Redis Set of instance keys::

    $TagF:ModelName:field_name:tag_value -> Set of redis_keys

(The ``$TagF`` prefix is auto-derived by the ``FieldBase`` metaclass from the class
name ``TagField``.) Tag values containing the ``:`` DB_key separator — e.g.
``agent:valor`` — are escaped by ``DB_key.clean`` (``agent{&#58;}valor``), so
convention prefixes never collide with the key structure.

Atomic multi-value maintenance
------------------------------
Because a record belongs to N Sets at once, index maintenance rides the
backend's atomic :meth:`~popoto.backends.Backend.swap_tags` — on Redis a
dedicated Lua script, :data:`TAG_SWAP_LUA`, that *diffs* the record's previous
tag membership against the new one and issues only the necessary
``SREM``/``SADD`` calls, all inside a single server-side ``EVAL`` (no
cross-process race, no orphaned members). The previous membership is read from
a server-authoritative **pointer side key** (a standalone Redis Set of the
value-Set keys the record currently belongs to), never from a client-side
snapshot — the same #476 lesson that made :data:`INDEX_SWAP_LUA` use a side
key instead of an in-hash pointer. Since #631 WS1c the pointer keys are
derived inside :class:`popoto.backends.redis.RedisBackend`; this module names
only the record key, the field and the new per-tag index keys.

``TagFieldMixin`` subclasses :class:`IndexedFieldMixin` so that
``isinstance(field, IndexedFieldMixin)`` remains true: this makes ``Model.save()``
(a) exclude the tag field from the plain HSET mapping — the tag swap owns the
hash write — and (b) run the field eagerly on its own atomic swap before the
surrounding pipeline, closing the same unique-conflict window as #476. All four
hook methods are fully overridden for multi-value semantics.

Usage
-----
    from popoto import Model, AutoKeyField, TagField

    class Memory(Model):
        key = AutoKeyField()
        tags = TagField()          # optional; zero tags == shared pool

    Memory.create(tags=["agent:valor", "project:popoto"])
    Memory.create()               # untagged — lives in the shared pool

    Memory.query.filter(tags__contains="agent:valor")     # membership
    Memory.query.filter(tags__any=["agent:a", "agent:b"]) # OR  (SUNION)
    Memory.query.filter(tags__all=["agent:valor",
                                   "project:popoto"])      # AND (SINTER)
"""

import logging
from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING, Any, overload

import msgpack

from ..backends import get_backend
from ..exceptions import ModelException, QueryException
from ..models.db_key import DB_key
from .indexed_field_mixin import IndexedFieldMixin, _members_as_bytes

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ..models.base import Model

logger = logging.getLogger("POPOTO.TagFieldMixin")


class _LazyFallbackIdxs(Sequence[str]):
    """Fallback index keys that are built on first use, not at construction.

    ``Backend.drop_tag_entries`` takes ``fallback_idxs`` as a ``Sequence[str]``
    and consults it only after the pointer side key came back empty. Before
    the seam, ``on_delete`` computed those keys inside that same no-pointer
    branch, so ``_normalize(field_value)`` -- which raises ``ModelException``
    on a non-list value -- never ran while a pointer existed. Building the
    list eagerly in the mixin moved that raise onto every delete (#744 review,
    B1). This wrapper keeps base's order through the unchanged signature:
    ``bool()``/``len()``/iteration call ``build`` once and cache the result.
    """

    __slots__ = ("_build", "_items")

    def __init__(self, build: Callable[[], list[str]]) -> None:
        self._build = build
        self._items: list[str] | None = None

    def _materialize(self) -> list[str]:
        if self._items is None:
            self._items = self._build()
        return self._items

    def __len__(self) -> int:
        return len(self._materialize())

    def __iter__(self) -> Iterator[str]:
        return iter(self._materialize())

    @overload
    def __getitem__(self, index: int) -> str: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[str]: ...

    def __getitem__(self, index: int | slice) -> str | Sequence[str]:
        return self._materialize()[index]


# ``TAG_SWAP_LUA`` moved to ``popoto.backends.redis`` (#631 WS0).
# It is re-imported here under its existing name so every current
# reader -- the tests that import it from here, and
# ``tests/test_transfer_roundtrip.py``'s source scan -- keeps finding it.
# The script text itself is byte-identical; since WS1c this module no
# longer runs it, ``RedisBackend.swap_tags`` does.
from ..backends.redis import TAG_SWAP_LUA  # noqa: E402,F401


class TagFieldMixin(IndexedFieldMixin):
    """
    Mixin that provides optional, indexed, multi-value scoping via Redis Sets.

    A record may carry any number of tag values (or none). For each tag value a
    Redis Set tracks the instance keys carrying it, enabling membership / any-of
    (``SUNION``) / all-of (``SINTER``) filtering that composes with the rest of
    ``Query.filter()`` through the same set-intersection pipeline.

    Subclasses :class:`IndexedFieldMixin` purely so ``Model.save()`` treats it as
    an atomic-index field (HSET exclusion + eager swap); all behavior is
    overridden for multi-value semantics. Tags are never part of the record's
    Redis key.

    Attributes:
        tag (bool): Always True for tag fields.
    """

    tag: bool = True

    # Export/import: the per-tag index Sets and pointer are fully rebuilt
    # from the field's tag list value by on_save(), so nothing needs to be
    # carried across export/import.
    roundtrip_policy: str = "rebuild"

    # The two key builders below are the Redis key layout the backend derives
    # for itself (``popoto.backends.redis._tag_ptr_key`` and its pre-#540
    # twin). This module no longer passes them to anything; they stay as
    # documented, test-visible helpers (``tests/test_tag_field.py`` and
    # ``tests/test_issue_540_*`` inspect these keys) and must stay byte-equal
    # to the backend's builders.

    @staticmethod
    def _tag_pointer_side_key(model_hash_key: str, field_name: str) -> str:
        """Standalone Redis SET key holding this record's current value-Set keys.

        Not a field inside the model hash (see #476), and namespaced under
        ``$TagPtr:`` so it sits outside every model's key space (#540) — a
        ``\\x00`` in the name does NOT keep it out of a glob, since Redis
        ``*`` matches any byte including NUL. See
        ``IndexedFieldMixin._pointer_side_key`` for the full rationale.
        """
        return f"$TagPtr:{model_hash_key}:{field_name}"

    @staticmethod
    def _pre_540_tag_pointer_side_key(model_hash_key: str, field_name: str) -> str:
        """The 1.8.1/1.8.2 tag pointer side key. Read-only migration fallback.

        Never written by current code; read once and then DEL'd so records
        self-heal off the colliding key space.
        """
        return f"{model_hash_key}\x00tagptr\x00{field_name}"

    @staticmethod
    def _normalize(field_value) -> list:
        """Normalize a tag value to a sorted list of unique string tags.

        Accepts ``None`` (→ untagged, ``[]``) or any ``list``/``set``/``tuple`` of
        scalar tags. Duplicates collapse; order is deterministic (sorted) so the
        serialized form and the derived index keys are stable across saves.

        Raises:
            ModelException: if the value is not an iterable of scalars.
        """
        if field_value is None:
            return []
        if isinstance(field_value, (list, set, tuple)):
            items = field_value
        else:
            raise ModelException(
                "TagField value must be a list/set/tuple of tags, "
                f"got {type(field_value).__name__}"
            )
        tags = set()
        for tag in items:
            if tag is None:
                continue
            if not isinstance(tag, (str, int, float, bool)):
                raise ModelException(
                    f"TagField tag must be a scalar (str/int/float/bool), "
                    f"got {type(tag).__name__}"
                )
            tags.add(str(tag))
        return sorted(tags)

    @classmethod
    def is_valid(cls, field, value, null_check=True, **kwargs) -> bool:
        """Validate that a value is an optional collection of scalar tags."""
        if value is None:
            return (not null_check) or bool(field.null)
        if not isinstance(value, (list, set, tuple)):
            logger.error(
                f"field {field} expects a list/set/tuple of tags, "
                f"got {type(value).__name__}"
            )
            return False
        for tag in value:
            if tag is not None and not isinstance(tag, (str, int, float, bool)):
                logger.error(f"field {field} tag must be a scalar, got {type(tag)}")
                return False
        return True

    def format_value_pre_save(self, field_value, **kwargs):
        """Normalize to a sorted unique list before serialization.

        Model.save() writes this result back onto the instance before encoding,
        so the model hash (and any in-memory reads after save) see a stable list
        even if the caller assigned a ``set`` or ``tuple``.
        """
        return type(self)._normalize(field_value)

    @classmethod
    def on_save(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value,
        pipeline: Any = None,  # duck-typed; see IndexedFieldMixin's note
        **kwargs,
    ):
        """Atomically diff-and-update the per-tag Sets via the backend's
        ``swap_tags`` (:data:`TAG_SWAP_LUA` on Redis).

        Internal path (no pipeline): runs the swap now — atomic on the
        server, eager (before the surrounding save pipeline) exactly like
        IndexedFieldMixin. External path: queues the swap on the caller's
        unit of work so the hash write and index update commit together in
        one MULTI/EXEC.
        """
        tags = cls._normalize(field_value)
        member_key = model_instance.db_key.redis_key
        prefix = cls.get_special_use_field_db_key(model_instance, field_name)
        new_set_keys = [DB_key(prefix, tag).redis_key for tag in tags]
        new_bytes = msgpack.packb(tags)

        backend = get_backend()
        if pipeline is not None:
            backend.swap_tags(
                member_key, field_name, new_set_keys, new_bytes, uow=pipeline
            )
            return pipeline
        return backend.swap_tags(member_key, field_name, new_set_keys, new_bytes)

    @classmethod
    def on_delete(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value,
        pipeline: Any = None,  # duck-typed; see IndexedFieldMixin's note
        **kwargs,
    ):
        """Remove the record from every tag Set it belongs to, then drop the pointer.

        The backend's ``drop_tag_entries`` reads the authoritative membership
        from the pointer side key (still present because Model.delete() runs
        field hooks before the hash DELETE). It falls back to the field-value
        derived keys passed here if the pointer is somehow absent (legacy /
        partial state). Those keys are handed over lazily: ``_normalize``
        rejects a non-list value, and as before the seam that must only be
        reachable on the no-pointer path -- a record whose in-memory ``tags``
        was reassigned to ``"oops"`` and never saved still deletes cleanly
        while its pointer exists.
        """
        member_key = kwargs.get("saved_redis_key", model_instance.db_key.redis_key)

        def _fallback_idxs() -> list[str]:
            # Fallback: derive Set keys from the field value if no pointer
            # exists. Runs only when the backend finds no pointer.
            if not field_value:
                return []
            prefix = cls.get_special_use_field_db_key(model_instance, field_name)
            return [
                DB_key(prefix, tag).redis_key for tag in cls._normalize(field_value)
            ]

        fallback_idxs = _LazyFallbackIdxs(_fallback_idxs)

        backend = get_backend()
        if pipeline is not None:
            backend.drop_tag_entries(
                member_key, field_name, fallback_idxs=fallback_idxs, uow=pipeline
            )
            return pipeline
        return backend.drop_tag_entries(
            member_key, field_name, fallback_idxs=fallback_idxs
        )

    def get_filter_query_params(self, field_name: str) -> set:
        """Valid tag lookups: membership / any-of / all-of.

        Deliberately does NOT inherit IndexedFieldMixin's single-value lookups
        (exact match, ``__in``, ``__startswith`` ...): the stored value is a list,
        so exact-match on it is meaningless. Absent any of these params, a query
        never routes to this field and results stay unscoped (shared pool).
        """
        return {
            f"{field_name}",  # bare exact-match: intercepted to raise (see below)
            f"{field_name}__contains",  # membership: SMEMBERS(one Set)
            f"{field_name}__any",  # OR over values: SUNION
            f"{field_name}__all",  # AND over values: SINTER
        }

    @classmethod
    def filter_query(cls, model: "Model", field_name: str, **query_params) -> set:
        """Resolve tag lookups to matching Redis keys via plain set-index reads.

        - ``__contains=v`` → ``index_members`` of the single value Set.
        - ``__any=[...]``  → ``index_union`` of the value Sets (OR).
        - ``__all=[...]``  → ``index_intersection`` of the value Sets (AND).

        Multiple tag params AND-intersect client-side, consistent with the rest of
        ``filter_for_keys_set``. Empty any/all lists yield an empty match (no crash).
        Returns the keys as ``bytes`` (see ``_members_as_bytes``).
        """
        prefix = model._meta.fields[field_name].get_special_use_field_db_key(
            model, field_name
        )
        keys_lists_to_intersect = list()
        backend = get_backend()

        for query_param, query_value in query_params.items():
            if query_param == field_name:
                # Bare exact-match on a multi-value tag list is ambiguous and
                # would otherwise degrade to a client-side list-equality compare.
                # Route users to the explicit membership lookups instead.
                raise QueryException(
                    f"Exact-match filter on TagField '{field_name}' is not "
                    f"supported; use {field_name}__contains (membership), "
                    f"{field_name}__any (any-of) or {field_name}__all (all-of)."
                )
            if query_param.endswith("__contains"):
                keys_lists_to_intersect.append(
                    _members_as_bytes(
                        backend.index_members(DB_key(prefix, query_value).redis_key)
                    )
                )
            elif query_param.endswith("__any"):
                set_keys = [DB_key(prefix, v).redis_key for v in query_value]
                keys_lists_to_intersect.append(
                    _members_as_bytes(backend.index_union(set_keys))
                    if set_keys
                    else set()
                )
            elif query_param.endswith("__all"):
                set_keys = [DB_key(prefix, v).redis_key for v in query_value]
                keys_lists_to_intersect.append(
                    _members_as_bytes(backend.index_intersection(set_keys))
                    if set_keys
                    else set()
                )

        if keys_lists_to_intersect:
            return set.intersection(
                *[set(key_list) for key_list in keys_lists_to_intersect]
            )
        return set()
