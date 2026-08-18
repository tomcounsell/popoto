"""AppendOnlyMixin — write-once records with no in-place mutation (#560).

A model that composes this mixin accepts exactly one write per Redis key and
refuses every delete. Corrections are expressed as *new* records that point at
the old ones, never as edits. The provenance journal
(:mod:`popoto.recipes.provenance_journal`) is the first consumer, but nothing
here is journal-specific: any model that wants write-once semantics composes
it::

    class Ledger(AppendOnlyMixin, Model):
        entry_id = AutoKeyField()
        amount = FloatField()

    e = Ledger(amount=1.0).save()
    e.amount = 2.0
    e.save()          # AppendOnlyViolation
    e.delete()        # AppendOnlyViolation
    Ledger.delete_all()  # AppendOnlyViolation (routes through instance.delete)

What the guard actually checks
-----------------------------
``EXISTS`` on ``self.db_key.redis_key``, read from ``POPOTO_REDIS_DB``
**directly**. Two things this is deliberately *not*:

* It is not ``self._db_content`` and not ``self._saved_field_values``. Both are
  empty in cases the guard must catch. ``_db_content`` is empty on a
  query-loaded instance (``base.py``'s ``_is_create`` check is
  EventStream-specific and is not a persisted-ness signal), and
  ``_saved_field_values`` is empty on a fresh Python object whose key collides
  with a stored record — which is exactly the shape a retry or a duplicate
  ingest takes.
* It is never read from a caller-supplied pipeline. An ``EXISTS`` queued on a
  pipeline returns the ``Pipeline`` object, which is always truthy, so a
  pipelined guard would refuse every save including the first.

Two refusals the ``EXISTS`` check cannot express, both unconditional:

* ``save(migrate_key=True)``. A key migration makes ``EXISTS`` on the *new* key
  return 0, so the guard would pass — and ``Model.save()`` then ``DELETE``s the
  old key. That is a destroy through a supported public kwarg.
* A set ``obsolete_redis_key``, which is the same migration reached by mutating
  a ``KeyField`` on an already-saved instance.

The boundary, stated rather than papered over
---------------------------------------------
**Immutability here is an ORM-layer contract, not a storage guarantee.** It
holds against every Python write path in ``models/base.py`` — ``save``,
``create``, ``get_or_create``, ``update_or_create``, both bulk-save sites,
``delete``, and ``delete_all`` (which routes through ``instance.delete()`` per
instance). It does **not** hold against a raw Redis client, and the repo's own
migration cookbook (``models/migrations.py``) teaches a ``delete`` + re-``hset``
recipe that bypasses it by construction. Redis and Valkey have no per-key
write-once mode; ``SETNX``/``HSETNX`` are the only atomic create-if-absent
primitives and neither covers a multi-field ``HSET`` plus the index writes a
Popoto model performs.

Two known TOCTOU shapes, neither claimed as closed:

1. **Cross-process.** Two writers save the same key concurrently; both
   ``EXISTS`` calls return 0 before either ``HSET`` lands, so the second
   silently overwrites the first. Structurally narrowed rather than locked: an
   ``AutoKeyField`` identity means two independent appends cannot collide, so
   the window is only reachable when a caller supplies an explicit colliding
   key — a programming error the guard still catches in every non-concurrent
   case.
2. **Intra-pipeline.** Two saves of the same key queued onto *one* pipeline.
   The guard's ``EXISTS`` executes immediately against ``POPOTO_REDIS_DB`` and
   cannot see a command that is queued but not yet executed, so both pass and
   the second overwrites. This shape needs no concurrency and is
   deterministically reproducible.

Closing either shape at the storage layer would mean an ``HSETNX``-based write
path that duplicates ``Model.save()``'s index handling. That is a rabbit hole,
not a follow-up: the boundary is documented instead.

Transfer/export
---------------
``roundtrip_policy = "rebuild"`` — the mixin owns no Redis state of its own.
``on_conflict="overwrite"`` is **unsupported** on append-only models:
``transfer/import_.py`` calls ``instance.save()``, so every colliding record
raises :class:`~popoto.exceptions.AppendOnlyViolation` and is classified
``ERRORED``. ``"skip"`` is the supported conflict mode.

Retention escape hatch
----------------------
:meth:`AppendOnlyMixin.hard_delete` is a named, greppable classmethod rather
than a ``skip_append_only=True`` kwarg (which would require changing
``Model.save()``'s signature). It exists for retention and erasure, not for
test teardown — ``popoto.pytest_plugin`` already flushes the test DB before
every test. It sweeps derived state as well as the record hash; a
``hard_delete`` that left index or chain state behind would be worse than no
escape hatch, because it manufactures orphan index members pointing at a
nonexistent hash.
"""

