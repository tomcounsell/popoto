---
status: Ready
type: feature
appetite: Large
owner: valorengels
created: 2026-09-16
tracking: https://github.com/tomcounsell/popoto/issues/581
---

# V1 — TypedRelationField: typed edges with validity and bounded adjacency queries (#581)

## Problem

Popoto's only association primitive is `CoOccurrenceField`
(`src/popoto/fields/co_occurrence_field.py`), which stores undirected
association strength and nothing else. "Alice works_at TechCorp, since March"
is representable only as "Alice and TechCorp co-occur strongly": the predicate
and the time axis are both lost at write time and unrecoverable at read time.

**Current behavior (verified on `origin/main` at `5955d141`):** `CoOccurrenceField`
stores one ZSET per primary key at `$CoOcF:{ClassName}:{field_name}:{pk}`,
whose members are target pks and whose scores are weights. `link()` writes the
mirror ZSET when `symmetric=True`; `LINK_WITH_PRUNE_LUA` prunes to
`max_edges`. There is no per-edge record anywhere, so there is nothing for a
predicate, a validity interval, or a provenance pointer to attach to. Grep for
typed edges, predicates, or adjacency indexes across `src/` returns zero hits
beyond co-occurrence.

**Desired outcome:** a typed edge model with ORM-speed one- and two-hop
neighborhood queries ("everything currently true about Alice"), where edges
supersede like facts via V0's validity intervals, on plain Redis/Valkey types
only.

## Freshness Check

Two upstream changes landed after this issue was written; both are recorded on
the issue and both bind this plan.

**#588 / PR #601 — supersession is now atomic.**
`SupersessionProtocol.save_and_supersede(new, identity_key=...)` applies the
successor's hash, indexes, open interval, the incumbent's close, both chain
links and the pointer repoint in one MULTI/EXEC, returning a `SupersedeResult`
(`src/popoto/fields/supersession.py:363`). Before it, composing "create the
successor edge" with "close the incumbent" silently no-opped the close inside a
single pipeline, so the issue's acceptance criterion was not implementable as
one write. Edge supersession **must** be built on `save_and_supersede`, not on
`save()` + a separate `supersede()`. `supersede()`/`invalidate()` now raise
`ValidityMemberAbsentError` for an absent member rather than returning `None`,
and `valid_from` has a single writer with `ValidityValidFromConflictError` on a
conflicting re-save — both propagate to the edge API and to the migration path.

**#564 / PR #709 — M5's type vocabulary exists.**
`src/popoto/recipes/reconciliation.py` exports
`CLAIM_TYPES = ("preference", "deadline", "trait", "relationship", "goal",
"procedure", "note")`, `CLAIM_TYPE_FAMILY`, `DEFAULT_CLAIM_TYPE = "note"`, and
`normalize_claim_type()`. The soft prerequisite is met, but the issue comment's
warning is the operative fact: **`claim_type` and `predicate` are not the same
axis.** `claim_type` labels a whole claim (`relationship` is one of seven);
`predicate` is the relationship's *name* (`works_at`). Reusing `CLAIM_TYPES`
for `predicate` would collapse every typed edge to a single predicate. M5's
identity digest is `claim_slot = sha256(agent_id|subject|claim_type)`, a
different identity shape from this issue's `subject|predicate|object`, so M5's
supersession slot and the edge slot deliberately do not coincide.

**The migration claim is false as stated, and this plan takes the other
branch.** The issue's sketch asserts CoOccurrenceField is "the degenerate case
(`predicate = co_occurs`), so existing data upgrades in place," flagged as
unverified. Verified: it does not. The co-occurrence layout has (a) no per-edge
record, so no hash exists to carry predicate, validity, or provenance; (b) no
predicate dimension in the key, so `$CoOcF:...:{pk}` cannot be reinterpreted as
`out:{subject}` without losing the ability to ever hold a second predicate; (c)
symmetric mirroring with pruning at `max_edges`, so the two directions are
independently lossy and cannot be reconstructed into a single directed edge
record faithfully. The acceptance criterion's second branch therefore applies:
this plan documents why the layouts cannot converge and ships a one-way
migration script.

## Prior Art

