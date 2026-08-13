# Writing Custom Fields

Popoto's field types (`Field`, `SortedField`, `KeyField`, `GeoField`, and the rest)
all derive from a single `Field` base class. Subclassing `Field` directly is how you
add a new storage or indexing behavior that the built-in field types do not cover —
for example a field that maintains its own companion hash in Redis alongside the
model's primary hash, the way `ConfidenceField` and `CyclicDecayField` do.

This page documents the contract a `Field` subclass should follow, with a focus on
the round-trip protocol every field author must satisfy: `roundtrip_policy`,
`export_state`, and `import_state`. These four members exist so that
[`popoto.transfer`](guides/export-import.md) — the export/import driver — can move
records between Redis instances without losing state that only your field knows how
to serialize.

## The `on_save` hook

Most custom fields extend behavior through `on_save`, a classmethod called after the
model instance's primary hash is written:

```python
from popoto import Field

class UppercaseField(Field):
    @classmethod
    def on_save(cls, model_instance, field_name, field_value, pipeline=None, **kwargs):
        # maintain whatever secondary Redis structure this field needs
        ...
```

If your field's secondary structures are a pure function of the field's stored
value — an index entry, a sorted-set score, a geo-set member — `on_save` alone is
sufficient. A plain re-save (construct a new instance with the same values and call
`save()`) fully reconstructs that structure. This is the common case, and it is
exactly what `roundtrip_policy = "rebuild"` (the default) declares.

## The round-trip obligation

Some fields maintain state that is *not* derivable from the field's stored value
alone — a running counter, a learned score, an amplitude that accumulated over many
`strengthen`/`weaken` calls. For those fields, `on_save` is not enough: reconstructing
the record from its plain value on a new Redis instance silently resets that state to
whatever `on_save` seeds it to.

**Every `Field` subclass in `src/popoto/fields/` must declare an explicit
`roundtrip_policy`.** This is enforced by a test (`tests/test_transfer_roundtrip.py`)
that enumerates every field subclass in the package and asserts each declares a
policy, so a field with independent Redis state cannot silently ship with the
inherited `"rebuild"` default. Third-party fields outside the package are not
enforced by that test, but the obligation is the same: if your field holds state
`on_save` cannot reconstruct, declare it honestly.

`roundtrip_policy` is one of three values:

- **`"rebuild"`** (default) — the field maintains no independent state that export
  needs to capture. Whatever `on_save` does on import is sufficient to fully
  reconstruct it. Correct for the vast majority of fields: indexes, sorted sets, geo
  indexes, and any structure that is a pure function of the stored value.
- **`"carry"`** — the field maintains state that is not derivable from the stored
  value alone. A field declaring `"carry"` must implement `export_state` and
  `import_state` to serialize and restore that state explicitly.
- **`"partial"`** — some state is carried or rebuilt, and some is knowingly not
  preserved. A field declaring `"partial"` must also set `roundtrip_note` explaining
  what is lost and why — typically citing a tracking issue for future work.

```python
class LearnedScoreField(Field):
    roundtrip_policy = "carry"

    @classmethod
    def export_state(cls, model_instance, field_name, field_value, **kwargs):
        """Return this field's auxiliary state, or None if there is nothing to carry."""
        raw = POPOTO_REDIS_DB.hget(cls._data_hash_key(model_instance), field_name)
        if raw is None:
            return None
        return {"score": float(raw)}

    @classmethod
    def import_state(cls, model_instance, field_name, state, **kwargs):
        """Restore state after the record has been constructed and saved."""
        if state is None:
            return
        POPOTO_REDIS_DB.hset(
            cls._data_hash_key(model_instance), field_name, state["score"]
        )
```

`export_state` is called once per field per exported record, after the field's plain
value has already been captured by `to_dict()`. Read whatever secondary Redis
structure your field owns and return it as a JSON-serializable dict, or `None` if,
for this particular instance, there is nothing to carry. The base implementation
always returns `None`, which is correct for `"rebuild"` fields.

`import_state` is called once per field per imported record, **after** the instance
has been constructed and saved — so any structure `on_save` already rebuilt exists
and can be overwritten or supplemented. `state` is whatever `export_state` returned
for this field on the exporting side, or `None` if nothing was carried. The base
implementation is a no-op, which is correct whenever there is no carried state to
restore.

Both methods resolve their own configuration from
`model_instance._meta.fields[field_name]` rather than taking it as an argument,
mirroring `on_save`'s existing shape.

## Model-level state

The round-trip protocol also applies to Model-level mixins that are not `Field`
subclasses — for example a mixin that maintains its own top-level Redis key rather
than a field-scoped one. `popoto.transfer` walks `type(instance).__mro__` and calls
`export_state`/`import_state` on any class that defines them **as its own
attribute** (`"export_state" in cls.__dict__`), with the same two-value shape minus
`field_name`:

```python
class AuditedMixin:
    roundtrip_policy = "carry"

    def export_state(self):
        return {"audit_log": self._read_audit_log()}

    def import_state(self, state):
        if state:
            self._write_audit_log(state["audit_log"])
```

State from a model-level mixin is keyed by the mixin's class name in the export
file, distinct from the field-keyed state above. `Model` itself defines neither
method, so a model with no such mixins contributes nothing at this level.

## Why this matters

The driver holds no knowledge of any concrete field or mixin type — it never checks
`isinstance` against a named class. That is what lets a new field type work
correctly with zero changes to `popoto.transfer`. The cost of that genericity is
that it is entirely on the field author to declare `roundtrip_policy` honestly and
implement `export_state`/`import_state` when the default is wrong. A field that
declares `"rebuild"` while quietly growing independent state produces the exact
failure the export/import feature exists to prevent: an import report that confirms
success while auxiliary state was silently reset.

If you are unsure which policy applies, ask: "if I export this record, delete it,
and reconstruct it from the exported plain values with a normal `save()`, is
anything about this field's Redis footprint different from before?" If yes, that
field needs `"carry"` (fully) or `"partial"` (partly), plus the corresponding
`export_state`/`import_state` overrides.

See [Export & Import](guides/export-import.md) for the user-facing guide to running
an export/import and reading the resulting report.
