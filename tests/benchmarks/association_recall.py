"""Association-recall micro-benchmark for graph-traversal retrieval (#484).

Measures the one capability #462 claims and no lexical/vector arm can have:
**retrieving X surfaces an associated Y that shares no lexical overlap with
the query.**

LoCoMo and LongMemEval cannot measure this — they ship no annotated
association graph, so any edge the harness builds there is a proxy
(conversational adjacency). This benchmark constructs the graph explicitly,
which makes the association-recall number a direct measurement of the
traversal mechanism rather than of a dataset artifact.

Protocol (one trial):
    1. Save ``n_distractors`` filler records from a disjoint vocabulary.
    2. Save an **anchor** X whose text is built from the trial's *query*
       vocabulary, so BM25 ranks it first.
    3. Save a **target** Y whose text is built from a *third* vocabulary
       sharing zero tokens with the query, with X, or with the fillers — so
       BM25 and cosine can never surface it.
    4. Link X -> Y: a self-referential ``Relationship`` edge (and, in the
       ``co_occurrence`` variant, a symmetric ``CoOccurrenceField`` edge).
    5. Query with the query vocabulary. Y is "recalled" if it lands in the
       assembler's top-k.

Arms compared (identical corpus, identical query, same seed):
    - ``lexical``   — model declares NO CoOccurrenceField, so the assembler
                      never builds a graph arm. Pure BM25 + composite.
    - ``cooccur``   — pre-#483 behaviour: the graph arm is
                      ``CoOccurrenceField.propagate()`` only, no relationship
                      walk.
    - ``graph``     — #483 traversal: CoOccurrence + Relationship expansion
                      with confidence/decay-modulated admission.

``cooccur`` and ``graph`` share an identical field set and differ only by the
``graph_traversal_relationship_fields`` kwarg, so their delta isolates PR
#483 exactly. ``lexical`` necessarily differs in field presence (the
assembler auto-detects the CoOccurrenceField), which is stated rather than
papered over.

Run with ``--edges relationship`` to build ONLY the Relationship edge: the
``cooccur`` arm then has nothing to propagate over, so any lift over it is
attributable solely to ``expand_relationships()``.

Usage:
    POPOTO_BENCH_DB=9 python -m tests.benchmarks.association_recall \
        --trials 100 --hops 1 --output tests/benchmarks/results/external/...
"""

import argparse
import json
import logging
import os
import platform
import random
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("POPOTO.Benchmark.AssociationRecall")

# Three disjoint vocabularies. No token appears in more than one, so a record
# built from one can never be retrieved by a query built from another via any
# lexical arm.
QUERY_VOCAB = [
    "quantum",
    "photon",
    "lattice",
    "isotope",
    "neutrino",
    "plasma",
    "graviton",
    "fermion",
]
TARGET_VOCAB = [
    "harpsichord",
    "oboe",
    "timpani",
    "clarinet",
    "cello",
    "bassoon",
    "marimba",
    "piccolo",
]
FILLER_VOCAB = [
    "casserole",
    "paprika",
    "skillet",
    "vinaigrette",
    "risotto",
    "brisket",
    "shallot",
    "fennel",
    "turmeric",
    "polenta",
    "gnocchi",
    "chorizo",
]

MAX_ITEMS = 10


def _build_model(prefix, with_cooccur=True):
    """Model with BM25, optionally plus the association primitives.

    ``ContextAssembler`` auto-detects a ``CoOccurrenceField`` and runs the
    graph arm whenever one is present, so a genuine no-graph baseline needs a
    model *without* the field — the ``lexical`` arm therefore differs in
    field presence, not just in assembler kwargs. The ``cooccur`` and
    ``graph`` arms share identical field sets and differ only by the
    ``graph_traversal_relationship_fields`` kwarg, so the cooccur→graph delta
    isolates PR #483's relationship-walk contribution exactly.
    """
    from src import popoto
    from src.popoto.fields.bm25_field import BM25Field
    from src.popoto.fields.co_occurrence_field import CoOccurrenceField
    from src.popoto.fields.confidence_field import ConfidenceField
    from src.popoto.fields.decaying_sorted_field import DecayingSortedField
    from src.popoto.fields.relationship import Relationship

    if with_cooccur:

        class AssocMemory(popoto.Model):
            mem_id = popoto.AutoKeyField()
            agent_id = popoto.KeyField()
            content = popoto.StringField(default="")
            importance = popoto.FloatField(default=0.5)
            relevance = DecayingSortedField(
                decay_rate=0.5,
                base_score_field="importance",
                partition_by="agent_id",
            )
            certainty = ConfidenceField(initial_confidence=0.5)
            content_index = BM25Field(source="content")
            associations = CoOccurrenceField(symmetric=True, max_edges=100)

    else:

        class AssocMemory(popoto.Model):
            mem_id = popoto.AutoKeyField()
            agent_id = popoto.KeyField()
            content = popoto.StringField(default="")
            importance = popoto.FloatField(default=0.5)
            relevance = DecayingSortedField(
                decay_rate=0.5,
                base_score_field="importance",
                partition_by="agent_id",
            )
            certainty = ConfidenceField(initial_confidence=0.5)
            content_index = BM25Field(source="content")

    AssocMemory.__name__ = f"Assoc{prefix}"
    AssocMemory.__qualname__ = f"Assoc{prefix}"
    AssocMemory.related = Relationship(model=AssocMemory, null=True)
    AssocMemory._meta.add_field("related", AssocMemory.related)
    return AssocMemory


def _sentence(rng, vocab, n=8):
    return " ".join(rng.choice(vocab) for _ in range(n))