- `docs/plans/validity_primitives_v0.md` — V0, the mechanism edges reuse.
- `docs/plans/co_occurrence_field.md` and
  `docs/plans/cooccurrence_edge_weight_clamp.md` — the primitive being
  complemented (not replaced).
- `docs/plans/graph_traversal_retrieval.md` and
  `src/popoto/recipes/graph_traversal.py` — the existing bounded-traversal
  consumer; the natural reader of a typed adjacency index.
- `docs/plans/m5_reconciliation.md` — the claim-type vocabulary.
- `docs/plans/db_key_colon_escape_self_escaping.md` — directly relevant: the
  repo has already been bitten by delimiter collisions in composite keys. See
  Q1.

## Data Flow

```
caller (or M5 judged "relationship" output)
   │
   ▼
Edge(subject=..., predicate=..., object=..., weight=..., validity=...)
   │
   ▼
SupersessionProtocol.save_and_supersede(edge, identity_key=(subject,predicate,object))
   │  one MULTI/EXEC:
   │    hash + indexes + open interval + incumbent close + chain fwd/rev + pointer
   ▼
adjacency maintenance (same transaction):
   ZADD $TRF:{Class}:out:{subject} weight {edge_redis_key}
   ZADD $TRF:{Class}:in:{object}   weight {edge_redis_key}

read:
   neighbors(subject, as_of=None)            one hop
     ZRANGE out:{subject} BYSCORE REV LIMIT
       └─ minus ValidityField.resolve_excluded_keys(as_of)
   neighbors2(subject, as_of=None, limit=N)  two hops, fan-out capped per hop
```

## Architectural Impact

Additive. A new field/recipe module plus one model; nothing existing changes
behavior. `CoOccurrenceField` is untouched and remains the recommended
primitive for untyped association — this plan explicitly does not deprecate it.

The one genuine coupling is to V0: edges are the second consumer of
`ValidityField`/`SupersessionProtocol`, and the first outside the claim/fact
shape. Any V0 assumption that identity is `(subject, predicate)` — as
`SupersessionProtocol.identity_key(subject, predicate)` at `supersession.py:170`
suggests — must be confirmed to tolerate a three-part identity.
`_coerce_identity` accepts `Union[str, Sequence[str]]`
(`supersession.py:570`), which is the seam; task 1 verifies it rather than
assuming it.

## Appetite

**Size:** Large.

**Team:** Solo dev, code reviewer, PM for Q1/Q2 (the two decisions that are
cheap now and expensive after data exists).

**Interactions:** PM check-ins: 1, before task 3 — the key-namespace and
predicate-vocabulary decisions must be settled before any key shape is written,
because they are unmigratable once edges exist. Review rounds: 2.

## Prerequisites

