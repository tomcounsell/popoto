"""graph_traversal — separable seed→expand stage for multi-hop retrieval.

Wraps the two existing association primitives (``CoOccurrenceField`` and
``Relationship``) into a single, budget-bounded expansion step that
``ContextAssembler`` can drop in wherever it currently calls
``CoOccurrenceField.propagate()`` directly. Deliberately kept independent of
``context_assembler.py``'s internals: everything here takes primitives
(model class, field objects/names, seed PK strings) and returns a plain
``list[(pk, weight)]`` — the exact shape ``propagate()`` already returns, so
call sites are a pure conditional swap, not a rewrite (see issue #462).

No graph database, no Redis modules — only ``ZADD``/``ZRANGE`` (via
``CoOccurrenceField``, unchanged) and ``SRANDMEMBER`` (bounded-sample Set
reads for ``Relationship`` edges). Valkey-safe by construction.

Two things this module adds beyond the CoOccurrence-only graph arm already
shipped in ``ContextAssembler``:

1. **RelationshipField edge expansion** — walks a *self-referential*
   ``Relationship`` field (a field on ``model_class`` pointing back to
   ``model_class``) in both directions: forward (a node's own relationship
   value) and reverse (other nodes pointing at it, via the field's existing
   ``$RelationshipF:...`` index Set). Bounded per-hop fan-out via
   ``SRANDMEMBER`` (a capped sample, never a full ``SMEMBERS`` scan of a
   potentially large Set).
2. **Confidence/decay-modulated hop admission** — after co-occurrence and
   relationship expansion are merged and capped to ``max_candidates``, each
   surviving candidate's weight is multiplied by its own
   ``ConfidenceField``/decaying-field state (when configured), so a
   low-confidence or heavily decayed node is less likely to survive into
   the merged candidate set than a fresh, corroborated one.

Example:
    from popoto.recipes.graph_traversal import traverse

    results = traverse(
        Memory,
        seed_pks=["Memory:abc", "Memory:def"],
        co_occurrence_field=Memory._meta.fields["associations"],
        relationship_field_names=["related_memory"],
        confidence_field_name="certainty",
    )
    # => [("Memory:xyz", 0.42), ("Memory:qrs", 0.18), ...]
"""

import logging

from ..models.db_key import DB_key
from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger("POPOTO.graph_traversal")


# ---------------------------------------------------------------------------
# Tuning constants — experimental magic numbers, not user config (per repo
# convention; see COMPETITIVE_SUPPRESSION_SIGNAL / RRF_K in
# context_assembler.py for precedent).
# ---------------------------------------------------------------------------

RELATIONSHIP_HOP_DECAY = 0.5
"""Weight multiplier applied per relationship hop (``weight ** hop``),
matching CoOccurrenceField's default ``decay_per_hop`` so relationship- and
co-occurrence-derived edges are comparable in magnitude when merged."""

RELATIONSHIP_HOP_FANOUT_LIMIT = 50
"""Max related PKs consumed per node per hop, per direction. Enforced via
``SRANDMEMBER(key, n)`` — a bounded sample read at the Redis level, not a
full ``SMEMBERS`` followed by a Python-side slice, so a single high-degree
node cannot force an unbounded Set transfer."""

GRAPH_TRAVERSAL_MAX_CANDIDATES = 200
"""Candidate expansion is capped to this many PKs (by weight, descending)
*before* the confidence/decay modulation pass, which is the only part of
this module that pays per-candidate Redis round-trips (an instance load per
candidate). This bounds worst-case traversal cost independent of graph
fan-out."""

ADMISSION_THRESHOLD = 0.01
"""Minimum weight (before or after modulation) for a candidate to survive
into the returned list. Matches CoOccurrenceField.propagate()'s default
threshold."""


