# Postgres Backend (proof of concept)

The storage backend seam ([#631](https://github.com/tomcounsell/popoto/issues/631))
puts a `Backend` protocol behind the agent-memory field layer and ships a
second implementation, `popoto.backends.postgres.PostgresBackend`, on the
`poc/backend-seam` branch. It is **not** a supported way to run popoto: nothing
here reaches `main` or PyPI until the POC's report (WS4) says it should, and a
user with `REDIS_URL` set and no `POSTGRES_URL` sees no change at all.

Selection is lazy: the first `popoto.get_backend()` call reads `POSTGRES_URL`
and, when it is set and `psycopg` is importable (`pip install
'popoto[postgres]'`), binds the Postgres backend; otherwise the Redis backend.
Importing popoto never dials Postgres.

## What is implemented (WS3a)

The record family, the atomic increment, and the orphan purge, all behind the
[conformance harness](../testing.md#backend-conformance-tests-opt-in) with
`RedisBackend` as the oracle:

| Protocol methods | Postgres shape |
|---|---|
| `save_record`, `load_record`, `load_records`, `load_fields`, `record_exists`, `delete_record`, `list_keys`, `count_records` | `popoto_record(key, field bytea, value bytea)`, one row per field, so the msgpack bytes the field layer emits are stored and returned untouched and a save merges fields the way `HSET` does. The class set lives in `popoto_set(idx, member)`. |
| `save_record(numeric=...)` | `popoto_numeric(key, field, value double precision)`, the typed side-map the decay query will `ORDER BY`. |
| `increment_field` | Per-key advisory lock, `SELECT ... FOR UPDATE`, decode / add / re-encode in Python, upsert; reproduces the Redis Lua's `%.14g` return and cmsgpack's integer / float32 / float64 packing, including the `Decimal` envelope and the absent-record case. |
| `purge_orphan` | One CTE statement: an `EXISTS` gate over `popoto_record` and conditional deletes from `popoto_sorted` / `popoto_set`. |
| `begin()` | `PostgresUnitOfWork`: a queue of operations run inside one transaction on `commit()`; leaving the `with` block without committing discards it. |

## What is implemented (WS3b)

The side maps, the set and sorted indexes, and the two remaining maintenance
methods, under the same harness and oracle:

| Protocol methods | Postgres shape |
|---|---|
| `map_get`, `map_set`, `map_delete`, `map_scan` | `popoto_map(idx, member, value bytea)`, one table for composite unique indexes, confidence payloads and supersession chain links. `map_set` is an upsert whose `RETURNING (xmax = 0)` is `HSET`'s new-entry reply; `only_if_absent` is `ON CONFLICT DO NOTHING` (`HSETNX`). `map_scan` pushes the glob down as a regex; its `count` is `HSCAN`'s batch hint and is ignored, there being no cursor. |
| `index_add`, `index_remove`, `index_members`, `index_union`, `index_intersection` | `popoto_set(idx, member)`. `SADD`/`SREM` replies are the statement's row count; union is `idx = ANY(...)`, intersection is `GROUP BY member HAVING count(DISTINCT idx) = n`, so a missing index empties it as `SINTER` does. |
| `scan_index_names`, `scan_record_keys` | The Redis glob translated to an anchored POSIX regex and applied with `~`: over the three index tables' `idx` for the first, over `popoto_record.key` for the second (the table *is* Redis's `TYPE` filter). |
| `sorted_add`, `sorted_remove`, `sorted_score`, `sorted_count`, `sorted_increment` | `popoto_sorted(idx, member, score double precision)` with an `(idx, score)` index. `ZADD`'s 1-for-new / 0-for-update reply is `RETURNING (xmax = 0)`; `ZINCRBY` is one `ON CONFLICT DO UPDATE SET score = score + delta`, which locks the row it updates. |
| `sorted_members`, `sorted_range` | `ORDER BY score, member COLLATE "C"` (see below); `ZRANGE`'s index arithmetic over a `row_number()` window in one statement; bounds compared as `double precision` with the operator chosen by the inclusivity flags. |
| `scan_index_members`, `drop_index` | A lazy `SELECT member` generator over the table `kind` names; `DELETE ... WHERE idx = %s`, replying 1 when any row went, else 0, as `DEL` does. |

Every one of these is a single statement, so none takes an advisory lock; the
per-key lock above is only for the record read-modify-write paths.

```sql
CREATE TABLE IF NOT EXISTS popoto_map (
    idx    text  NOT NULL,
    member text  NOT NULL,
    value  bytea NOT NULL,
    PRIMARY KEY (idx, member)
);
```

**Tie order.** Redis orders a sorted set by score and breaks ties by member,
comparing the member *bytes*, in both directions. Postgres's default collation
is locale-aware and does not agree (`B` sorts between `a` and `b` in
`en_US`), so every ordered read says `member COLLATE "C"` -- byte order for
UTF-8 text -- and the reverse reads flip both sort keys. The conformance
suite's property test seeds few distinct scores over twelve members chosen
for byte-order traps (`a`, `A`, `aa`, `a:1`, `a-1`, `_`, `0`, `é`) so ties are
the common case, and compares the Postgres leg against both a reference
model and the live `RedisBackend`.

**Bound rendering.** The Redis backend renders `sorted_range` bounds in wire
format -- `2.0`, `(2.0` for an exclusive bound, `-inf` / `+inf` -- and Redis
parses them back to doubles. Postgres has no rendering step: the bounds travel
as `double precision` parameters (`±Infinity` is a native value), and
`lo_inclusive` / `hi_inclusive` pick `>=` / `>` and `<=` / `<`. A `NaN` score
raises `ValueError` with Redis's wording (`value is not a valid float` on
`sorted_add`, `resulting score is not a number (NaN)` on an increment of
`inf` by `-inf`) where Redis replies with an error; the increment's
transaction rolls the row back.

**Non-positive `limit`** means unbounded in both backends, because the Redis
backend only passes `LIMIT` for a positive `int`.

Tables are created with `CREATE TABLE IF NOT EXISTS` on the first connection a
backend instance opens, in whatever schema the URL's `search_path` names. The
block runs in one transaction under
`pg_advisory_xact_lock(631, hashtext(current_schema()))`, because `IF NOT
EXISTS` is not race-safe: two sessions that both find a table absent both try
to create it and the loser fails on the catalog's unique index
(`UniqueViolation: pg_type_typname_nsp_index`). Measured on this tree before
the lock, 8 of 12 concurrent first connections against an empty schema failed
that way; with it, none, and `tests/conformance/test_postgres_bootstrap.py`
holds the line. The two-argument lock form is a separate key space from the
one-argument `hashtext(key)` record locks, so no record key can collide with
it. A bootstrap that fails closes its connection before the error propagates.

## What is implemented (WS3c)

The atomic index and tag swaps -- `INDEX_SWAP_LUA` and `TAG_SWAP_LUA` on
Redis -- and their delete-side counterparts, under the same harness and
oracle (`tests/conformance/test_swaps.py`):

| Protocol methods | Postgres shape |
|---|---|
| `swap_index` | One transaction following the Lua's phases. *Validation*, reads only: the pointer (`popoto_pointer`, then the pre-#476 in-hash `{field}\x00idxset` field of `popoto_record`, adopted and scrubbed as the Lua does), the idempotent re-save check (pointer already names the new index and the member is in it: rewrite the field bytes, reply 1), then uniqueness (`SELECT 1 FROM popoto_set WHERE idx = new AND member <> self`), which raises `ModelException` **before any write**. *Mutation*, all-or-nothing: `DELETE` the old membership (the pointer's index, else the field layer's `legacy_old_idx` hint), `INSERT ... ON CONFLICT DO NOTHING` the new, repoint, upsert the field bytes. Reply 1. |
| `swap_tags` | Previous membership from `popoto_pointer`, then the diff: `DELETE` from the indexes no longer named, `INSERT` into the newly named, reset the pointer rows to exactly the new set, upsert the packed tag list. Reply 1. |
| `drop_index_entry` | Pointer (then the in-hash legacy field, then `fallback_idx`) decides which index; `DELETE` the membership (the `SREM` reply), `DELETE` the pointer rows. |
| `drop_tag_entries` | Every index the pointer names, else `fallback_idxs` -- consulted only after the pointer read came back empty and never materialised up front, because the field layer hands in a lazy sequence whose first use may raise; `DELETE` each membership; the pointer `DELETE`'s row count is the `DEL` reply (1 / 0). |

```sql
CREATE TABLE IF NOT EXISTS popoto_pointer (
    key   text NOT NULL,
    field text NOT NULL,
    idx   text NOT NULL,
    PRIMARY KEY (key, field, idx)
);
```

**The pointer is a table, not a record field.** Both scripts need to know
which index a record *was* in for a field before they can move it, and on
Redis that is the `$IdxPtr:` / `$TagPtr:` side key. The plan's "the index row
is the pointer" holds only in reverse -- `popoto_set(idx, member)` answers
"who is in this index", not "which index is this record in for this field",
and an index name is opaque to the protocol -- so the reverse lookup is a row
of its own: one per `(key, field)` for an indexed/unique field, one per tag
for a tag field. It is never a field of `popoto_record` (that is the pre-#476
in-hash scheme the Lua scrubs), so `load_record` returns exactly what
`HGETALL` does. Of the Lua's two migration fallbacks only the second has a
Postgres shape: no record written by this backend can carry a pre-#540 side
key (`{key}\x00idxptr\x00{field}`), but a record *imported* with the pre-#476
`{field}\x00idxset` field can, and both swaps honour it.

**Conflict mapping.** The Lua replies `redis.error_reply('POPOTO_UNIQUE_CONFLICT')`
and `RedisBackend.swap_index` turns that into `ModelException("Uniqueness
violation on {record_key}.{field}: the value indexed at {new_idx!r} is already
taken by another instance")`. The Postgres backend raises the same exception
class with the byte-identical message from inside the swap's transaction, so
the field layer's re-wrap (`IndexedFieldMixin._unique_conflict_message`,
deviation 6) sees the same object on both backends and a user still reads
`Uniqueness violation on Model.field: value 'x' is already taken by another
instance`. Because the raise precedes every write, the transaction -- on the
`uow=` path the whole queue -- rolls back untouched: the "validation phase
then mutation phase" comment in the Lua is a real rollback here. One
difference, on the queued path only: a Redis pipeline relays the server's raw
`ResponseError('POPOTO_UNIQUE_CONFLICT')` from `execute()`, where Postgres
raises the protocol's `ModelException` from `commit()`; the conflicting swap
leaves no trace on either (see "Known deviations" for what happens to the
rest of the queue).