def run_trial(arm, rng, n_distractors=50, hops=1, edges="both"):
    """Run one association-recall trial for ``arm``.

    Returns:
        dict with ``recalled`` (target in top-k), ``target_rank`` (1-based or
        None), ``anchor_rank``, and ``n_retrieved``.
    """
    from src.popoto.recipes.context_assembler import ContextAssembler

    prefix = uuid.uuid4().hex[:8]
    model = _build_model(prefix, with_cooccur=arm != "lexical")
    agent_id = f"assoc:{uuid.uuid4().hex[:12]}"
    cooccur = model._meta.fields.get("associations")

    saved = []
    try:
        for _ in range(n_distractors):
            inst = model(
                agent_id=agent_id, content=_sentence(rng, FILLER_VOCAB), importance=0.5
            )
            inst.save()
            saved.append(inst)

        query_text = _sentence(rng, QUERY_VOCAB, n=6)
        anchor = model(agent_id=agent_id, content=query_text, importance=0.5)
        anchor.save()
        saved.append(anchor)

        # Chain: anchor -> t1 -> t2 ... The final link in the chain is the
        # scored target, so hops=2 measures whether a 2-hop walk still lands.
        chain = []
        for _ in range(hops):
            node = model(
                agent_id=agent_id,
                content=_sentence(rng, TARGET_VOCAB),
                importance=0.5,
            )
            node.save()
            saved.append(node)
            chain.append(node)

        prev = anchor
        for node in chain:
            if edges in ("both", "relationship"):
                node.related = prev
                node.save()
            if cooccur is not None and edges in ("both", "cooccurrence"):
                cooccur.link(
                    model,
                    prev.db_key.redis_key,
                    node.db_key.redis_key,
                    initial_weight=0.5,
                )
            prev = node
        target = chain[-1]

        kwargs = dict(
            model_class=model,
            score_weights={"relevance": 1.0},
            max_items=MAX_ITEMS,
            retrieval_mode="auto",
        )
        if arm == "graph":
            kwargs["graph_traversal_relationship_fields"] = ["related"]
        assembler = ContextAssembler(**kwargs)

        result = assembler.assemble(query_cues={"topic": query_text}, agent_id=agent_id)
        keys = []
        for rec in result.records:
            try:
                keys.append(rec.db_key.redis_key)
            except Exception:
                keys.append("")

        target_key = target.db_key.redis_key
        anchor_key = anchor.db_key.redis_key
        target_rank = keys.index(target_key) + 1 if target_key in keys else None
        anchor_rank = keys.index(anchor_key) + 1 if anchor_key in keys else None
        return {
            "arm": arm,
            "recalled": target_rank is not None,
            "target_rank": target_rank,
            "anchor_rank": anchor_rank,
            "n_retrieved": len(keys),
        }
    finally:
        for inst in saved:
            try:
                inst.delete()
            except Exception:
                pass
        try:
            from src.popoto.redis_db import POPOTO_REDIS_DB

            cursor = 0
            while True:
                cursor, ks = POPOTO_REDIS_DB.scan(
                    cursor, match=f"*{prefix}*", count=500
                )
                if ks:
                    POPOTO_REDIS_DB.delete(*ks)
                if cursor == 0:
                    break
        except Exception:
            pass


def main():
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--distractors", type=int, default=50)
    parser.add_argument(
        "--hops",
        type=int,
        default=1,
        help="Length of the anchor->target chain (1 or 2).",
    )
    parser.add_argument(
        "--edges",
        choices=("both", "relationship", "cooccurrence"),
        default="both",
        help=(
            "Which association edge(s) to build between anchor and target. "
            "'relationship' isolates PR #483's new walk: the cooccur arm has "
            "no edge to follow, so any lift is attributable to "
            "expand_relationships()."
        ),
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--arms",
        default="lexical,cooccur,graph",
        help="Comma-separated arms to run.",
    )
    args = parser.parse_args()

    # Bench-DB isolation, same contract as run_external.
    from tests.benchmarks.run_external import _select_bench_db, _teardown_bench_db

    bench_db = _select_bench_db()

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    summary = {}
    per_trial = []
    try:
        for arm in arms:
            rng = random.Random(args.seed)
            recalled = 0
            ranks = []
            anchor_hits = 0
            for i in range(args.trials):
                r = run_trial(
                    arm,
                    rng,
                    n_distractors=args.distractors,
                    hops=args.hops,
                    edges=args.edges,
                )
                per_trial.append(dict(r, trial=i))
                recalled += bool(r["recalled"])
                if r["target_rank"]:
                    ranks.append(r["target_rank"])
                anchor_hits += bool(r["anchor_rank"])
            summary[arm] = {
                "trials": args.trials,
                "association_recall_at_k": round(recalled / args.trials, 4),
                "mean_target_rank": (
                    round(sum(ranks) / len(ranks), 2) if ranks else None
                ),
                "anchor_recall": round(anchor_hits / args.trials, 4),
            }
            print(
                f"  {arm:10s} association-recall@{MAX_ITEMS} = "
                f"{summary[arm]['association_recall_at_k']:.4f}  "
                f"(anchor recall {summary[arm]['anchor_recall']:.4f}, "
                f"mean target rank {summary[arm]['mean_target_rank']})"
            )
    finally:
        _teardown_bench_db(bench_db)

    import redis as _redis

    report = {
        "benchmark": "association_recall",
        "issue": 484,
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "config": {
            "trials": args.trials,
            "distractors": args.distractors,
            "hops": args.hops,
            "edges": args.edges,
            "seed": args.seed,
            "max_items_k": MAX_ITEMS,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "redis_py": _redis.__version__,
            "bench_db": bench_db,
        },
        "summary": summary,
        "per_trial": per_trial,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
        print(f"\nWrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
