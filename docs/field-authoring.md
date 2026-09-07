# Writing Custom Fields

Popoto's field types (`Field`, `SortedField`, `KeyField`, `GeoField`, and the rest)
all derive from a single `Field` base class. Subclassing `Field` directly is how you
add a new storage or indexing behavior that the built-in field types do not cover —
for example a field that maintains its own companion hash in Redis alongside the
model's primary hash, the way `ConfidenceField` and `CyclicDecayField` do.

This page documents the contract a `Field` subclass should follow, with a focus on
the round-trip protocol every field author must satisfy: `roundtrip_policy`,
`roundtrip_note`, `export_state`, and `import_state`. These four members exist so that
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

## The `pre_save_validate` hook: refusing a save before anything is written

`on_save` is the wrong place to *reject* a save. By the time it runs on the
internal-pipeline paths, every `IndexedFieldMixin` field on the model has already
committed its hash value and its index entry against live Redis; raising there
leaves those writes behind. `pre_save_validate` exists for the check that has to
precede all of them:

```python
from popoto import Field

class MonotonicField(Field):
    @classmethod
    def pre_save_validate(cls, model_instance, field_name, field_value, **kwargs):
        # Raise to abort. Returning None (the default) allows the save.
        ...
```

It is dispatched once per field from a **single** site in `Model.save()`, after the
`pre_save` gate and above the partial/full split — which is above both eager
indexed-field phases and above the two external-pipeline arms. So a `raise` from
this hook means *nothing at all* was written or queued: not the model hash, not a
sibling field's index entry, not your own companion structure. On a partial save
the dispatch is scoped to `update_fields`, so a save of an unrelated column cannot
trip a validation your field owns.

Two rules follow from where it sits:

- **Raise, don't return.** The return value is ignored; a refusal is an exception.
  Prefer a typed error subclassing `ValueError`, so existing
  `except (TypeError, ValueError)` handlers around `save()` keep working.
- **Implement it only for a check that must precede every write on the model**, not
  merely every write your field owns. A check that only needs to precede your own
  structure belongs in `on_save`. Declining a save (the never-record firewall, the
  write filter) is also *not* this hook's job — those paths decline by returning,
  and they run first.

`ValidityField` is the worked example: it uses `pre_save_validate` to refuse a
re-save that declares a `valid_from` the index disagrees with. See
[Valid-time has one writer](features/validity-and-supersession.md#valid-time-has-one-writer).

## Lua scripts: use `run_lua`, not `client.eval`

A field that needs atomicity should run its script through
`popoto.redis_db.run_lua`, which takes the same arguments `client.eval` does:

```python
import popoto
from popoto.redis_db import run_lua

run_lua(pipeline or popoto.get_redis(), MY_LUA, 2, key_a, key_b, arg_1)
```

!!! warning "Resolve the client at the point of use"
    `from popoto.redis_db import POPOTO_REDIS_DB` binds a copy of that name in
    your module, frozen at import time. `set_REDIS_DB_settings()` rebinds the
    original and Python does not propagate a rebind to a copy, so a field
    holding one keeps writing to the pre-reconfiguration database while the rest
    of the ORM has moved on — silently. `popoto.get_redis()` re-reads the global
    on every call. Fields shipped before this was understood still hold
    module-level imports;
    [#655](https://github.com/tomcounsell/popoto/issues/655) tracks converting
    them.

`run_lua` caches a redis-py `Script` per script text and sends `EVALSHA`,
falling back to loading the source only when the server does not know the
hash. Calling `client.eval` directly still works, but it ships the whole
script body on every invocation — for a field whose hook runs on every save,
that is the script's byte size added to every write. Every shipped field was
converted in 1.9.0.

`run_lua` accepts a pipeline as its client, so the pipelined and immediate
branches of an `on_save` keep the shape they already have.

## Your field's index namespace, and why it cannot collide

`field_class_key` — the `$<Stem>F` prefix your field's internal keys live
under — is derived from the class name with `str.strip("Field")`. That
strips a character *set*, not a suffix: `FloatField` lives under `$oatF`,
`SortedField` under `$SortF`. The spelling is on disk for every deployment,
yours included, so it is frozen; 1.9.0 does not change it.

What 1.9.0 does change is that two class names can no longer fold onto one
namespace silently. `ModelField` and `MoField` both strip to `$MoF`; defining
the second one now raises `TypeError` naming the class that already owns the
namespace, instead of letting both write into the same index keys. If you
hit that, give the newcomer an explicit namespace:

```python
from popoto.models.db_key import DB_key

class MoField(Field):
    field_class_key = DB_key("$MoF2")
```

An explicit `field_class_key` is honored as-is and is the right tool
whenever the derived spelling is undesirable for a new class.

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
  preserved. A field declaring `"partial"` must also set `roundtrip_note`.

### Deciding between `"carry"` and `"partial"`

The line is not how hard the carry would be to write. It is what the bytes mean:

> Carry a structure when the exported bytes are a **fact about the record** that the
> destination can restore verbatim. Do not carry one that is a **property of the
> source deployment's history**, which the destination never lived through.

Three questions settle it. If the answer to any is no, the structure does not carry:

1. **Is it decomposable to this record?** A Bloom bit or a Count-Min counter is
   shared by every value that collides onto it, so no record owns a share of it —
   which is exactly why `on_delete` is a no-op for both.
2. **Can the destination restore it without replaying anything?** If reaching the
   exported value means calling the field's own mutators (`strengthen`, `resolve`,
   `confirm`), you are inventing a sequence of events to arrive at a number. Write
   the structure raw, or do not write it.
3. **Is rebuilding actually worse?** Sometimes it is better. A Bloom filter rebuilt
   from the imported records is *more* accurate than a carried one, which would hold
   bits for records that were never imported.

A carrier that answers all three must write raw Redis commands, not go through the
field's public API — the public API stamps fresh timestamps and fires feedback hooks
that would double-count against state carried independently by other fields.

### Writing the note

`roundtrip_note` is read by someone deciding whether an import is safe for their
data. State the **destination's contract** — what they end up with ("frequencies
restart from the import") — not what the code declines to do ("counters not
carried"). Cite a tracking issue only while the gap is genuinely open work; a
permanent limitation should say so, because a lingering issue number invites a
future reader to "finish" a decision that was already made. See
[Export and import](guides/export-import.md#fidelity-what-crosses-and-what-does-not)
for how Popoto's own subsystems came down on each side of this.

```python
import popoto
from popoto import Field


class LearnedScoreField(Field):
    roundtrip_policy = "carry"

    @classmethod
    def export_state(cls, model_instance, field_name, field_value, **kwargs):
        """Return this field's auxiliary state, or None if there is nothing to carry."""
        raw = popoto.get_redis().hget(
            cls._data_hash_key(model_instance), field_name
        )
        if raw is None:
            return None
        return {"score": float(raw)}

    @classmethod
    def import_state(cls, model_instance, field_name, state, **kwargs):
        """Restore state after the record has been constructed and saved."""
        if state is None:
            return
        popoto.get_redis().hset(
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