def _resolve_pk(value):
    """Resolve a Relationship field's runtime value to a PK (redis_key) string.

    Mirrors the type-dispatch in ``Relationship.on_save``/``on_delete``:
    the value is a ``Model`` instance, a redis_key ``str``, or ``None``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value if ":" in value else None
    # Model instance
    try:
        return value.db_key.redis_key
    except Exception:
        return None


def expand_relationships(
    model_class,
    seed_pks,
    relationship_field_names,
    *,
    depth=2,
    decay_per_hop=RELATIONSHIP_HOP_DECAY,
    fanout_limit=RELATIONSHIP_HOP_FANOUT_LIMIT,
):
    """BFS-style expansion over self-referential Relationship field(s).

    Only ``Relationship`` fields declared on ``model_class`` whose ``model``
    is ``model_class`` itself are honored (self-referential edges) — a
    relationship pointing at a *different* model class would surface
    instances of the wrong type into a candidate set that
    ``ContextAssembler`` treats as homogeneous. Any other configured field
    name is skipped with a warning, not an error.

    Args:
        model_class: The Popoto Model class being traversed.
        seed_pks: Starting PK (redis_key) strings.
        relationship_field_names: Names of self-referential Relationship
            field(s) on ``model_class`` to walk, both forward (a node's own
            relationship value) and reverse (other nodes pointing at it).
        depth: Max hops. Default 2.
        decay_per_hop: Weight multiplier applied per hop.
        fanout_limit: Max related PKs sampled per node per hop per direction.

    Returns:
        dict[str, float]: Discovered PKs (excluding seeds) mapped to their
        propagated weight (max across all discovered paths).
    """
    from ..fields.relationship import Relationship

    if not seed_pks or not relationship_field_names or depth <= 0:
        return {}

    valid_fields = []
    for field_name in relationship_field_names:
        f = model_class._meta.fields.get(field_name)
        if not isinstance(f, Relationship):
            logger.warning(
                "graph_traversal: %s is not a Relationship field on %s — skipped",
                field_name,
                model_class.__name__,
            )
            continue
        if f.model is not model_class:
            logger.warning(
                "graph_traversal: Relationship field %s on %s points to %s, "
                "not itself — only self-referential relationships are "
                "traversed, skipped",
                field_name,
                model_class.__name__,
                getattr(f.model, "__name__", f.model),
            )
            continue
        valid_fields.append((field_name, f))

    if not valid_fields:
        return {}

    seed_pks = [str(pk) for pk in seed_pks]
    visited = set(seed_pks)
    results = {}
    frontier = list(seed_pks)

    for hop in range(1, depth + 1):
        hop_weight = decay_per_hop**hop
        next_weights = {}

        for pk in frontier[:fanout_limit]:
            for field_name, field_obj in valid_fields:
                # Forward: load the node, read its own relationship value.
                try:
                    instance = model_class.load(db_key=pk)
                except Exception as e:
                    logger.warning(
                        "graph_traversal: failed to load %s for forward "
                        "relationship expansion: %s",
                        pk,
                        e,
                    )
                    instance = None
                if instance is not None:
                    try:
                        related_pk = _resolve_pk(getattr(instance, field_name))
                    except Exception as e:
                        logger.warning(
                            "graph_traversal: failed to resolve %s.%s: %s",
                            pk,
                            field_name,
                            e,
                        )
                        related_pk = None
                    if related_pk and related_pk not in visited:
                        if next_weights.get(related_pk, 0.0) < hop_weight:
                            next_weights[related_pk] = hop_weight

                # Reverse: bounded sample of other nodes pointing at pk.
                try:
                    reverse_key = DB_key(
                        Relationship.get_special_use_field_db_key(
                            model_class, field_name
                        ),
                        DB_key.from_redis_key(pk),
                    ).redis_key
                    members = POPOTO_REDIS_DB.srandmember(reverse_key, fanout_limit)
                except Exception as e:
                    logger.warning(
                        "graph_traversal: reverse lookup failed for %s.%s: %s",
                        pk,
                        field_name,
                        e,
                    )
                    members = []
                for member in members or []:
                    if isinstance(member, bytes):
                        member = member.decode("utf-8")
                    if member not in visited:
                        if next_weights.get(member, 0.0) < hop_weight:
                            next_weights[member] = hop_weight

        if not next_weights:
            break

        for pk, w in next_weights.items():
            if results.get(pk, 0.0) < w:
                results[pk] = w
        visited.update(next_weights.keys())
        frontier = list(next_weights.keys())

    return results


def _modulate_admission(
    model_class,
    candidates,
    *,
    confidence_field_name=None,
    decay_field_name=None,
    threshold=ADMISSION_THRESHOLD,
):
    """Multiply each candidate's weight by its confidence/decay state.

    ``candidates`` is a list of ``(pk, weight)`` tuples, already capped to a
    bounded size by the caller (this is the only place in the module that
    pays a per-candidate Redis round-trip: one instance load per
    candidate). A candidate whose modulated weight falls below
    ``threshold`` is dropped. Missing fields/instances degrade to a neutral
    multiplier of ``1.0`` rather than raising, so a misconfigured or
    absent field name never breaks traversal.

    Returns:
        list[tuple[str, float]]: Modulated, threshold-filtered candidates.
    """
    if not candidates or not (confidence_field_name or decay_field_name):
        return candidates

    from ..fields.confidence_field import ConfidenceField
    from .context_assembler import _partition_scores_for_field

    confidence_field = None
    if confidence_field_name:
        confidence_field = model_class._meta.fields.get(confidence_field_name)
        if not isinstance(confidence_field, ConfidenceField):
            logger.warning(
                "graph_traversal: %s is not a ConfidenceField on %s — "
                "confidence modulation skipped",
                confidence_field_name,
                model_class.__name__,
            )
            confidence_field = None

    decay_field = None
    if decay_field_name:
        decay_field = model_class._meta.fields.get(decay_field_name)
        if decay_field is None:
            logger.warning(
                "graph_traversal: %s not found on %s — decay modulation " "skipped",
                decay_field_name,
                model_class.__name__,
            )

    # Load bounded instance set once; missing instances get a neutral
    # multiplier rather than being silently dropped by traversal alone.
    instances_by_pk = {}
    for pk, _weight in candidates:
        try:
            instances_by_pk[pk] = model_class.load(db_key=pk)
        except Exception as e:
            logger.warning(
                "graph_traversal: failed to load %s for admission " "modulation: %s",
                pk,
                e,
            )
            instances_by_pk[pk] = None

    confidence_by_pk = {}
    if confidence_field is not None:
        for pk, instance in instances_by_pk.items():
            if instance is None:
                confidence_by_pk[pk] = 1.0
                continue
            try:
                confidence_by_pk[pk] = float(
                    ConfidenceField.get_confidence(instance, confidence_field_name)
                )
            except Exception as e:
                logger.warning(
                    "graph_traversal: get_confidence failed for %s: %s",
                    pk,
                    e,
                )
                confidence_by_pk[pk] = 1.0

    decay_by_pk = {}
    if decay_field is not None:
        loaded_instances = [i for i in instances_by_pk.values() if i is not None]
        try:
            decay_scores = _partition_scores_for_field(
                loaded_instances,
                model_class=model_class,
                field_name=decay_field_name,
            )
        except Exception as e:
            logger.warning("graph_traversal: decay score proxy failed: %s", e)
            decay_scores = {}
        for pk, instance in instances_by_pk.items():
            if instance is None:
                decay_by_pk[pk] = 1.0
                continue
            val = decay_scores.get(pk)
            decay_by_pk[pk] = 1.0 if val is None else max(0.0, min(1.0, float(val)))

    modulated = []
    for pk, weight in candidates:
        factor = 1.0
        if confidence_field is not None:
            factor *= confidence_by_pk.get(pk, 1.0)
        if decay_field is not None:
            factor *= decay_by_pk.get(pk, 1.0)
        new_weight = weight * factor
        if new_weight >= threshold:
            modulated.append((pk, new_weight))
    return modulated


def traverse(
    model_class,
    seed_pks,
    *,
    co_occurrence_field=None,
    relationship_field_names=None,
    depth=2,
    decay_per_hop=0.5,
    threshold=ADMISSION_THRESHOLD,
    max_candidates=GRAPH_TRAVERSAL_MAX_CANDIDATES,
    confidence_field_name=None,
    decay_field_name=None,
):
    """Seed→expand traversal: co-occurrence BFS + relationship walk, merged,
    budget-capped, and confidence/decay-modulated.

    Drop-in replacement for a bare ``CoOccurrenceField.propagate()`` call —
    returns the same ``list[(pk, weight)]`` shape, so existing callers only
    need to swap the call, not restructure downstream consumption (RRF
    ``graph`` arm, ``co_occurrence_boost``).

    Cost is bounded independent of graph fan-out: expansion (co-occurrence
    BFS + relationship walk) is pure sorted-set/set operations, capped to
    ``max_candidates`` *before* any instance is loaded; only the
    (optional) modulation pass pays per-candidate Redis round-trips, and
    only for the capped set.

    Args:
        model_class: The Popoto Model class.
        seed_pks: Seed PK (redis_key) strings from the upstream retrieval
            arm(s) (BM25/vector/composite).
        co_occurrence_field: A ``CoOccurrenceField`` instance to expand via
            ``propagate()``, or ``None`` to skip co-occurrence expansion.
        relationship_field_names: List of self-referential ``Relationship``
            field name(s) on ``model_class`` to expand via, or ``None`` to
            skip relationship expansion.
        depth: Max hops for both expansion sources. Default 2.
        decay_per_hop: Weight multiplier per hop (co-occurrence and
            relationship expansion both use this constant so their
            magnitudes are comparable when merged).
        threshold: Minimum weight to survive expansion/modulation.
        max_candidates: Cap on merged candidates before modulation.
        confidence_field_name: Optional ``ConfidenceField`` name on
            ``model_class`` used to modulate hop admission.
        decay_field_name: Optional ``DecayingSortedField``/
            ``CyclicDecayField`` name on ``model_class`` used to modulate
            hop admission.

    Returns:
        list[tuple[str, float]]: Discovered PKs (excluding seeds) with
        their final weight, sorted descending. Empty list on no signal or
        on any internal failure (logs a warning, never raises).
    """
    if not seed_pks:
        return []
    if co_occurrence_field is None and not relationship_field_names:
        return []

    seed_pks = list(dict.fromkeys(str(pk) for pk in seed_pks))
    candidate_weights = {}

    if co_occurrence_field is not None:
        try:
            propagated = co_occurrence_field.propagate(
                model_class,
                seed_pks,
                depth=depth,
                decay_per_hop=decay_per_hop,
                threshold=threshold,
            )
            for pk, w in propagated.items():
                if candidate_weights.get(pk, 0.0) < w:
                    candidate_weights[pk] = w
        except Exception as e:
            logger.warning("graph_traversal: co-occurrence propagation failed: %s", e)

    if relationship_field_names:
        try:
            rel_weights = expand_relationships(
                model_class,
                seed_pks,
                relationship_field_names,
                depth=depth,
                decay_per_hop=decay_per_hop,
            )
            for pk, w in rel_weights.items():
                if candidate_weights.get(pk, 0.0) < w:
                    candidate_weights[pk] = w
        except Exception as e:
            logger.warning("graph_traversal: relationship expansion failed: %s", e)

    for seed in seed_pks:
        candidate_weights.pop(seed, None)

    if not candidate_weights:
        return []

    ranked = sorted(candidate_weights.items(), key=lambda kv: kv[1], reverse=True)
    capped = ranked[:max_candidates]

    if confidence_field_name or decay_field_name:
        try:
            capped = _modulate_admission(
                model_class,
                capped,
                confidence_field_name=confidence_field_name,
                decay_field_name=decay_field_name,
                threshold=threshold,
            )
        except Exception as e:
            logger.warning("graph_traversal: admission modulation failed: %s", e)

    return sorted(capped, key=lambda kv: kv[1], reverse=True)