**Concurrency.** Two instances claiming one unique value for two *different*
records each read an empty index, and `FOR UPDATE` has nothing to lock on a
row that does not exist, so `swap_index` with `unique=True` takes the
advisory lock on the target index as well as on the record key (both through
the same ordered helper the record writers use). The second claimant waits,
re-reads under the lock, sees the first's committed row and raises: exactly
one success and one conflict, as Redis's single thread guarantees.
`tests/conformance/test_swaps.py::TestConcurrentUniqueClaim` forces the
interleaving deterministically (the second claim is started while the first
is mid-transaction and must show up blocked on the lock in `pg_stat_activity`)
and, with the index lock removed, stores two members. The other three swaps
lock the record key only, which serialises them against each other and
against `save_record` / `delete_record` on the same record.

## What is implemented (WS3e)

The validity intervals and the supersession script, under the same harness
and oracle (`tests/conformance/test_validity.py`):

| Protocol methods | Postgres shape |
|---|---|
| `supersede` | One call of the PL/pgSQL function `popoto_supersede`, which is `SUPERSEDE_LUA` phase for phase (see below). Runs inside the caller's transaction: its own on the direct path, the unit of work's at `commit()`. |
| `interval_of`, `interval_members` | Reads of `popoto_sorted` under the `valid_from` / `invalid_at` index names. `interval_members(select="valid")` is an `INTERSECT` of `valid_from <= as_of` and `invalid_at > as_of`; `select="excluded"` is one `WHERE (invalid_at <= as_of) OR (valid_from > as_of)` over both names, so a member absent from an index is excluded only by the index it is in, and one in neither is never excluded. |
| `drop_validity` | Three `DELETE`s under the member's advisory lock: its rows from the three interval names in `popoto_sorted`, its rows from the two chain names in `popoto_map`, and every row of `popoto_open_ptr` whose `member` is it (the Redis backend's `{prefix}:open:*` scan-and-compare as one statement). Replies 1 when a pointer went, else 0, as the last `DEL` does. |
| `open_pointer` | `SELECT member FROM popoto_open_ptr WHERE prefix = %s AND digest = %s`. |