| Requirement | Check Command | Purpose |
|---|---|---|
| V0 landed | `python -c "from popoto.fields.supersession import SupersessionProtocol as S; S.save_and_supersede"` | The atomic entry point (#588/PR #601). |
| M5 present (soft) | `python -c "from popoto.recipes.reconciliation import CLAIM_TYPES"` | Type vocabulary alignment; not a hard blocker. |
| Redis/Valkey reachable | `redis-cli -n 15 PING` | Test suite. |
| Dev extras | `pip install -e '.[dev,embeddings,benchmark,mcp]'` | Worktree gotcha #2. |

## Solution

### Key Elements

1. **`src/popoto/fields/typed_relation_field.py`** — the `Edge` model and the
   `TypedRelationField` descriptor that owns the adjacency indexes.
2. **Adjacency as two ZSETs per endpoint**, scored by weight, whose *members
   are edge redis_keys* — not target pks. This is the design decision the
   issue's sketch left implicit and it is load-bearing: `ValidityField
   .resolve_excluded_keys` returns redis_keys, so a member that is already a
   redis_key makes validity filtering a set-difference. A member that was a
   target pk would require a lookup per neighbor to decide validity, which
   destroys the "ORM speed" claim at exactly the fan-out where it matters.
3. **Supersession via `save_and_supersede`** with the three-part edge identity.
4. **A one-way migration script**, not an in-place reinterpretation.
5. **Every operation accepts `pipeline=`**, except the ones that cannot — see
   the Technical Approach note, which is a real constraint the issue's
   acceptance criteria do not anticipate.

### Flow

```python
class Edge(Model):
    edge_key  = KeyField()                     # canonical subject|predicate|object
    subject   = IndexedField(type=str)
    predicate = IndexedField(type=str)
    object    = IndexedField(type=str)
    claim_type = IndexedField(type=str, null=True)   # M5 vocabulary, not predicate
    weight    = SortedField(type=float)
    validity  = ValidityField()
```

- `link(subject, predicate, object, weight=..., valid_from=None)` →
  `save_and_supersede`, then adjacency ZADDs.
- `neighbors(subject, predicate=None, as_of=None, limit=...)` → one hop.
- `neighbors2(subject, ..., per_hop_limit=..., limit=...)` → two hops with a
  per-hop fan-out cap, mirroring `graph_traversal.py`'s existing bounding.
- `history(subject, predicate, object)` → the supersession chain, via
  `SupersessionProtocol.chain`.

### Technical Approach

**Valkey-safe throughout.** Core types only: HASH (the edge record, via the
normal Model path), ZSET (adjacency, `valid_from`, `invalid_at`), plus V0's
existing Lua. No modules.

**Default ON with a deploy-level kill switch.** Per repo doctrine, a capability
is not opt-in. Declaring `TypedRelationField` on a model enables adjacency
maintenance automatically; the escape hatch is a `Defaults.TRF_ENABLED`-style
pin readable from the environment at the deploy level, matching the shape of
`Defaults.NEVER_RECORD_ENABLED` and `Defaults.M4_RESOLUTION_ENABLED`. Any new
`Defaults` constant must be registered in `tests/benchmarks/overrides.py`'s
`MODULE_CONSTANTS` or `tests/benchmarks/test_defaults_sync.py` fails — and it
fails only in CI, after review has approved.

**The `pipeline=` criterion collides with `save_and_supersede`.** The acceptance
criterion says all ops accept `pipeline=`; `save_and_supersede` owns its own
MULTI/EXEC and `_validate_caller_pipeline` (`supersession.py:614`) exists
precisely to reject a caller-supplied one. The resolution this plan proposes:
read-path operations (`neighbors`, `neighbors2`, `history`) accept `pipeline=`;
the write path accepts it only on the non-superseding call shape and raises the
existing typed error otherwise, documented as such. This is a genuine conflict
between two acceptance criteria and is raised as Q3.

**Validity semantics inherited from V0, including its sharp edge.** As
`docs/plans/sdlc-586.md` records: `ValidityField.on_save` routes through
`execute_supersede(mode="open")`, which `ZADD NX`s `valid_from = save_time` and
`invalid_at = +inf`, and `resolve_excluded_keys` excludes only when
`invalid_at <= now` or `valid_from > now`. So a save-only edge is never
excluded — correct for edges (an open edge *is* currently true), but it means
**the default query returns everything ever saved unless something superseded
it**, and a test asserting "currently-valid only" on a corpus with no
supersession is vacuous by construction. Every validity test in this plan needs
an anti-vacuity control asserting the exclusion set is non-empty.

Backdated edges ("since March") need an explicit `valid_from`, which has a
single writer and raises `ValidityValidFromConflictError` on a conflicting
re-save. The edge API surfaces `valid_from` as a first-class argument to
`link()` rather than leaving callers to discover the constraint.

**Two-hop is bounded deliberately.** Path-finding, community detection, and
graph-wide semantic search are non-goals: that is where plain sorted sets stop
being the right tool, and stating the boundary is part of the deliverable.

**Migration is a script, not a reinterpretation.**
`scripts/migrate_cooccurrence_to_edges.py` SCANs `$CoOcF:{Class}:{field}:*`,
reads each ZSET with scores, and emits `Edge(subject=pk, predicate="co_occurs",
object=member, weight=score)`. Properties the script must state, because they
are losses and not bugs:
- A symmetric co-occurrence pair yields two directed `co_occurs` edges; it
  cannot be recovered as one undirected edge because `max_edges` pruning may
  have removed one direction independently.
- No `valid_from` is recoverable; migrated edges get an explicit
  `valid_from = migration_time` and are marked as such, never backdated to a
  time nobody recorded.
- The script is idempotent and non-destructive: it never deletes `$CoOcF:` keys.
  Deleting the source is a separate, operator-initiated step.

## Failure Path Test Strategy

- `ValidityMemberAbsentError` on superseding a nonexistent edge — asserted, not
  swallowed.
- `ValidityValidFromConflictError` on re-saving a backdated edge with a
  different `valid_from` — asserted as the typed error.
- `_validate_caller_pipeline` rejection on a caller pipeline into the
  superseding write path — asserted with its typed error.
- Self-loop (`subject == object`): `CoOccurrenceField.link` raises; the edge
  model should decide explicitly rather than inherit by accident. See Q4.
- Empty/invalid: empty `predicate`, empty `subject`, a delimiter character
  inside any of the three parts (Q1) — each a typed error at construction, not
  a silently mangled key.
- Adjacency membership for a superseded edge: the ZSET member must remain (so
  `as_of` history works) and be filtered by validity at read time — asserted
  both directions, with the non-empty-exclusion-set control.

## Test Impact

New: `tests/test_typed_relation_field.py` (the issue names this path). No
existing test changes expected; `tests/test_co_occurrence_field.py` must stay
green untouched, which is the regression guard for "CoOccurrenceField is not
disturbed."

**Connection binding for any fixture that builds its own client.** Do not read
`REDIS_URL` and trust it. An operator-injected `REDIS_URL=redis://localhost:6379/0`
— the live agent store — was observed on a developer machine on 2026-09-16, and
`tests/benchmarks/run_external._resolve_bench_db()` is a second binder
(`POPOTO_BENCH_DB`, default 14) with different precedence. Every fixture here
binds through `get_REDIS_DB()` and, if it stands up its own connection, asserts
the resolved DB from the live `connection_pool` and refuses `0`, following
`examples/tests/conftest.py`. See `docs/plans/sdlc-568.md` for the full
statement of this hazard.

## Rabbit Holes

- **Path-finding and community detection.** Dropped by the issue's recon; wrong
  tool on plain types.
- **Automatic write-time entity linking.** Already rejected by M4's recon.
  Edge population stays caller-defined or routed through M5's judged output.
  Nothing here touches the write path.
- **Unifying `predicate` with `CLAIM_TYPES`.** Explicitly wrong (Freshness
  Check). Do not attempt it to save a vocabulary.
- **Migrating co-occurrence in place.** Verified impossible; the temptation to
  retry it will recur because the issue's sketch claims it.
- **A general graph query language.** One and two hops, by weight, with
  validity. That is the scope.

## Risks

**R1 — Delimiter collision in `subject|predicate|object`.** A subject
containing the delimiter silently produces a colliding edge_key. The repo has
already paid for this class of bug (`db_key_colon_escape_self_escaping.md`).
Mitigation: settle Q1 before any key is written; reject or escape at
construction, never mangle.

**R2 — Unbounded adjacency growth.** `CoOccurrenceField` prunes at `max_edges`;
a validity-bearing edge set cannot prune the same way without destroying
history. Mitigation: cap *fan-out at read time* rather than membership at write
time, and pin the cap in `Defaults`. State plainly that an edge set grows
without bound and that lifecycle/forgetting owns that problem, not this module.

**R3 — Vacuous validity tests.** See Technical Approach. Mitigation: every
validity assertion carries a non-empty-exclusion-set control.

**R4 — Two-hop cost.** A high-degree subject makes `neighbors2` quadratic.
Mitigation: per-hop fan-out cap, mirroring `graph_traversal.py`; a test
asserting the cap binds on a deliberately high-degree fixture.

**R5 — The adjacency ZADDs are outside `save_and_supersede`'s MULTI/EXEC
unless they are passed into it.** A crash between the two leaves an edge record
with no adjacency entry — invisible to every query. Mitigation: task 3 must
establish whether `save_and_supersede` can carry the extra ZADDs inside its
transaction; if it cannot, the plan needs a repair path (a reconcile that
rebuilds adjacency from the indexed `subject`/`object` fields) and that is
scope this plan must own, not defer.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG] Deprecating or changing `CoOccurrenceField`.
- [SEPARATE-SLUG] Automatic entity extraction or write-time entity linking.
- [SEPARATE-SLUG] Path-finding, shortest path, community detection, graph-wide
  semantic search.
