# Belief-Sheet View

The read-path claim resolver over the [Provenance Journal](provenance-journal.md):
surviving claims (retracted dropped, superseded collapsed to winners,
disjunctions shown as explicit uncertainty), each with a provenance handle, a
per-entry staleness annotation, and deterministic, replayable resolution.

`ContextAssembler.assemble()` is additive-then-truncate: it can rank and cut
candidates but cannot remove one because another record retracts it, has no
reader-scoped visibility, and emits no claim-level output — so a retracted
claim keeps its rank and is injected into the prompt as if it were live.
`BeliefSheetResolver` is the view layer that fixes that. It wraps an inner
`ContextAssembler` (the `AdaptiveAssembler` composition precedent — wrap, never
extend `assemble()`) and computes a `BeliefSheet`, a pure fold over the journal
parameterized by a plain policy dict.

```python
from popoto.recipes import BeliefSheetResolver
from popoto.recipes.context_assembler import ContextAssembler

resolver = BeliefSheetResolver(
    ContextAssembler(model_class=JournalEntry, score_weights={...})
)
sheet = resolver.resolve(
    {"subject": "launch"},
    reader={"agent_id": "agent-1", "purpose": "answer"},
)
for claim in sheet.claims:
    print(claim.key, claim.provenance)
```

Ran against a scratch Redis DB: a retracted entry never appears in the sheet
(even as a V0 straggler or a post-snapshot arrival); a superseded entry
collapses to its winner with loser→winner handle links on the winner's
`provenance["supersedes"]`; competing supersessions that tie under the policy
are flagged as unresolved contradictions for downstream LLM escalation — the
deterministic fold never guesses.

## The claim fold

`resolve_entries(records, chains_by_key, policy, ...)` is the pure core: no
Redis, no clock, no RNG. It partitions selected records into claims vs
annotations (annotation-kind records are evidence, never standalone claims),
unions the journal chains with selected annotations, and folds in a
deterministic `(target, kind, ts, key, position)` sort order:

- **Retract anywhere in the chain drops the claim outright.** A record
  targeted by any closing annotation (supersede/retract — including one that
  landed after the V0 snapshot, the Race 1 window) is dropped as a loser, never
  kept. The re-check costs no extra read because the chain postdates the
  snapshot.
- **Supersessions collapse to one winner.** `_pick_winner` applies the policy
  `prefer` precedence (below); a complete tie returns no winner and the entry
  is flagged unresolved with a warning, for escalation-only LLM handling.
- **A selected supersede whose target never ranked is itself a winner claim**:
  its statement *is* the correction. Selected confirms/retracts whose target is
  absent corroborate nothing visible and are dropped.
- **Disjuncts surface together, never collapsed.** Records carrying the same
  structural `class_id` pair as explicit uncertainty via
  `provenance["disjunct_with"]`. M5 is consumed structurally (duck-typed
  `class_id`, never imported), so without M5 ids this degrades to per-record
  output.
- **Corrupt evidence is flagged, not invented.** A targeted kind with no target
  address is dropped with an `unresolved contradiction` warning — surfacing it
  as a claim would invent provenance.

Membership stays on the V0 exclusion-set post-filter path; the chain walk only
re-checks the already-selected top-K.

## The policy dict

`resolve_policy(policy)` merges a caller dict over library defaults, read from
`Defaults` at call time so deploy-level overrides apply. Unknown keys are
ignored with a warning per key, never a crash; an invalid `prefer` falls back
to the default with a warning.

| Key | Default (`Defaults`) | Meaning |
|---|---|---|
| `prefer` | `"self-stated"` | Winner precedence among competing supersessions: `recent` (latest), `self-stated` (stated outright outranks inferred), `confirmed` (most corroborated) |
| `staleness_threshold` | `VIEW_RESOLVER_STALENESS_THRESHOLD` (0.5) | Decayed-score floor below which a claim is marked `stale` |
| `gate_overfetch_multiplier` | `VIEW_RESOLVER_GATE_OVERFETCH_MULTIPLIER` (2) | Headroom multiplier for the pre-truncation gate pull |
| `max_backfill_pulls` | `VIEW_RESOLVER_MAX_BACKFILL_PULLS` (1) | Extra assemble pulls to back-fill gate shortfall, via `exclude_keys` |

All four numerics are pinned in `Defaults` (magic numbers for experimental
tuning, not constructor kwargs) and registered as exemptions in
`tests/benchmarks/test_defaults_sync.py`.

## The reader gate

`resolve()` requires a concrete reader —
`{"agent_id": str, "purpose": str (optional), "tags": [...] (optional),
"tag_match": "any"|"all"}`. A `None` reader or a missing `agent_id` raises
`ValueError` immediately: a missing reader is a caller bug (fail fast), not a
gate decision. `purpose` is recorded in the sheet metadata; the gate enforces
`agent_id` + `tags`, since per-record purpose enforcement has no schema field
to check against.

The gate is a pre-truncation predicate between candidate merge and the cut, and
it is the **single enforcement point** on the resolver path: the inner
`assemble()` always runs with `tags=None` so its cooperative arm scoping does
not double-enforce (which would consume rejections the gate must count) or
re-expose the cooperative degrade the gate exists to invert. Bare `assemble()`
keeps its cooperative default byte-identical; only the resolver path inverts
degrade. Per-record gate errors deny that record with a log line.

Runtime gate failures fail **closed** without raising: tag-resolution failure
(e.g. Redis errors mid-gating) returns an empty sheet plus a
`reader gate failed closed` warning. Gate shortfall back-fills from arm
headroom with capped re-pulls; a shortfall that survives the cap lands as a
split `warnings` entry, not a silent cut. `metadata["reader_gate"]` reports
`validity_excluded` and `gate_rejected` counts when the gate is active.

Per-`resolve()` I/O budget: `1 assemble + <=1 gate batch + <=K chain reads`
(plus at most `max_backfill_pulls` further assembles). The gate batch is zero
when the reader carries no tags; chains are read only for the truncated top-K;
winner confirmations resolve from the winner's own chain when the winner was
also selected, and read nothing otherwise.

## Staleness and replay

Each claim carries `staleness` (the decayed relevance score, `None` when the
model has no `DecayingSortedField` in `score_weights`) and `stale` (`True` when
staleness is missing or below the policy threshold, `None` when unavailable).
Staleness is computed in one call via the shared `_staleness_details` helper,
so the resolver costs no extra per-record pass beyond what the trace +
staleness reads already do.

`BeliefSheet.serialize()` renders a canonical byte string: the same journal
snapshot + policy dict replays byte-identical (wall-clock-derived fields are
excluded). To replay, pin `as_of` (point-in-time membership) and `now` (clock
override for the staleness read) and compare `serialize()` output.
`metadata` carries `counts` (`admitted`, `claims`, `retracted_dropped`,
`superseded_collapsed`, `disjunct_groups`, `unresolved`, `gate_rejected`,
`validity_excluded`, `assembles`, `chain_reads`), the merged `policy`, and the
`reader`.
