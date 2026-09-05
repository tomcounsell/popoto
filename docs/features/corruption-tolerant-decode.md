# Corruption-Tolerant Decode

One undecodable byte string in one hash field no longer blinds the entire
record. A non-key field that fails to decode is **quarantined**: its raw
bytes stay untouched in Redis, the attribute reads as the field's declared
default, a warning is logged, and the instance remembers what happened so
`save()` can refuse to overwrite the evidence. Losing one field now costs one
field, not the whole row.

## The problem

Every decode path used to call `msgpack.unpackb` directly inside a dict
comprehension, with no exception handling. One bad field raised out of the
comprehension and every healthy field on that row became unreachable with it:

```python
Person.query.get(name="Alice")
# msgpack.exceptions.ExtraData: unpack(b) received extra data.
```

The exception named neither the model, the Redis key, nor the field, and
every query touching the row failed the same way — including a bulk query
where the poisoned row is one of a thousand.

This is exactly the failure mode [#476](https://github.com/tomcounsell/popoto/issues/476)
documented in production: a pre-1.9.0 writer put a raw, non-msgpack index
pointer into a model hash, and a reader's `unpackb` call on it raised for
every read of every indexed row. The index sets and the hashes were both
fine — only hydration failed — but from the caller's side the data looked
gone. The writer that caused that specific corruption is long gone (fixed in
[#540](https://github.com/tomcounsell/popoto/issues/540)/PR #547), but the
general class survives it: a partially-flushed write, a downstream project
writing into a popoto hash by hand, a future encoder change, a msgpack
version whose reader rejects what an older writer produced, or a
`decode_custom_types` tag whose payload no longer parses.

## Quarantine contract

A corrupt **non-key** field, on read:

- its raw bytes are left exactly as they are in Redis — nothing is deleted
  or rewritten
- the attribute reads as the field's declared default
- a `WARNING` is logged on the `POPOTO.encoding` logger, naming the model,
  the Redis key, the field, and the underlying decode exception
- the raw bytes are recorded on the instance's `_corrupt_fields` dict, keyed
  by field name

```python
loaded = Person.query.get(name="Alice")
loaded.bio                    # None -- the declared default, not the raw bytes
loaded._corrupt_fields        # {"bio": b"$IndexF:Person:status:active"}
```

All three decode call sites — the `fields_only` projection (`.values()`),
eager hydration (`.get()`, `.all()` without `lazy=True`), and the lazy path
(`.filter()`, `Query.get_many_objects()`, the async equivalent, all of which
default to `lazy=True`) — route through the same guarded seam,
`_decode_field_value`, so none of the readers can disagree about what a
corrupt field means. On the lazy path the field decodes (and is quarantined,
if corrupt) on first *access*, not at load time; a lazy instance that never
touches the poisoned field never quarantines it.

A non-UTF-8 hash *field name* (as opposed to a corrupt field *value*) is
skipped and warned rather than quarantined — a name that isn't a valid Python
identifier can never match a declared field, so it can never be misread as
one, and it never blocks a save.

## Why KeyFields are the exception

A corrupt **KeyField** raises `CorruptFieldError` immediately rather than
quarantining. A defaulted key is a *wrong identity*, not a missing value: the
row's `save()` guards against key mutation and against stale keys by
recomputing the key from the same decoded values that a quarantine would
default, so both safety nets would agree the key never changed and let the
row get written to a second hash with no exception and no log line — the
silent-duplication mechanism documented in
[#537](https://github.com/tomcounsell/popoto/issues/537)/[#538](https://github.com/tomcounsell/popoto/issues/538).
Losing the availability of one row is cheaper than silently duplicating it.

## `save()` refuses to overwrite quarantined bytes

Assigning a value clears a field's quarantine — that is the intended repair
path. But calling `save()` on an instance that still carries a quarantined
field raises `CorruptFieldError` instead of packing the field's declared
default over the preserved raw bytes:

```python
obj = Person.query.get(name="Alice")   # bio quarantined on load
obj.save()
# CorruptFieldError: Refusing to save Person (Person:...): ['bio'] could not
# be decoded from storage and would be overwritten with their declared
# defaults. The raw bytes are preserved in Redis. Repair with
# `obj.bio = <value>` then save(), or write only unaffected fields with
# save(update_fields=[...]).

obj.bio = "recovered text"             # clears the quarantine
obj.save()                             # succeeds; the row is clean
```

The refusal is scoped to the fields the save will actually write:
`save(update_fields=["last_seen"])` on a row whose *unrelated* field is
quarantined still succeeds, because that write never touches the poisoned
field. A full save (`update_fields=None`, or every declared field) keeps the
strict behavior.

`delete()` is deliberately **not** guarded — it removes the row outright, so
there is nothing to overwrite, and refusing would strand a poisoned row
permanently with no way to remove it.

## Inspecting a quarantine as an operator

`instance._corrupt_fields` gives you the field names and raw bytes without
leaving Python. To go straight to Redis:

```bash
redis-cli HGETALL Person:a1b2c3d4...
```

The field's raw value is exactly what was written — nothing has been
touched. If it's a legacy `$IndexF:`/`$KeyF:` pointer, or bytes from a since-
removed writer, that's your answer. If it's genuinely unrecoverable, the
repair path above is: decide what the field's value should be, assign it,
and save.

## The kill switch

`POPOTO_DECODE_QUARANTINE_DISABLE=1` restores the pre-#573 reader, which
raises the underlying decode exception (e.g. `msgpack.exceptions.ExtraData`)
for every corrupt field instead of quarantining it. It's read at call time,
inside the decode path's `except` branch, so flipping it takes effect
immediately without restarting the process — including via
`monkeypatch.setenv` in a test. See
[Configuration](../configuration.md#environment-variables) for the full
environment variable reference.

## See also

- [`CorruptFieldError`](../reference/index.md) — the exception raised for a
  corrupt KeyField and for a blocked `save()`.
- [Indexed Fields](../indexed_fields.md) — `IndexedField`/`UniqueField` are
  covered by the same guard: a corrupt indexed field blocks `save()` before
  the atomic index swap runs, so a poisoned field's default value can never
  land in a secondary index.