- [ORDERED] Wiring typed edges into `ContextAssembler` as a retrieval source.
  Recipe-level consumption is a separate plan; this one ships the primitive.
- [ORDERED] Routing M5's judged `relationship` output into edges automatically.
  The producer integration is separate and depends on Q2's answer.
- Deleting `$CoOcF:` keys after migration.

## Update System

No update-system change. New module, no dependency, no config file. The
migration script is operator-invoked and ships under `scripts/`.

## Agent Integration

None required by this plan. Typed edges become agent-reachable only when a
recipe consumes them, which is an explicit No-Go above.

## Documentation

- `docs/features/typed-relations.md` — new page: model shape, key namespace,
  one- and two-hop API, validity semantics including the save-only caveat, the
  bounded-traversal boundary statement, and the migration story.
- `docs/features/co-occurrence.md` (or equivalent) — a cross-reference stating
  when to use which primitive, and that co-occurrence is not deprecated.
- Issue comment on #581 recording the migration verdict, since the issue
  currently carries the opposite claim.
- Per `project_diff_scoped_review_misses_docstrings`: grep repo-wide for
  docstrings teaching co-occurrence as the only association primitive and
  update them; pre-existing lines never enter the diff.

## Success Criteria

- [ ] An `Edge` persists subject, predicate, object, weight and validity, using
      only core Redis/Valkey types plus V0's existing Lua — no modules.