**Where the intervals live.** On Redis a `ValidityField` is three ZSETs
(`valid_from`, `invalid_at`, `ingested_at`), two HASHes (the chain links)
and one STRING per identity digest (the open pointer), all named from the
`$ValidityF:<Model>` prefix. The backend keeps the intervals as rows of
`popoto_sorted` under those same three names and the links as rows of
`popoto_map` under the two chain names, rather than in the dedicated
`popoto_validity(model, pk, valid_from, invalid_at)` table the issue sketched,
because the field layer reads them through the *generic* families:
`filter(validity__current=False)` is `sorted_members` on the `invalid_at` and
`valid_from` names, and `SupersessionProtocol.chain` is `map_get` on the chain
names. An interval kept anywhere else would be invisible to both. The index
names are derived by the Redis backend's own `_validity_keys` (imported, not
copied), so `get_all_keys` parity holds by construction. The one new table is
the pointer:

```sql
CREATE TABLE IF NOT EXISTS popoto_open_ptr (
    prefix text NOT NULL,   -- "$ValidityF:<Model>:<field>"
    digest text NOT NULL,   -- the identity digest
    member text NOT NULL,   -- the open record's key
    PRIMARY KEY (prefix, digest)
);
CREATE INDEX IF NOT EXISTS popoto_open_ptr_prefix_member
    ON popoto_open_ptr (prefix, member);
```