from typing import TYPE_CHECKING, Any, Optional, Union

from ..exceptions import AppendOnlyViolation
from ..redis_db import POPOTO_REDIS_DB, scan_keys

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from redis.client import Pipeline


def _as_str(value: Any) -> str:
    """Decode a Redis reply element to ``str`` (bytes or str in, str out)."""
    return value.decode() if isinstance(value, bytes) else str(value)


class AppendOnlyMixin:
    """Model mixin enforcing write-once records and refusing deletes.

    Compose it ahead of ``Model`` (and ahead of any other save-gating mixin
    whose work should not run for a write that is going to be refused)::

        class JournalEntry(AppendOnlyMixin, NeverRecordMixin, Model):
            ...

    Raises:
        AppendOnlyViolation: From :meth:`save` when the record's Redis key
            already exists, when ``migrate_key=True``, or when
            ``obsolete_redis_key`` is set; and unconditionally from
            :meth:`delete`.
    """

    # Export/import: this mixin maintains no Redis state of its own -- it only
    # refuses writes. Nothing to carry across a round trip. (Declaring the
    # policy is not optional: the mixin lives in ``src/popoto/fields/``, is
    # named ``*Mixin`` and references ``POPOTO_REDIS_DB``, which is exactly the
    # collector predicate behind
    # ``test_every_model_level_mixin_has_a_policy_declared``.)
    roundtrip_policy: str = "rebuild"

    if TYPE_CHECKING:
        # Supplied by Model, which every user of this mixin also inherits
        # from. Declared for the type checker only.
        _meta: Any
        db_key: Any
        obsolete_redis_key: Optional[str]

    def save(
        self,
        pipeline: Optional["Pipeline"] = None,
        ignore_errors: bool = False,
        skip_auto_now: bool = False,
        update_fields: Optional[list[str]] = None,
        migrate_key: bool = False,
        skip_write_filter: bool = False,
        **kwargs: Any,
    ) -> Union["Pipeline", int, bool]:
        """Persist the record, but only if its Redis key is not already taken.

        Runs before ``Model.save()``'s own gates (this mixin is first in the
        MRO), so a refused write never reaches the never-record scan, the write
        filter, ``pre_save``, or any index. The ordering is MRO-determined and
        harmless in the other direction too: the violation message carries only
        the Redis key, never content, and content blocked by the firewall is
        still never written.

        Args:
            pipeline: Optional Redis pipeline, forwarded unchanged. The
                existence check is never queued onto it -- see the module
                docstring.
            ignore_errors: Forwarded to ``Model.save()``.
            skip_auto_now: Forwarded to ``Model.save()``.
            update_fields: Forwarded to ``Model.save()``. A partial save of an
                existing record is still an overwrite and is still refused.
            migrate_key: Always refused. A key migration deletes the old key.
            skip_write_filter: Forwarded to ``Model.save()``.
            **kwargs: Forwarded to ``Model.save()``.

        Returns:
            Whatever ``Model.save()`` returns: the pipeline when one was
            supplied, else a truthy result.

        Raises:
            AppendOnlyViolation: If the key exists, if ``migrate_key`` is True,
                or if ``obsolete_redis_key`` is set.
        """
        redis_key = self.db_key.redis_key
        model_name = type(self).__name__

        if migrate_key:
            raise AppendOnlyViolation(
                f"{model_name} is append-only: save(migrate_key=True) would "
                f"DELETE the previous key after writing the new one "
                f"(key={redis_key}). Append a new record instead."
            )

        obsolete = getattr(self, "obsolete_redis_key", None)
        if obsolete:
            raise AppendOnlyViolation(
                f"{model_name} is append-only: a KeyField was mutated on a "
                f"saved instance, so this save would DELETE the previous key "
                f"(obsolete_key={_as_str(obsolete)}). Append a new record "
                f"instead."
            )

        # Read POPOTO_REDIS_DB directly, never `pipeline`: an EXISTS queued on
        # a pipeline returns the Pipeline object, which is always truthy, and
        # would refuse every save including the first.
        if POPOTO_REDIS_DB.exists(redis_key):
            raise AppendOnlyViolation(
                f"{model_name} is append-only: a record already exists at "
                f"{redis_key}. Append a new record instead of overwriting; "
                f"use {model_name}.hard_delete() only for retention/erasure."
            )

        return super().save(  # type: ignore[misc]
            pipeline=pipeline,
            ignore_errors=ignore_errors,
            skip_auto_now=skip_auto_now,
            update_fields=update_fields,
            migrate_key=migrate_key,
            skip_write_filter=skip_write_filter,
            **kwargs,
        )

    def delete(
        self,
        pipeline: Optional["Pipeline"] = None,
        *args: Any,
        **kwargs: Any,
    ) -> Union["Pipeline", bool]:
        """Always refuse. Append-only records are closed, never removed.

        This also covers ``Model.delete_all()`` and ``bulk_delete()``, which
        call ``instance.delete(pipeline=...)`` per instance.

        Raises:
            AppendOnlyViolation: Always.
        """
        raise AppendOnlyViolation(
            f"{type(self).__name__} is append-only: records are superseded or "
            f"retracted by appending a new record, never deleted "
            f"(key={self.db_key.redis_key}). "
            f"Use {type(self).__name__}.hard_delete() for retention/erasure."
        )

    @classmethod
    def hard_delete(cls, instance: Any, **kwargs: Any) -> bool:
        """Erase a record and every trace of it. Retention/admin only.

        The deliberate hole in the append-only contract, kept explicit and
        greppable so an audit can find every call site. It exists for erasure,
        not convenience: ``POPOTO_NEVER_RECORD_DISABLE=1`` is a supported
        deployment action, and with the firewall off a keyspace that can only
        grow has no way to remove a secret that landed in it.

        Sweeps, in order:

        1. The record hash, the class Set, every ``$IndexedF:``/``$TagF:``
           index Set, and every composite index entry -- by running
           ``Model.delete()`` itself (reached past this mixin's refusing
           override via the MRO), so field ``on_delete`` hooks do the work
           rather than a second, drifting copy of them.
        2. For each ``ValidityField`` on the model: the three interval ZSETs,
           the record's own field in both chain HASHes, and any
           ``{prefix}:open:*`` pointer still naming it. Steps 1 and 2 overlap
           by design -- ``ValidityField.on_delete`` already does most of this
           -- because the sweep is the contract here, not an optimization.
        3. The *value* side of both chain HASHes, which step 1 does not cover:
           ``ValidityField.on_delete`` removes the record as a chain *field*,
           but a neighbor's link may still name it as a *value*
           (``fwd`` holds ``old -> erased``). Left behind, that is a dangling
           link into a record that no longer exists.

        Args:
            instance: The record to erase. Must be saved.
            **kwargs: Forwarded to ``Model.delete()``.

        Returns:
            bool: True if the record existed and was removed.
        """
        from .validity_field import ValidityField

        member = instance.db_key.redis_key

        # Reach Model.delete() past this mixin's refusing override. Field
        # on_delete hooks own index/chain/interval cleanup; duplicating them
        # here would drift.
        # Same reason as ``super().save()`` above: the mixin does not inherit
        # from ``Model``, so the checker cannot see the ``delete`` that the MRO
        # of every composing class supplies.
        existed = bool(
            super(AppendOnlyMixin, instance).delete(**kwargs)  # type: ignore[misc]
        )

        validity_field_names = [
            field_name
            for field_name, field in instance._meta.fields.items()
            if isinstance(field, ValidityField)
        ]
        for field_name in validity_field_names:
            keys = ValidityField.get_all_keys(instance, field_name)
            POPOTO_REDIS_DB.zrem(keys["valid_from"], member)
            POPOTO_REDIS_DB.zrem(keys["invalid_at"], member)
            POPOTO_REDIS_DB.zrem(keys["ingested_at"], member)
            POPOTO_REDIS_DB.hdel(keys["chain_fwd"], member)
            POPOTO_REDIS_DB.hdel(keys["chain_rev"], member)

            for chain_key in (keys["chain_fwd"], keys["chain_rev"]):
                links = POPOTO_REDIS_DB.hgetall(chain_key) or {}
                dangling = [
                    _as_str(link_field)
                    for link_field, link_value in links.items()
                    if _as_str(link_value) == member
                ]
                if dangling:
                    POPOTO_REDIS_DB.hdel(chain_key, *dangling)

            prefix = ValidityField.get_prefix_db_key(instance, field_name).redis_key
            for pointer_key in scan_keys(f"{prefix}:open:*"):
                pointer_key = _as_str(pointer_key)
                current = POPOTO_REDIS_DB.get(pointer_key)
                if current is not None and _as_str(current) == member:
                    POPOTO_REDIS_DB.delete(pointer_key)

        return existed
