---
status: Ready
type: bug
appetite: Medium
owner: valorengels
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/573
last_comment_id: none (issue has 0 comments)
---

# Corruption-tolerant decode for model hashes

## Problem

One undecodable byte string in one hash field currently blinds the entire record
load. `decode_popoto_model_hashmap` runs `msgpack.unpackb` inside a dict
comprehension with no exception handling, so any field that fails to unpack
raises out of the comprehension and the whole row — every healthy field on it —
becomes unreachable through `.get()`, `.filter()`, `.all()`, `.values()`,
`get_many_objects()` and the async hydration path.

This is exactly the failure mode #476 documented in the field. A 1.8.0 writer
put a raw, non-msgpack index pointer (`$IndexF:MyModel:status:active`) into the
model hash; a pre-1.8.0 reader called `unpackb` on it and got
`msgpack.exceptions.ExtraData(36, b'IndexF:...')` for every read of every
indexed row. The index sets were fine and the hashes were fine — only hydration
failed — but from the caller's side the data looked gone, and the true cause
took a full investigation to find. #476 shipped items 1-3 (pointer moved to a
namespaced side key in #540/PR #547, `delete()` hook ordering, unique-conflict
window) and left item 4, the decode hardening itself, explicitly optional and
open. #573 is that item.

The pointer-in-hash writer is gone, so this is not a fix for a live regression.
It is defense-in-depth against the general class: a partially-flushed write, a
downstream project writing into a popoto hash by hand, a future encoder change,
a msgpack version whose reader rejects what an older writer produced, or a
`decode_custom_types` tag whose payload no longer parses. In every one of those,
losing one field should cost one field, not the record.

**Current behavior:**

```python
Person.query.get(name="Alice")
# msgpack.exceptions.ExtraData: unpack(b) received extra data.
```

The exception names neither the model, nor the Redis key, nor the field. Every
healthy field on the row is unreachable, and every query that touches the row
fails the same way, including bulk queries where the poisoned row is one of a
thousand.

**Desired outcome:**

The healthy fields on the row hydrate. The undecodable field is *quarantined*:
its raw bytes are preserved untouched in Redis, the attribute reads as the
field's declared default, a `logger.warning` names the model, key, field and
exception, and the instance carries a `_corrupt_fields` record. A later
`save()` on that instance refuses with `CorruptFieldError` rather than
overwriting the preserved bytes with the default — so nothing is dropped
without a signal, at read time or at write time. A corrupt **KeyField** still
raises, because a defaulted key is a wrong identity and a wrong identity is how
rows get silently duplicated.

## Freshness Check

**Baseline commit:** `0dbce75917ec7d4db79a5de6908d1f980b5ee9eb` (`git rev-parse main`, HEAD at plan time, committed 2026-09-04T16:02:03+07:00)
**Issue filed at:** 2026-08-13T08:43:17Z
**Disposition:** Minor drift — every claim holds, all three line numbers moved.

**File:line references re-verified:**