**`+inf`.** The open sentinel is `'infinity'::double precision`, stored and
returned as Python's `float("inf")` exactly as the Redis backend returns
`ZSCORE`'s `inf`. It compares as the Lua's `math.huge` does: `invalid_at <=
as_of` is false for every finite `as_of` (an open record is never excluded)
and true for `as_of = inf`, on both backends. Plan decision 5 keeps the float
sentinel for the POC; whether `tstzrange` replaces it is WS4's question.

**The supersede, phase for phase.** `popoto_supersede` takes the three
interval names, the pointer's `(prefix, digest)`, the two chain names, the
members, the mode, the three instants and the assertion flag, and does what
`SUPERSEDE_LUA` does in the same order: resolve the incumbent from the pointer
(mode `open` skips every guard), the #588 membership guards (`EXISTS` over
`popoto_record`; an asserted incumbent that is absent is an error, one
resolved from the pointer is "no incumbent"), the idempotency guard on the
incumbent's `invalid_at`, the close-before-start check, the asserted
`valid_from` check; then -- below the function's own `MUTATION PHASE`
comment -- the close, both chain links, the `ON CONFLICT DO NOTHING` (NX)
open of the newcomer, and the repoint. It returns the closed member or `''`.
`now` is the caller's clock (WS0 deviation 2): every defaulted instant is
filled from it in Python and the function never reads `clock_timestamp()`.

**Token to exception.** Each `error_reply` token is a
`RAISE EXCEPTION USING ERRCODE = 'P0631'` (a custom SQLSTATE in the PL/pgSQL
class) whose `MESSAGE` is the same token line the Lua returns, and the backend
hands that line to `validity_field.map_lua_error` -- the function the Redis
backend calls on its `ResponseError` -- so `_LUA_ERROR_MAP` is the single
source of the mapping and the exception text is identical on both backends:

| Lua `error_reply` | Postgres `RAISE` | Exception |
|---|---|---|
| `POPOTO_VALIDITY_MEMBER_ABSENT successor <key>` | same `MESSAGE` | `ValidityMemberAbsentError` |
| `POPOTO_VALIDITY_MEMBER_ABSENT incumbent <key>` | same `MESSAGE` | `ValidityMemberAbsentError` |
| `POPOTO_VALIDITY_CLOSE_BEFORE_START` | same `MESSAGE` | `ValidityCloseBeforeStartError` |
| `POPOTO_VALIDITY_VALID_FROM_CONFLICT <stored> <requested>` | token as `MESSAGE`, the two numbers as `DETAIL` | `ValidityValidFromConflictError` |