- [ ] `neighbors(subject)` returns currently-valid edges only by default, and
      historical edges under `as_of`, with a control test proving the exclusion
      set is non-empty (not vacuously "nothing excluded").
- [ ] `neighbors2` is bounded by a per-hop fan-out cap pinned in `Defaults`,
      demonstrated on a deliberately high-degree fixture.
- [ ] A new edge for an existing `subject|predicate|object` slot supersedes the
      old via `save_and_supersede`: incumbent closed, never deleted, chain
      traversable forward and reverse.
- [ ] Adjacency membership survives supersession and is filtered at read time —
      asserted in both directions.
- [ ] A crash between the edge write and the adjacency write is either
      impossible (same transaction) or repairable by a shipped reconcile, with
      a test for whichever is true.
- [ ] `scripts/migrate_cooccurrence_to_edges.py` converts a `$CoOcF:` corpus to
      `co_occurs` edges; idempotent, non-destructive, and its documented losses
      (direction, `valid_from`) are asserted by test rather than only described.
- [ ] `predicate` is its own vocabulary; `claim_type` (when set) is one of
      M5's `CLAIM_TYPES`. A test asserts the two axes are not conflated.
- [ ] Delimiter-bearing subject/predicate/object raises a typed error at
      construction; no key is ever silently mangled.
- [ ] Read-path ops accept `pipeline=`; the superseding write path raises the
      existing typed error for a caller pipeline and documents why.
- [ ] Any new `Defaults` constant is registered in
      `tests/benchmarks/overrides.py`'s `MODULE_CONSTANTS`.
- [ ] New code binds Redis via `get_REDIS_DB()`; no
      `from popoto.redis_db import POPOTO_REDIS_DB` anywhere in the diff.
- [ ] `tests/test_typed_relation_field.py` present; `tests/test_co_occurrence_field.py`
      unchanged and green.
- [ ] `docs/features/typed-relations.md` published; `mkdocs build --strict`
      passes.
- [ ] `ruff check src/`, `black --check src/ tests/`, `scripts/mypy_ratchet.py`
      pass.

## Step by Step Tasks

### 1. Verify V0 tolerates a three-part identity
Read `_coerce_identity` (`supersession.py:570`) and
`SupersessionProtocol.identity_key` (`:170`). Confirm a 3-sequence identity
round-trips through `save_and_supersede` and the chain walkers. If it does not,
that is a V0 change and a separate issue — report before proceeding.

### 2. Verify the migration claim and record the verdict
Enumerate the `$CoOcF:` layout against the proposed edge layout. Write the
verdict into the plan and onto the issue. (Pre-verified above; task 1 of the
issue's own sketch, discharged here, to be re-confirmed against `main` at build
time.)