- `src/popoto/models/encoding.py:317` (issue's `fields_only` site) — drifted to
  **line 464**. `msgpack.unpackb(value_b, strict_map_key=False)` inside the
  `fields_only` dict comprehension, still bare.
- `src/popoto/models/encoding.py:330` (issue's instance-hydration site) —
  drifted to **line 484**. Same call inside the `model_attrs` comprehension,
  still bare.
- `src/popoto/models/encoding.py:490` (issue's lazy site) — drifted to **line
  656**, now `decode_lazy_field()`, a named single-line function
  (`return decode_custom_types(msgpack.unpackb(value_bytes, strict_map_key=False))`),
  still bare. It is called from two places: `_create_lazy_model` (line 607,
  eager KeyField decode) and `Model.__getattribute__` (`base.py:896`, on first
  access of a lazy field).
- A **fourth** unguarded site the issue does not mention:
  `key_b.decode(ENCODING)` at `encoding.py:479` and `encoding.py:588` raises
  `UnicodeDecodeError` on a hash field name that is not valid UTF-8. Same class
  of failure, same blast radius. In scope.

**Cited sibling issues/PRs re-checked:**

- **#476** — closed 2026-08-13, completed. Items 1-3 confirmed shipped, not just
  claimed: `Model.delete()` runs `field.on_delete()` before the hash `DELETE`
  (`base.py:1910-1937`, with the ordering rationale in a comment citing the
  stale-snapshot risk), and `Model.save()` runs indexed/unique `on_save()`
  eagerly as its own atomic `EVAL` before the base-hash pipeline.
- **#540 / PR #547** — merged 2026-08-14. Moved the pointer to `$IdxPtr:`/`$TagPtr:`
  after the first attempt (`3cda1c1`, 1.8.1) suffixed the model key with `\x00`
  and broke `AutoKeyField` glob SCAN with `WRONGTYPE`. Relevant twice over: it
  removed the writer that motivated #476, and it is the cautionary tale about
  `\x00` not being an escape from the key space.
- **#412 / PR #424** — closed. The atomic-index work that introduced the
  pointer-in-hash write.

**Commits on main since issue was filed (touching referenced files):**

- `3b21c7c` fix(#540): namespace index pointer side keys (PR #547) — this is the
  commit that closed the #476 root cause. It does not touch the decode sites.
- `0ab47a1` fix(#537,#538): datetime KeyField identity + migration (PR #548) —
  added the `source_redis_key` parameter and the identity-provenance docstring
  to `decode_popoto_model_hashmap`. This is most of the line drift above. Its
  silent-duplication analysis is the direct argument for raising rather than
  quarantining on a corrupt KeyField (see Technical Approach).
- `16aa702`, `337b3f0`, `90fc3d3`, `31535a3` — agent-memory, never-record
  firewall, supersession, export/import. None touch `encoding.py`.
- `git log --since=2026-08-20 -- src/popoto/models/encoding.py` is **empty**.

**Active plans in `docs/plans/` overlapping this area:** nine plans mention
`encoding.py` — `atomic_index_maintenance_lua.md`, `datetime_keyfield_canonical_identity.md`,
`generic_export_import_roundtrip.md`, `partition_by_canonical_rendering.md`,
`policycache-q-value-storage-slot.md`, `provenance_journal_m1.md`,
`transfer_type_cleanliness.md`, `field_defaults_roundtrip_tests.md`,
`docs_modernization.md`. All either shipped or reference the module without
changing the decode comprehensions; none adds exception handling at the
`unpackb` sites. No conflict. (Several still carry `status: Ready` after their
work merged; that staleness is pre-existing and out of scope here.)

**Notes:** the issue's three line numbers are all stale by roughly 150 lines.
Use the corrected numbers above. The extra `key_b.decode` site is a genuine
addition to the issue's scope, not drift.

## Prior Art

- **#476** *(closed, completed)*: the parent. Diagnosed the pointer-in-hash
  forward-incompatibility, named the missing try/except explicitly as item 4,
  and marked it optional. Its "Mechanism" paragraph is the canonical worked
  example of the failure this plan prevents: `$` reads as msgpack positive
  fixint 36, the rest is trailing data, `ExtraData`.
- **#540 / PR #547** *(merged 2026-08-14)*: removed the writer. Established the
  `$IdxPtr:`/`$TagPtr:` namespacing and proved that `\x00` inside a key is not
  isolation. Does not harden the reader.
- **#537 / #538, PR #548** *(merged)*: datetime KeyField identity. Its finding —
  that a decode-side change to a KeyField's rendering silently duplicates rows,
  because `_saved_field_values` and the recomputed `_redis_key` are both blinded
  by the same recomputation — is why this plan refuses to quarantine KeyFields.
- **#380** *(closed)*: partial-hash rows leaking the class-level `Field`
  descriptor through `object.__getattribute__`. `_create_lazy_model` already
  initializes absent fields to their declared default for exactly this reason.
  Quarantine reuses that established "absent field reads as its default"
  behavior rather than inventing a sentinel value.
- **#412 / PR #424** *(closed)*: atomic index maintenance via Lua; the origin of
  the pointer write.

No prior attempt at corruption-tolerant decode exists. This is the first.

## Research

No external research performed. The work is entirely internal: msgpack's
exception taxonomy is already a project dependency, the failure mode is
documented end-to-end in #476 with a verified repro, and the design question
(quarantine vs. raise, per field class) is answered by this repo's own #537/#538
finding rather than by any ecosystem convention.

**Key findings from the codebase instead:**

- `msgpack.unpackb` raises `ExtraData`, `FormatError`, `StackError`,
  `OutOfData`/`UnpackValueError`, and plain `ValueError` depending on the
  malformation. `ExtraData` and `FormatError` subclass `Exception` directly, not
  `ValueError` — a `except ValueError` would miss the #476 case. Catch
  `Exception` at the site, narrowly scoped to the one `unpackb`+`decode_custom_types`
  call, and re-raise nothing.
- `decode_custom_types` can itself raise on a well-formed msgpack map carrying a
  malformed tagged payload (a `__datetime__` sentinel whose string is not a
  timestamp). It must be inside the same guard as `unpackb`, not outside it.
- `Model.__init__` does `self.__dict__.update(kwargs)` before the field loop, so
  an *unknown* kwarg is already harmless — a hash field with no declared Field
  does not crash today. Only undecodable ones do.

## Data Flow

1. **Entry point**: `Query.get()` / `.filter()` / `.all()` / `.values()` /
   `Query.get_many_objects()` / `Query._async_get_many_objects()` /
   `DB_key.get()` issues `HGETALL` (or a pipelined batch of them) and gets back
   `{field_name_bytes: msgpack_bytes}`.
2. **`decode_popoto_model_hashmap`** (`encoding.py:390`) branches three ways on
   `fields_only` / `lazy` / neither.
3. **Per-field decode** — the guarded seam this plan adds:
   - `fields_only=True`: dict comprehension at line 464. Quarantined field is
     **omitted from the returned dict**; the caller sees a missing key, never a
     wrong value.
   - eager instance: dict comprehension at line 484, then `model_class(**attrs)`.
     Quarantined field is omitted from `model_attrs`, so `__init__`'s default
     loop supplies the declared default (the #380 behavior).
   - `lazy=True`: `_create_lazy_model` (line 543) eagerly decodes only KeyFields
     (line 607) and stores the rest as raw bytes; non-key fields decode later in
     `Model.__getattribute__` (`base.py:896`) via `decode_lazy_field`.
4. **Quarantine record**: every quarantined field is recorded on the instance as
   `_corrupt_fields[name] = raw_bytes` and warned once per decode.
5. **`_saved_field_values`** is populated from the hydrated attributes as today —
   a quarantined field's entry holds the default.
6. **Write-back guard**: `Model.save()` checks `_corrupt_fields` before building
   `hset_mapping` and raises `CorruptFieldError` if any quarantined *declared*
   field remains unrepaired.
7. **Output**: the caller gets a usable instance with a loud, structured signal
   about exactly which field is unreadable, and cannot accidentally erase it.

## Why Previous Fixes Failed

The prior fixes in this lineage did not fail — they shipped and worked. The
pattern worth naming is different: **#476 correctly identified this hardening,
ranked it fourth, tagged it optional, and it then sat unaddressed for three
weeks while a PR body claimed it was "tracked against #476" on an issue that was
already closed.** PR #547's review caught that, which is why #573 exists.

| Prior Fix | What It Did | Why It Was Incomplete |
|-----------|-------------|-----------------------|
| PR #424 (#412) | Atomic index maintenance via Lua; wrote the pointer into the model hash | Fixed a concurrency bug, introduced a hash-schema change no reader contract covered |
| `3cda1c1` (#476 item 1, 1.8.1) | Moved the pointer to `{model_key}\x00idxptr\x00{field}` | `\x00` is not excluded from Redis glob; broke `AutoKeyField` SCAN with `WRONGTYPE` (#540) |
| PR #547 (#540) | Namespaced the pointer to `$IdxPtr:`/`$TagPtr:` | Removed the *writer*. The reader is still one bad byte away from losing a whole record — that is this plan |

**Root cause pattern:** every fix so far has hardened the writer. The reader has
never had a contract for what to do with bytes it cannot parse, so each new
writer-side mistake costs a full investigation and full record unavailability
until the writer is found. This plan gives the reader that contract.

## Architectural Impact

- **New dependencies**: none. `msgpack`, `logging`, `os` are all already
  imported in the touched modules.
- **Interface changes**: additive.
  - New `Model._corrupt_fields: dict[str, bytes]` instance attribute, always
    present (empty dict for healthy rows), so callers can test it without
    `hasattr`.
  - New `popoto.exceptions.CorruptFieldError(ModelException)`.
  - New deploy-level env switch `POPOTO_DECODE_QUARANTINE_DISABLE`.
  - No signature change to `decode_popoto_model_hashmap`,
    `_create_lazy_model`, or `decode_lazy_field`'s public shape.
- **Coupling**: slightly increased — `encoding.py` gains a read of
  `model_class._meta.key_field_names` to decide raise-vs-quarantine. It already
  reads `_meta.fields` and `_meta.key_field_names` in `_create_lazy_model`, so
  no new import direction.
- **Data ownership**: unchanged. Nothing new is written to Redis. Quarantine is
  purely a read-side and write-refusal behavior.
- **Reversibility**: high. The env switch restores byte-for-byte pre-#573
  behavior at deploy time, and the change is a contained set of try/except
  wrappers plus one guard in `save()`.

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 1 (confirm the raise-on-corrupt-KeyField and refuse-on-save
  decisions, which are the two places this plan chooses strictness over
  tolerance)
- Review rounds: 1

Justification for Medium over Small: the try/except itself is Small, but the
change is only *correct* if it covers all four decode sites plus the two lazy
entry points, and only *safe* if the write-back path refuses to erase the
preserved bytes. That write-back guard reaches into `Model.save()` and
`Model.__setattr__` and needs its own tests across the sync, async, lazy and
`fields_only` paths. The test surface — planting five distinct malformed payload
shapes and asserting behavior through six query entry points — is the bulk of
the work. Calling this Small invites a build that guards two of four sites and
silently erases data on the next save.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `redis-cli -n 14 PING` | Tests plant raw payloads on DB 14 |
| Test DB is not 0 | `python -c "import os; assert os.environ.get('POPOTO_TEST_DB','15') != '0'"` | DB 0 is the live agent store on this machine |
| Dev extras installed | `python -c "import msgpack, pytest, mypy"` | Suite and type gate |

## Solution

### Key Elements

- **Guarded per-field decode**: one helper that owns the `unpackb` +
  `decode_custom_types` call and decides tolerate-or-raise, used by all four
  sites so eager, lazy, `fields_only` and async can never disagree.
- **Quarantine record**: `_corrupt_fields` on the instance, plus one structured
  `logger.warning` per quarantined field naming model, Redis key, field name and
  exception type.
- **KeyField strictness**: a corrupt KeyField raises `CorruptFieldError`. Never
  quarantined, because a defaulted key is a wrong identity.
- **Write-back refusal**: `save()` raises `CorruptFieldError` while any declared
  field is still quarantined, so the preserved bytes cannot be overwritten by a
  default. Assigning a value to the field clears its quarantine, making
  `obj.field = value; obj.save()` the explicit repair.
- **Deploy-level kill switch**: `POPOTO_DECODE_QUARANTINE_DISABLE`, default off
  (so quarantine is ON), restoring the pre-#573 raise-everything reader for any
  adopter who prefers loud total failure.

### Flow

Poisoned row in Redis → `Person.query.get(name="Alice")` → **instance returned**,
`bio` reads as `None`, `WARNING POPOTO.encoding: quarantined field 'bio' on
Person:Alice (ExtraData)` in the log → caller inspects `person._corrupt_fields`
→ `person.save()` → **`CorruptFieldError: field 'bio' on Person:Alice is
quarantined`** → `person.bio = "recovered text"` → `person.save()` → **row is
clean**, quarantine gone, `bio` written normally.

Corrupt KeyField instead → `Person.query.get(...)` → **`CorruptFieldError`
naming the key field and the Redis key**. No instance is produced.

### Technical Approach

**1. The seam.** Add one private helper in `encoding.py`:

```
_decode_field_value(model_class, key_str, value_b, redis_key) -> tuple[bool, Any]
```

It performs `decode_custom_types(msgpack.unpackb(value_b, strict_map_key=False))`
inside a single `try`. On success it returns `(True, value)`. On `Exception` it
decides:

- if `key_str` is in `model_class._meta.key_field_names` → raise
  `CorruptFieldError` chained from the original (`raise ... from exc`), naming
  model, Redis key, field, and the underlying exception class;
- if quarantine is disabled by the env switch → re-raise the original unchanged;
- otherwise → `logger.warning(...)` and return `(False, value_b)` so the caller
  records the raw bytes.

Catching bare `Exception` here is deliberate and is the one place in this change
where it is correct: msgpack's `ExtraData` and `FormatError` do not subclass
`ValueError`, and `decode_custom_types` can raise anything a tagged decoder
raises. The handler never swallows — it always either raises or logs and
records — so it does not create the silent-failure class the Failure Path
section guards against.

**2. Field-name decode.** `key_b.decode(ENCODING)` moves inside the same guard
at both comprehension sites (`encoding.py:479`, `encoding.py:588`). A field name
that is not valid UTF-8 cannot correspond to any declared field, so it is
skipped and warned, recorded under `repr(key_b)` in `_corrupt_fields`. It can
never be a KeyField (KeyField names are Python identifiers), so it never raises.

**3. The four call sites.**

| Site | File:line (baseline `0dbce75`) | Behavior on quarantine |
|------|-------------------------------|------------------------|
| `fields_only` comprehension | `encoding.py:464` | Key omitted from the returned dict |
| eager instance comprehension | `encoding.py:484` | Key omitted from `model_attrs`; `__init__` supplies the declared default |
| `_create_lazy_model` eager KeyField decode | `encoding.py:607` | KeyField → always raises |
| `decode_lazy_field` on first access | `encoding.py:656`, called from `base.py:896` | Returns the declared default, records quarantine on the instance |

The `fields_only` branch returns a plain dict with **bytes keys** and no
instance, so it has nowhere to hang `_corrupt_fields`. Decision: it warns and
omits, and that is the whole signal. `Query.values()` is a projection API whose
consumers already handle absent keys; inventing a side channel for it is a
rabbit hole.

**4. `_corrupt_fields` lifecycle.**

- Initialized to `{}` in `Model.__init__` and in `_create_lazy_model`, so it is
  unconditionally present.
- Populated by the eager comprehension after the instance exists, and by
  `decode_lazy_field`'s caller in `__getattribute__` on first access.
- Cleared per field by `Model.__setattr__` when a declared field is assigned:
  `self._corrupt_fields.pop(name, None)`. This is the repair.
- Not persisted. It is a property of one hydration of one row.

**5. `save()` guard.** In `Model.save()`, before `encode_popoto_model_obj`,
raise `CorruptFieldError` if any key of `_corrupt_fields` is a declared field
name. Non-declared entries (undecodable field names, stray hash fields) do not
block a save — they are not in `hset_mapping`, so `HSET` leaves them physically
untouched and nothing is lost.

**6. `delete()` stays permitted.** Deleting a poisoned row must remain possible;
refusing would strand it. Indexed/unique `on_delete` reads the live `$IdxPtr:`
side key (#540/PR #547), not `_saved_field_values`, so a defaulted quarantined
value does not misdirect the `SREM` for those fields. For a *non-indexed*
quarantined field there is no index to misdirect. Tested explicitly.

**7. Kill switch.** `_read_decode_quarantine_switch()` in
`src/popoto/fields/constants.py`, following the exact shape of
`_read_never_record_switch` (#561) and `_read_journal_coupling_switch` (#560):
phrased as a `_DISABLE` so the default-on doctrine holds when unset, read at
call time so a deployment can flip it without a restart, `_TRUTHY` membership,
and reuse of the existing `_WARNED_BAD_ENV` warn-once set for malformed values.

**8. Byte-identical healthy path.** The helper adds a `try` (zero cost when
nothing raises) and one dict-membership test per field only on the failure
branch. No change to what a healthy row decodes to. Proven by a test that
hydrates a row exercising every custom type in `TYPE_ENCODER_DECODERS` and
asserts field-by-field equality plus `_corrupt_fields == {}`.

## Failure Path Test Strategy

### Exception Handling Coverage

- [x] The change **adds** exactly one broad `except Exception` (in
  `_decode_field_value`). It is not a swallow: every path through it either
  raises `CorruptFieldError`, re-raises the original, or logs a `WARNING` and
  returns a recorded quarantine. Each of those three outcomes gets its own test
  asserting the observable behavior — `pytest.raises` for the two raise paths,
  `caplog` for the warning, `_corrupt_fields` for the record.
- [x] No pre-existing `except Exception: pass` blocks exist in
  `src/popoto/models/encoding.py`. Verified by grep; asserted as an
  anti-criterion in the Verification table so the build cannot introduce one.

### Empty/Invalid Input Handling

- [x] Empty hash (`{}`): `decode_popoto_model_hashmap` already returns `None`;
  unchanged, and covered by an existing test. Re-asserted.
- [x] Zero-length field value (`b""`): `unpackb` raises; must quarantine, not
  crash. Tested.
- [x] Field value that is valid msgpack `None`: must **not** quarantine — a
  legitimately null field is not corruption. Tested, because conflating the two
  would make every nullable field look poisoned.
- [x] Every field on the row corrupt at once: instance still hydrates (KeyFields
  permitting), `_corrupt_fields` holds all of them, `save()` refuses. Tested.
- [x] Not agent-output processing; no empty-output loop risk.

### Error State Rendering

- [x] `CorruptFieldError`'s message names the model class, the Redis key, the
  field name and the underlying exception type. Asserted with a regex, because
  "unpack(b) received extra data" with no key attached is precisely what made
  the #476 investigation expensive.
- [x] The `logger.warning` uses lazy `%s` formatting and the
  `POPOTO.encoding` logger name, matching `POPOTO.model_base` /
  `POPOTO.datetime_key_migration`, so `POPOTO_LOG_LEVEL` governs it.
- [x] The warning fires once per field per decode, not once per attribute
  access: `decode_lazy_field`'s result is cached in `_decoded_fields`, so a
  repeated read of a quarantined lazy field does not re-warn. Tested.

## Test Impact

New file: `tests/test_corrupt_decode.py`. Everything below is additive.

- [ ] `tests/test_encoding.py` (if present at build time) — UPDATE: add an
  assertion that a healthy round trip leaves `_corrupt_fields` empty. No
  existing assertion changes.
- [ ] `tests/test_migrations.py`, `tests/test_datetime_key_migration.py` —
  UPDATE only if they construct instances positionally in a way the new
  `_corrupt_fields` initialization disturbs. Expected: no change; the attribute
  is set in `__init__` alongside the existing `_db_content` / `_saved_field_values`
  initialization.
- [ ] No test asserts that a corrupt field raises today, so nothing is
  invalidated by making it tolerant.

No existing tests are expected to break. The change is additive on the healthy
path and only alters behavior for inputs that currently raise an unhandled
exception, which no test exercises.

**New coverage (all on `POPOTO_TEST_DB=14`, payloads planted with
`POPOTO_REDIS_DB.hset` after a normal `save()`):**

| Planted payload | Shape | Expected |
|-----------------|-------|----------|
| `b"$IndexF:M:status:active"` | the historical #476 pointer | `ExtraData` → quarantine |
| `b"\xc1"` | msgpack never-used byte | `FormatError` → quarantine |
| `b"\x82\xa1a"` | truncated map | `OutOfData`/`ValueError` → quarantine |
| `msgpack.packb({"as_encodable": True, "__datetime__": "not-a-date"})` | valid msgpack, malformed tag | decoder raises → quarantine |
| non-UTF-8 field **name** `b"st\xffatus"` | undecodable key | skipped + warned, does not block save |

Each asserted through: `.get()`, `.filter()`, `.all()`, `.values()`,
`Query.get_many_objects()`, the async `_async_get_many_objects()` path, and
`lazy=True` hydration. Plus: corrupt KeyField raises on all of them; healthy
siblings on the poisoned row decode correctly; `save()` refuses then succeeds
after repair; `delete()` succeeds on a poisoned row and leaves no orphaned index
member; the env switch restores raising.

## Rabbit Holes

- **A repair/scan CLI.** A `popoto scan-corrupt` that sweeps a keyspace and
  reports poisoned rows is genuinely useful and genuinely a separate project. It
  needs SCAN batching, a report format, and a decision about whether it may
  write. Out of scope — see No-Gos.
- **Quarantining into Redis.** Copying poisoned bytes to a `$Quarantine:` side
  key on read makes reads write, which breaks read-only replicas and the
  `fields_only` fast path. The bytes are already preserved where they are; that
  is sufficient.
- **A sentinel value.** Inventing a `CorruptValue` marker object that all
  comparisons reject looks tidier than defaulting, but it leaks into user
  arithmetic, template rendering and JSON serialization at unpredictable
  distance from the decode. Defaulting reuses the #380 behavior users already
  see for absent fields.
- **Backporting the guard to 1.7.x.** #476's optional half. A maintenance-branch
  release is a maintainer-gated event, not a code change in this plan.
- **Narrowing the `except`.** Enumerating msgpack's exception classes reads
  better and is wrong: `decode_custom_types` can raise whatever a tagged decoder
  raises, and a future msgpack version can add a class. Broad catch, guaranteed
  non-swallowing.
- **Making `values()` report quarantine.** Its return type is a plain dict with
  bytes keys and no home for metadata. Warn and omit.

## Risks

### Risk 1: quarantine masks a systemic writer bug

**Impact:** if a bad writer poisons a field on every row, the reader now hides
it behind a warning instead of a crash. A team with `POPOTO_LOG_LEVEL=WARNING`
unset in production could run for weeks reading defaults for a real field.
**Mitigation:** the `save()` refusal is the backstop — any write path touching a
poisoned row fails loudly with a message naming the field. Popoto's default log
level is `WARNING` (`redis_db.py:86`), so the warning is visible unless
deliberately suppressed. The kill switch restores hard failure for teams that
prefer it, and the docs page states plainly that quarantine is a floor on damage
and not a substitute for fixing the writer.

### Risk 2: the `save()` refusal breaks a working read-modify-write loop

**Impact:** an application that loads rows, mutates one field and saves in a
loop starts raising on poisoned rows where it previously raised at load. Same
loop, different exception, different line.
**Mitigation:** it raised before too — earlier, and with a worse message. There
is no regression in availability, only a move of the failure to a point where
the message identifies the field. `CorruptFieldError` subclasses `ModelException`,
so existing broad handlers still catch it. Called out in the CHANGELOG under
`### Changed`.

### Risk 3: a corrupt KeyField still takes the whole row down

**Impact:** the plan deliberately does not fix the worst case. A row whose key
field is unreadable stays unreadable through the ORM.
**Mitigation:** intended, and defended by #537/#538: a defaulted or skipped
KeyField yields a wrong `_redis_key`, and `save()`'s two guards
(`KeyMutationError` and the obsolete-key branch) are both blinded by the same
recomputation — the row would be duplicated to a second hash with no exception
and no log line. Availability of one row is worth less than silent duplication.
The error message names the exact Redis key so an operator can inspect it with
`redis-cli HGETALL`.

### Risk 4: `_corrupt_fields` initialization perturbs `__init__` or `__setattr__`

**Impact:** `Model.__setattr__` has a lazy-cache branch and `__getattribute__`
is on the hot path for every attribute read on every lazy instance. A careless
addition costs measurable throughput on bulk queries.
**Mitigation:** the `_corrupt_fields.pop` in `__setattr__` runs only for names
in `_meta.fields`, a check `__setattr__` effectively makes already. Nothing is
added to `__getattribute__`'s fast path — the quarantine record is written
inside `decode_lazy_field`'s failure branch, which is already off the hot path.
The full suite plus `tests/benchmarks/` is the regression net.

## Race Conditions

### Race 1: concurrent repair between hydration and save

**Location:** `src/popoto/models/base.py` `save()` guard vs. another process
writing the same field.
**Trigger:** process A hydrates a poisoned row and quarantines `bio`; process B
writes a valid `bio`; process A assigns and saves.
**Data prerequisite:** none beyond the row existing.
**State prerequisite:** none.
**Mitigation:** none needed, and none added. Popoto's save is last-writer-wins
per field via `HSET`, and that is unchanged here. `_corrupt_fields` is
per-hydration state and never consulted by another process. A stale repair
overwrites B's value exactly as any stale field write would today.

### Race 2: quarantine observed on a row being deleted concurrently

**Trigger:** process A hydrates and quarantines; process B deletes the row; A
calls `save()`.
**Mitigation:** A raises `CorruptFieldError` before issuing any write, so the
deleted row is not resurrected by the refused save. This is strictly better than
today, where A's save would have recreated the hash.

### Race 3: mid-write partial hash

**Trigger:** a reader hydrates a hash while a writer's `HSET` mapping is
partially applied. Redis `HSET` with a mapping is a single command and therefore
atomic, so this cannot produce a torn *value*; it can only produce the absent-field
case #380 already handles.
**Mitigation:** none required. Noted because "partially-flushed write" is one of
the motivating scenarios and it is worth recording that Redis's atomicity rules
it out for single-command writes.

## No-Gos (Out of Scope)

- `[DESTRUCTIVE]` **Bulk repair or rewrite of poisoned rows.** Any helper that
  scans a keyspace and rewrites hashes is an irreversible one-shot over
  production data where review-before-execute is the safety mechanism. This plan
  preserves bytes and refuses writes; it never rewrites. An anti-criterion in
  the Verification table asserts no such helper landed.
- `[DESTRUCTIVE]` **Deleting or moving quarantined field values in Redis.** The
  preserved bytes are the only forensic record of what the writer produced. No
  code path in this change may `HDEL` a quarantined field. Asserted as an
  anti-criterion.
- `[ORDERED]` **Backporting the decode guard to a 1.7.x maintenance release**
  (#476's optional second half). Requires cutting a release from a maintenance
  branch, which is a maintainer-gated event in another system. Cannot be done
  from this plan's branch.
- `[EXTERNAL]` **Auditing live deployments for existing poisoned rows.**
  Requires connecting to production Redis instances the agent cannot and should
  not reach. The docs page will carry the `redis-cli` recipe an operator runs.
- `[EXTERNAL]` **Deciding the fleet-wide default for
  `POPOTO_DECODE_QUARANTINE_DISABLE`.** The code default is quarantine-on;
  whether a given deployment prefers hard failure is a per-operator call made in
  that deployment's config.

## Update System

No update-system changes. The feature is a library-internal read-path change
with no new dependency, no config file and no migration. The new
`POPOTO_DECODE_QUARANTINE_DISABLE` variable is optional and unset by default, so
no environment needs to change for the default behavior. Existing installations
gain the tolerance on upgrade with no operator action.

## Agent Integration

No agent integration required. This is a change to the ORM's decode path beneath
every existing surface. The agent-memory recipes
(`src/popoto/recipes/default_memory.py:244`,
`src/popoto/recipes/memory_lifecycle.py:978`, `:1128`) and the extraction
decision log (`src/popoto/extraction/decision_log.py`) call
`decode_popoto_model_hashmap` and `decode_lazy_field` directly and inherit the
tolerance without wiring. No MCP tool, hook or CLI surface changes. `memory_lifecycle.py:1128`
decodes a raw tier value through `decode_lazy_field` outside a model instance —
verify at build time that it gets the re-raise (no instance to quarantine onto)
rather than a silent default, and add a test for that call shape.

## Documentation

### Feature Documentation

- [ ] Create `docs/features/corruption-tolerant-decode.md`: the quarantine
  contract, the `_corrupt_fields` attribute, `CorruptFieldError`, the repair
  recipe (`obj.field = value; obj.save()`), why KeyFields raise, the
  `POPOTO_DECODE_QUARANTINE_DISABLE` switch, and the `redis-cli HGETALL`
  inspection recipe for an operator holding a quarantine warning.
- [ ] Add the entry to `docs/features/README.md`.
- [ ] Add to `mkdocs.yml` nav under **Core Features** (alongside
  `indexed_fields.md`), not under Agent Memory — this is core ORM behavior.

### External Documentation Site

- [ ] `docs/configuration.md` — document the new environment variable beside the
  other `POPOTO_*` deploy switches.
- [ ] `mkdocs build --strict` must pass.

### Inline Documentation

- [ ] `decode_popoto_model_hashmap`'s docstring gains a **Corruption tolerance**
  section next to the existing **Identity provenance** one, citing #573/#476.
- [ ] `_decode_field_value` carries the rationale for the broad `except` and for
  the KeyField asymmetry, with the #537/#538 citation.
- [ ] `CorruptFieldError` docstring states the repair path.

### CHANGELOG

- [ ] `## [Unreleased]` → `### Changed`: one entry, in this file's established
  house style — what changed, what it means for an upgrading deployment, the
  behavioral asymmetry (non-key quarantines, KeyField raises), the `save()`
  refusal as a **behavior change for read-modify-write loops on poisoned rows**,
  and the escape hatch.

## Success Criteria

- [ ] A row with one undecodable non-key field hydrates through `.get()`,
  `.filter()`, `.all()`, `.values()`, `get_many_objects()`, the async path and
  `lazy=True`, with every healthy sibling field correct.
- [ ] The quarantined field's raw bytes are still byte-identical in Redis after
  hydration (`HGET` compared to the planted payload).
- [ ] A `logger.warning` naming model, Redis key, field and exception type is
  emitted exactly once per field per decode.
- [ ] `save()` on an unrepaired instance raises `CorruptFieldError`; the row in
  Redis is unchanged after the raise.
- [ ] Assigning the field clears the quarantine and the next `save()` writes a
  clean row.
- [ ] A corrupt **KeyField** raises `CorruptFieldError` on every entry point; no
  instance and no duplicate row is produced.
- [ ] `POPOTO_DECODE_QUARANTINE_DISABLE=1` restores the pre-#573 exception for
  every case above except the KeyField one (which raises either way).
- [ ] A healthy row exercising every type in `TYPE_ENCODER_DECODERS` decodes
  field-for-field identically to `main`, with `_corrupt_fields == {}`.
- [ ] `delete()` succeeds on a poisoned row and leaves no orphaned index member.
- [ ] Full suite passes on `POPOTO_TEST_DB=14` (`/do-test`).
- [ ] `ruff check src/` exits 0; `black --check src/ tests/` exits 0.
- [ ] mypy introduces zero new errors, measured base-vs-branch in the same
  environment (see Verification note).
- [ ] Documentation updated (`/do-docs`), `mkdocs build --strict` passes.
- [ ] No xfail is introduced and no existing xfail is left stale.

## Team Orchestration

### Team Members

- **Builder (decode)**
  - Name: `decode-builder`
  - Role: the guarded decode seam in `encoding.py` and the new exception + env switch
  - Agent Type: builder
  - Resume: true

- **Builder (write-back)**
  - Name: `writeback-builder`
  - Role: `_corrupt_fields` lifecycle in `base.py` — init, `__setattr__` clear, `save()` refusal
  - Agent Type: builder
  - Resume: true

- **Test engineer**
  - Name: `corrupt-tester`
  - Role: `tests/test_corrupt_decode.py` across all payload shapes and entry points
  - Agent Type: test-engineer
  - Resume: true

- **Documentarian**
  - Name: `decode-documentarian`
  - Role: feature page, configuration page, CHANGELOG, docstrings, mkdocs nav
  - Agent Type: documentarian
  - Resume: true

- **Validator**
  - Name: `decode-validator`
  - Role: verifies success criteria and anti-criteria
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Guarded decode seam

- **Task ID**: build-decode
- **Depends On**: none
- **Validates**: `tests/test_corrupt_decode.py` (create), `tests/test_common_models.py`
- **Assigned To**: `decode-builder`
- **Agent Type**: builder
- **Domain**: Redis/Popoto data
- **Parallel**: true
- Add `CorruptFieldError(ModelException)` to `src/popoto/exceptions.py` and export it from `popoto`'s public surface where the other model exceptions are exported.
- Add `_read_decode_quarantine_switch()` to `src/popoto/fields/constants.py`, modeled line-for-line on `_read_never_record_switch` (#561): `POPOTO_DECODE_QUARANTINE_DISABLE`, `_TRUTHY` membership, read at call time, reuse `_WARNED_BAD_ENV`.
- Add `logger = logging.getLogger("POPOTO.encoding")` to `src/popoto/models/encoding.py`.
- Add `_decode_field_value(model_class, key_str, value_b, redis_key)` per Technical Approach step 1. Broad `except Exception`, three exits: raise `CorruptFieldError` for KeyFields, re-raise when the switch disables quarantine, warn-and-return otherwise.
- Convert the `fields_only` comprehension (`encoding.py:464`) and the eager comprehension (`encoding.py:484`) to explicit loops using the helper; move `key_b.decode(ENCODING)` inside the guard at both sites.
- Route `decode_lazy_field` (`encoding.py:656`) and `_create_lazy_model`'s eager KeyField decode (`encoding.py:607`) through the same helper.
- Preserve the existing `\x00` skip at every site verbatim — it is legacy pre-#476 pointer handling and is unrelated to quarantine.
- Keep the healthy path allocation-identical: no membership test, no dict write, nothing added outside the `except` branch.

### 2. Write-back guard and quarantine lifecycle

- **Task ID**: build-writeback
- **Depends On**: build-decode
- **Validates**: `tests/test_corrupt_decode.py`, `tests/test_atomic_save.py`, `tests/test_migrations.py`
- **Assigned To**: `writeback-builder`
- **Agent Type**: builder
- **Domain**: Redis/Popoto data
- **Parallel**: false
- Initialize `self._corrupt_fields = {}` in `Model.__init__` and in `_create_lazy_model`, beside the existing `_db_content` / `_saved_field_values` initialization, so the attribute is unconditionally present.
- Populate it from the eager decode after the instance is constructed, and from `Model.__getattribute__`'s lazy-decode branch (`base.py:896`).
- In `Model.__setattr__`, clear `self._corrupt_fields.pop(name, None)` when `name` is a declared field. Do not disturb the existing lazy-cache branch.
- In `Model.save()`, before `encode_popoto_model_obj`, raise `CorruptFieldError` if any `_corrupt_fields` key is a declared field name. Message names model, Redis key, field list and the repair.
- Leave `Model.delete()` permitted on a quarantined instance; add the reasoning as a comment citing #540/PR #547's `$IdxPtr:` pointer read.
- Verify `src/popoto/recipes/memory_lifecycle.py:1128`'s bare `decode_lazy_field` call gets the re-raise, since there is no instance to record quarantine on.

### 3. Corruption test suite

- **Task ID**: build-tests
- **Depends On**: build-decode, build-writeback
- **Validates**: `tests/test_corrupt_decode.py` (create)
- **Assigned To**: `corrupt-tester`
- **Agent Type**: test-engineer
- **Domain**: Redis/Popoto data
- **Parallel**: false
- Create `tests/test_corrupt_decode.py`. Save rows normally, then plant malformed payloads with `POPOTO_REDIS_DB.hset` — never construct hashes by hand from scratch, so the surrounding row is genuine.
- Cover all five payload shapes in the Test Impact table.
- Cover all seven entry points: `.get()`, `.filter()`, `.all()`, `.values()`, `Query.get_many_objects()`, `_async_get_many_objects()`, `lazy=True`.
- Assert the preserved-bytes criterion with `HGET` after hydration.
- Assert the warning with `caplog` on the `POPOTO.encoding` logger, including the once-per-decode property for repeated lazy access.
- Assert `save()` refuses, the row is unchanged after the refusal, repair-then-save works, and `delete()` succeeds and leaves no orphaned index member.
- Assert the KeyField raise on every entry point, and that no second hash was created (`SCAN` the model key space).
- Assert `msgpack.packb(None)` does **not** quarantine.
- Assert the env switch restores raising, with `monkeypatch.setenv`.
- Add the healthy-round-trip test over every `TYPE_ENCODER_DECODERS` type asserting `_corrupt_fields == {}`.
- Run with `POPOTO_TEST_DB=14`. Any ad-hoc repro script outside pytest must export `REDIS_URL=redis://localhost:6379/14` **before** `import popoto`.

### 4. Documentation

- **Task ID**: document-feature
- **Depends On**: build-tests
- **Assigned To**: `decode-documentarian`
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/features/corruption-tolerant-decode.md`; index it in `docs/features/README.md`; add it to `mkdocs.yml` nav under Core Features.
- Document `POPOTO_DECODE_QUARANTINE_DISABLE` in `docs/configuration.md`.
- Add the CHANGELOG entry under `## [Unreleased]` → `### Changed`.
- Update the three docstrings named in the Documentation section.
- Run `mkdocs build --strict`.

### 5. Final validation

- **Task ID**: validate-all
- **Depends On**: build-decode, build-writeback, build-tests, document-feature
- **Assigned To**: `decode-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run every row of the Verification table.
- Confirm each Success Criterion.
- Measure the mypy delta base-vs-branch in the same environment and report the redis-py version alongside the number, per `CLAUDE.md`.

## Verification

**mypy note:** the delta row below checks only that the touched files introduce
no errors of their own. Per `CLAUDE.md`, a full base-vs-branch mypy delta is
redis-py-version-dependent and must be measured in both a 7.x and an 8.x
environment before being trusted; state the version alongside the count.

**Swallow-check note:** the no-swallowed-exception row targets `except Exception`
specifically, not every `except`. `encoding.py:344-346` already carries a
legitimate `except ImportError: pass` guarding the optional `msgpack_numpy`
import; a bare `except` scan would fail at baseline. All rows were smoke-tested
against `0dbce75` and pass as written on unmodified `main`.

| Check | Command | Expected |
|-------|---------|----------|
| Full suite passes | `POPOTO_TEST_DB=14 pytest tests/ -q` | exit code 0 |
| Corruption suite passes | `POPOTO_TEST_DB=14 pytest tests/test_corrupt_decode.py -q` | exit code 0 |
| KeyField corruption raises | `POPOTO_TEST_DB=14 pytest tests/test_corrupt_decode.py -q -k keyfield` | exit code 0 |
| Save refusal covered | `POPOTO_TEST_DB=14 pytest tests/test_corrupt_decode.py -q -k save_refus` | exit code 0 |
| Async path covered | `POPOTO_TEST_DB=14 pytest tests/test_corrupt_decode.py -q -k async` | exit code 0 |
| Lint clean | `ruff check src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| Exception is importable | `python -c "from popoto.exceptions import CorruptFieldError"` | exit code 0 |
| Kill switch exists | `grep -c "POPOTO_DECODE_QUARANTINE_DISABLE" src/popoto/fields/constants.py` | output > 0 |
| Feature page exists | `test -f docs/features/corruption-tolerant-decode.md` | exit code 0 |
| Switch documented | `grep -c "POPOTO_DECODE_QUARANTINE_DISABLE" docs/configuration.md` | output > 0 |
| Touched files clean under mypy | `mypy src/ 2>&1 \| grep -c "^src/popoto/models/encoding.py"` | match count == 0 |
| No swallowed exception introduced | `grep -A1 "except Exception" src/popoto/models/encoding.py \| grep -c "^\s*pass$"` | match count == 0 |
| Anti-criterion: no bulk repair helper | `grep -rcEi "def (repair\|rewrite\|sweep)_corrupt" src/popoto/models/encoding.py src/popoto/models/base.py` | match count == 0 |
| Anti-criterion: decode never HDELs | `grep -rc "hdel" src/popoto/models/encoding.py` | match count == 0 |
| Anti-criterion: no new xfail | `grep -rc "xfail" tests/test_corrupt_decode.py` | match count == 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

The two questions below are **decided in-plan** and recorded here so a reviewer
can overturn them explicitly rather than discover them in the diff. Neither
blocks the build.

1. **`save()` refuses on an unrepaired quarantine.** The alternative is to drop
   the quarantined field from `hset_mapping`, leaving the bytes untouched and
   letting the save succeed silently. Decided against: a silent partial save is
   the exact "drop user data without a signal" outcome the issue rules out, and
   it would leave the instance's in-memory value permanently disagreeing with
   storage. Overturn this if a caller genuinely needs to update unrelated fields
   on a poisoned row without touching the poisoned one — the fallback would be
   an explicit `save(allow_quarantined=True)` opt-in, not a default.
2. **A corrupt KeyField raises rather than quarantining.** Defended by #537/#538
   in Risk 3. Overturning it requires a design for how a keyless instance
   reports its own identity, which is a larger piece of work than this plan.