The conflict's two numbers travel in `DETAIL` as Postgres `float8` text and
are rendered in Python with Lua's `tostring` (`%.14g`), because the script
prints `1759500000.123456` as `1759500000.1235` and Postgres would print
every digit. Any other error from the function -- a different SQLSTATE --
propagates untouched.

**Serialisation.** Redis runs the script on one thread. Here the function's
first statement is `pg_advisory_xact_lock(hashtext(prefix))` -- one lock per
model/field -- so every supersede on one `ValidityField` runs after the
previous one commits and the order among them is total. Then come the
per-key locks on the pointer key, the successor and the asserted incumbent
(the record writers' key space and ordering), which serialise a supersede
against `save_record` / `delete_record` / `drop_validity` on the same member,
and `SELECT ... FOR UPDATE` on the incumbent's `invalid_at` row. The prefix
lock is what closes the fourth writer pairing: an incumbent resolved from
the pointer *inside* the function is outside the per-key set, so two writers
whose chains cross (`d1 -> X` superseded by `Y` while `d2 -> Y` is superseded
by `X`) held disjoint key sets, both entered, and each NX insert on its
successor's `invalid_at` row waited on the other's uncommitted close:
`DeadlockDetected` on one of them, a raw error where Redis completes both
(#750 review B1). `tests/conformance/test_validity.py::TestConcurrency`
forces that interleaving (both incumbents' rows held from a probe
connection, both writers observed blocked, then released together, ten
times) and asserts Redis's outcome, alongside the three pairings it already
held deterministically. What remains is the cross-*operation* case the
record family documents as detected, not prevented: a unit of work that
queued `save_record(K)` ahead of a supersede holds `K` while it waits for
the prefix, and a concurrent supersede holding the prefix and naming `K`
explicitly waits for `K`; Postgres raises `DeadlockDetected` on one side and
persists nothing from it.

**#588 for free.** On the `uow=` path the function runs inside the unit of
work's transaction, so a successor whose `save_record` is queued ahead of the
supersede on the same unit of work is visible to the membership guard --
transaction visibility, with no pipeline-ordering argument needed.

## What is implemented (WS3d)

The decay ranking and the confidence update -- `DECAY_SCORE_LUA` and
`CAPPED_BAYESIAN_UPDATE_LUA` on Redis -- under the same harness and oracle
(`tests/conformance/test_decay.py`):

| Protocol methods | Postgres shape |
|---|---|
| `decayed_rank` | One `SELECT` (below): the member's last-seen timestamp from `popoto_sorted`, the base score decoded from the member's own `popoto_record` bytes, the confidence decoded from the `:data` companion row in `popoto_map`, the validity gate as two `NOT EXISTS` against the interval rows, the power law in the `ORDER BY`, `LIMIT` after the sort. Replies with the script's flat `[member, score, ...]` of `bytes`, every score rendered by Lua's `tostring` (`%.14g`). |
| `confidence_update` | One transaction: the member's advisory lock, the `require_record` existence check (an absent record returns `None` and writes nothing; the field layer raises), `SELECT ... FOR UPDATE` on the companion row, the script's reads and arithmetic in Python, the upsert of the packed payload. Returns `(confidence, evidence_count, corroborations, contradictions)` parsed as the Redis backend parses the script's `tostring` reply. |

```sql
WITH scanned AS (
  SELECT z.member,
         greatest((%(now)s - z.score) / 86400.0, 0.01)        AS elapsed,
         popoto_base_score(b.value)                            AS base,
         greatest(0.0, least(1.0,
             coalesce(popoto_confidence(cm.value), %(c0)s)))    AS c
    FROM popoto_sorted AS z
    LEFT JOIN popoto_record AS b
           ON b.key = z.member AND b.field = %(base_field)s
    LEFT JOIN popoto_map AS cm
           ON cm.idx = %(conf_idx)s AND cm.member = z.member
   WHERE z.idx = %(idx)s
     AND NOT EXISTS (SELECT 1 FROM popoto_sorted AS ia
                      WHERE ia.idx = %(invalid_idx)s AND ia.member = z.member
                        AND ia.score <= %(as_of)s)
     AND NOT EXISTS (SELECT 1 FROM popoto_sorted AS vf
                      WHERE vf.idx = %(valid_idx)s AND vf.member = z.member
                        AND vf.score > %(as_of)s)
), scored AS (
  SELECT member,
         (CASE WHEN base < 0 THEN -1.0 ELSE 1.0 END) * abs(base)
           * power(elapsed, -%(rate)s)
           * power(greatest(elapsed, 1.0),
                   -((%(rate)s * power(2.0, %(s)s * 2.0 * (%(c0)s - c)))
                     - %(rate)s))                              AS score
    FROM scanned
)
SELECT member, score FROM scored
 ORDER BY score DESC, member COLLATE "C"
 LIMIT %(limit)s
```

Each optional clause -- the base-score join, the confidence join and its
factor, the gate, the limit -- is present exactly when the script's guard for
it is true (`base_score_field ~= ''`; `confidence_hash_key ~= '' and s ~= 0`;
both validity keys and a parseable as-of; a limit at all), so a disabled path
reads nothing the script would not read. The validity gate is the script's
exclusion rule verbatim: `invalid_at <= as_of` or `valid_from > as_of`
excludes, and a member absent from an interval index is not excluded by it
(`+inf` is `'infinity'::float8`, which is never `<=` a finite as-of).
`pretrim_max_ratio` (ARGV[8]) only chooses between the script's two membership
strategies, which are asserted reply-identical, so one statement has one
strategy and the argument is accepted and unused.

**Reading msgpack in SQL.** The script decodes two payloads inside the store
(plan finding 2): `HGET member base_score_field` and `HGET confidence_hash
member`. Here those bytes are `popoto_record.value` and `popoto_map.value`,
so the bootstrap installs the subset of `cmsgpack.unpack` the two rules
consume as PL/pgSQL: every integer and float encoding (`popoto_mp_number`
rebuilds a float from sign, exponent and mantissa with exact `float8`
arithmetic, subnormals and the signed zero included), the str-key lookup in a
map and the first element of an array, and "is the buffer well-formed". The
two rules are `popoto_base_score` -- a number; else a map whose truthy
`as_encodable` is `tonumber`-ed (the `Decimal` envelope); else `1.0` -- and
`popoto_confidence` -- `data['confidence'] or data[1]` when the payload is a
table and that value is a number; else `c0`. What Redis's cmsgpack rejects is
rejected here too: it predates the msgpack bin and ext families, so a bin,
ext or `0xc1` byte anywhere in a payload is "Bad data format in input." and
the default wins, on both backends. The conformance suite holds 43 base-score
payload shapes and 18 confidence payload shapes to the Lua's reading on both
legs.

**Why the base score is not read from `popoto_numeric`.** Architect decision
2 confirmed the numeric side-map for exactly this `ORDER BY`. The query reads
the record bytes instead, for two reasons. The bytes are where the script
reads the value, so they reproduce it by construction -- a `Decimal`
envelope, a string, a boolean and a missing field all land where the Lua
lands them. And the side-map only reproduces it under the invariant that
`save_record(numeric=...)` keeps it current, which no caller holds today:
every `save_record` call in `models/base.py` (WS1a) omits `numeric`, so the
side-map is written by `increment_field` alone, and reading it would have
ranked every member at base `1.0`. Whether the side-map survives into
production is WS4's question; nothing in this family depends on the answer.

**Floating point.** The Lua computes in doubles and Postgres in `double
precision`, and both call the platform's `pow` (`power()` is a thin wrapper).
The asserted contract is *identical order* and scores within **`1e-9`
relative** of the Lua's; where no `pow` is involved (an elapsed time of
exactly one day, whose power is exactly 1) the reply bytes are asserted
equal. Measured on this tree (macOS aarch64, Redis and PostgreSQL 18.6 on
the same libm): over 577 ranked scores in five random scenarios of 200
members each -- random ages including the future, every base-score and
confidence payload shape, random validity intervals, several decay rates,
strengths and `c0`s, with and without the gate and a limit -- **0** of the
rendered scores differed from the live Lua's and the maximum relative
deviation was **0.0**; against a Python transcription of the script's
arithmetic both legs sat within `4.4e-14`. Across libms (CI runs
`redis:7-alpine` on musl and `postgres:16` on glibc) a last-ulp difference in
`pow` is possible, which is why the tolerance is stated rather than
bit-equality asserted. The tie-break is the script's: score descending, then
the member *bytes* ascending (`COLLATE "C"`; Lua 5.1's `<` on strings is
`strcoll`, which Redis runs in the C locale).

**The confidence payload is byte-identical.** `cmsgpack.pack` of the
script's four-key table emits the keys in Lua's hash order --
`corroborations`, `confidence`, `contradictions`, `evidence_count`, measured
-- with each number packed by cmsgpack's integer / float32 / float64 rule, so
a confidence of `0.5` is a float32, `1.0` an integer and `0.7` a float64 on
both backends. The Redis leg of the conformance suite checks the Lua's stored
bytes against the Postgres backend's packer on every run, and the Postgres
leg checks the backend stores them. The reply rounds the confidence through
`%.14g` as the script's `tostring` does; the stored value is the raw double.
Two instances updating one member concurrently serialise on the member's
advisory lock and the row lock (`TestConfidenceUpdate::test_two_connections_both_apply`
on both legs): every update lands and, the running mean being order-invariant
below the cap, the final state is the serial one.

## Cross-instance serialisation

Redis runs every command and script on one thread, so an `increment_field`
and a `save_record` on the same key can never interleave. Postgres gives each
backend instance its own connection, and a transaction on its own only
serialises rows that already exist (`FOR UPDATE` cannot lock a row that an
upsert is about to create). Every operation that writes a record key --
`save_record`, `delete_record`, `increment_field` and `purge_orphan` --
therefore takes `pg_advisory_xact_lock(hashtext(key))` as its first statement,
held until the transaction ends; on the `uow=` path that is the unit of work's
transaction at `commit()`. A rename (`save_record(obsolete_key=...)`) locks
both keys in one global order (ascending lock id) so two instances renaming
in opposite directions cannot deadlock on each other. Without this, an
increment on an absent field could read "no row", lose to another instance
committing `n = 100`, and store `0 + 1`: a value no serial order produces.

## Known deviations from the Redis oracle

**`2**63` packs differently per platform.** cmsgpack decides whether a Lua
number "fits `int64`" with a C `(int64_t)d` cast, which is undefined
behaviour at exactly `9223372036854775808.0`. On aarch64 the cast saturates,
so an arm64 Redis stores the msgpack `int64` max (`cf 7fffffffffffffff`); an
x86-64 Redis, and the Postgres backend on every platform, store the lossless
float32 (`ca 5f000000`). The value `increment_field` *returns* is identical
either way; only a later decode of the stored bytes differs (`int`
`2**63 - 1` against `float` `2**63`). The Postgres backend documents this
rather than emulating one platform's undefined behaviour.

**Globs match bytes on Redis and characters on Postgres.** `SCAN MATCH` and
`HSCAN MATCH` run Redis's `stringmatchlen` over the key's bytes, so `?` and a
`[...]` class consume one *byte*; the Postgres backend translates the glob to
a regular expression over `text`, where they consume one *character*. The two
disagree only on a non-ASCII name under a single-character glob (`_tag:?`
does not match `_tag:é` on Redis -- two bytes -- and does on Postgres). `*`,
the only glob popoto's own callers use (`prefix*`, `prefix:*`), agrees
everywhere. `tests/conformance/test_indexes.py::TestScanDeviations` pins the
difference rather than hiding it.

**`scan_index_names` sees only indexes.** Redis's `SCAN` returns every key of
every type that matches, so a *record* whose key happens to match an index
glob is returned too (and `rebuild_indexes` would `DEL` it). The Postgres
backend enumerates the three index tables only.

**A finite sum that overflows `double` raises.** `sorted_increment` of
`1e308` by `1e308` stores `inf` on Redis; Postgres's `float8` addition raises
`value out of range: overflow` for a finite-operand overflow, and the backend
does not emulate `inf`. `inf` plus a finite delta is `inf` on both.

**A failing operation in a unit of work rolls back the whole queue.** On
Postgres `commit()` runs the queue in one transaction, so `[add a, increment
inf by -inf, add b]` raises and stores nothing. A Redis pipeline reports the
error but still commits the other commands (`a` and `b` are stored). Scores
are also validated when an operation is queued, so a `NaN` raises before
`commit()` on Postgres where Redis raises at execute.

**A mismatched `kind` replies differently.** `drop_index` with the wrong
`kind` replies 0 and deletes nothing on Postgres where Redis `DEL` replies 1
and deletes whatever the key is; `scan_index_members` with the wrong `kind`
yields nothing where Redis raises `WRONGTYPE`. Callers pass the right kind.

**`-0.0` scores are stored as `0.0`**, matching `ZADD`'s normalisation.

**A conflicting swap's migration scrub is rolled back.** `INDEX_SWAP_LUA`
`HDEL`s an adopted pre-#476 in-hash pointer *before* it checks uniqueness, so
on Redis a save that then conflicts has still scrubbed the legacy field. On
Postgres the conflict rolls the whole transaction back, scrub included, and
the next save scrubs it. The legacy field is hidden from every decoder (its
name contains `\x00`), so nothing user-visible differs.

**A queued conflict surfaces as a different exception.** On the `uow=` path a
Redis pipeline raises the server's `ResponseError('POPOTO_UNIQUE_CONFLICT')`
from `execute()`; Postgres raises `ModelException` with the backend's wording
from `commit()`. The executed-now path raises the identical `ModelException`
on both. Pinned leg-aware in `test_swaps.py::TestUnitOfWork`.


**`popoto_supersede` is re-created on every bootstrap.** `SCHEMA_DDL` runs
once per new connection under the DDL lock, and `CREATE OR REPLACE FUNCTION`
is the one statement in it that is not a no-op on a bootstrapped schema: it
re-installs the function body every time. Idempotent today, and the reason
a hand-patched function does not survive the next connection; one more
reason the bootstrap belongs to the operator in production.

**A supersede error inside a unit of work is typed at `commit()`.** On Redis
the pipeline surfaces the script's reply as a raw
`redis.exceptions.ResponseError` at `execute()`, which the field layer's
`commit()` owners remap through `map_lua_error`; on Postgres the backend
raises the typed `ValidityError` from `commit()` directly (and, per the
rollback rule above, nothing else queued on that unit of work is applied).
A caller that remaps the Redis error sees the same typed exception either
way. `commit()`'s per-operation entry for a supersede is the raw script
reply on Redis (`b"<closed>"` / `b""`) and the decoded member or `None`
here; both are truthy exactly when something was closed, which is what
`SupersedeResult.close_index` and `ProvenanceJournal._write` read.


**A `-inf` last-seen timestamp raises on the decay ranking.** `ZADD` accepts
`-inf` as a score, and the Lua then computes `pow(inf, -rate) == 0` and scores
the member `0`; Postgres's `power()` reports that as `value out of range:
underflow` and the statement fails. Timestamps come from `time.time()`, so no
in-tree writer produces one. A `+inf` timestamp (a member "last seen" in the
infinite future) floors its elapsed time at a hundredth of a day on both
backends.

**A `commit()` entry for a queued `confidence_update`** is the script's raw
four-string reply on Redis and the typed `(confidence, evidence_count,
corroborations, contradictions)` tuple here; `None` for an absent record on
both. Callers read neither (the field layer returns `None` when queued).

**Not implemented**: record TTL (`save_record(ttl=...)` / `expire_at=...`
raises `NotImplementedError` rather than silently storing a record that never
expires), and `native()`, which raises on Postgres by design: it is the
Redis-only escape hatch for out-of-scope features; on the validity family
that is the transfer path (`export_state` / `import_state` /
`find_open_pointers_for_member`). Every protocol method is implemented.