### 3. Settle the key namespace and predicate vocabulary — **PM gate**
Q1 and Q2. Nothing below may start first; both are unmigratable after data
exists.

### 4. `Edge` model + `TypedRelationField`
Model, field, key builders, typed construction errors.

### 5. Write path on `save_and_supersede`
Including R5's transaction question and whichever of (same transaction /
reconcile) it resolves to.

### 6. Read path: `neighbors`, `neighbors2`, `history`
With validity filtering, `as_of`, fan-out caps, and `pipeline=`.

### 7. Tests
`tests/test_typed_relation_field.py`, including every anti-vacuity control
named in Risks.

### 8. Migration script
`scripts/migrate_cooccurrence_to_edges.py` + its tests for idempotence,
non-destructiveness, and documented losses.

### 9. Docs
Feature page, cross-reference, repo-wide docstring sweep, issue comment.

### 10. Verification
Full gate run; confirm co-occurrence suite untouched.

## Verification

| Claim | How it is verified |
|---|---|
| Typed edges persist with validity | `tests/test_typed_relation_field.py` round-trip |
| Supersession is atomic and chain-traversable | Chain walk test + a mid-write interruption test |
| Currently-valid default is not vacuous | Non-empty exclusion-set control |
| Two-hop is bounded | High-degree fixture asserts the cap binds |
| Valkey-safe | Passes the Valkey CI job |
| CoOccurrenceField undisturbed | Its suite green with a zero-line diff |
| Migration losses are real and known | Migration tests assert them |

## Questions for the architect

1. **What is the edge-key delimiter, and what happens when a subject contains
   it?** The issue writes `subject|predicate|object`. Popoto's own key
   separator is `:`, and the repo has already been bitten by delimiter
   collisions (`db_key_colon_escape_self_escaping.md`). Options: (a) reject
   delimiter-bearing parts with a typed error; (b) self-escaping, matching the
   existing plan's approach; (c) hash the triple into an opaque key and store
   the three parts as indexed fields. **The plan proceeds on (a)** as the
   smallest honest default, but (c) is arguably better and is unmigratable
   later. This is the single most expensive decision to defer.

2. **Where does the predicate vocabulary come from — open strings, a frozen
   enum, or M5-aligned?** M5's comment establishes `predicate` and `claim_type`
   are different axes, so `CLAIM_TYPES` cannot supply predicates. But an open
   string space means `works_at`, `works at`, and `WorksAt` are three
   predicates. Options: open strings with a `normalize_predicate()`; a frozen
   starter enum with an escape hatch; caller-registered vocabulary. **The plan
   proceeds on:** open strings plus normalization, with `claim_type` carried
   separately from `CLAIM_TYPES`. Confirm — a frozen enum would change the
   model shape.

3. **How is the `pipeline=` acceptance criterion reconciled with
   `save_and_supersede` owning its own MULTI/EXEC?** `_validate_caller_pipeline`
   exists specifically to reject a caller pipeline there. **The plan proceeds
   on:** `pipeline=` on the read path and on non-superseding writes; the typed
   rejection elsewhere, documented. That partially fails the criterion as
   literally written, which is why it is a question rather than a decision.

4. **Are self-loops legal?** `CoOccurrenceField.link` raises on
   `source_pk == target_pk`. But `Alice related_to Alice` is meaningless while
   `Doc cites Doc` may not be. **The plan proceeds on:** raise, matching the
   sibling primitive. Cheap to reverse; expensive to discover by accident.

5. **Does M5's judged `relationship` output feed edges automatically, or is
   population strictly caller-defined?** The issue says caller-defined "or
   routed through M5's judged output" — which reads as either. Automatic
   routing is a write-path change and brushes M4's rejected-rabbit-hole
   boundary. **The plan proceeds on:** strictly caller-defined, with the M5
   integration as an explicit `[ORDERED]` No-Go and a follow-up issue.

6. **Who owns unbounded edge-set growth?** R2: co-occurrence prunes at
   `max_edges`; an edge set carrying history cannot. **The plan proceeds on:**
   read-time fan-out caps only, and an explicit statement that lifecycle /
   forgetting owns pruning. If you want a write-time bound, it needs a policy
   that does not destroy supersession chains, and that is its own design.
