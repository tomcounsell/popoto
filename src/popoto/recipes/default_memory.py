"""DefaultMemory -- the batteries-included agent-memory model (issue #513).

The agent-memory quickstart teaches a progressive schema: five levels of
fields you assemble yourself. That ladder stays, but it should not be the
*first* thing a new adopter writes. Following Level 1 verbatim into
:class:`~popoto.recipes.subconscious_memory.SubconsciousMemory` produced
silent query-blind retrieval, because a model without a ``BM25Field``
makes ``ContextAssembler(retrieval_mode='auto')`` resolve to the
``composite`` path, which ignores the query text entirely.

``DefaultMemory`` is the shipped answer: import it and you get the
benchmarked configuration -- query-sensitive lexical retrieval, decay,
confidence, and an association graph -- with no schema authoring::

    from popoto.recipes import DefaultMemory, SubconsciousMemory

    sm = SubconsciousMemory(agent_id="agent-1")

Field choices and why:

``memory_id`` (AutoKeyField)
    A generated key so callers never invent one.
``agent_id`` (KeyField)
    Partition key. Every other index partitions by it, and an explicit
    ``.filter(agent_id=...)`` query always honors that partition -- but
    the default lexical/BM25 retrieval path does not yet filter by it
    ([#576](https://github.com/tomcounsell/popoto/issues/576)), so two
    agents sharing one Redis via the default loop can retrieve each
    other's memories. Not a project-isolation boundary today.
``content`` (StringField)
    The memory text. Also the ``BM25Field`` source and the field the
    content-first injection format reads.
``importance`` (FloatField)
    Base score for time decay. ``1.0`` default means "unweighted".
``relevance`` (DecayingSortedField)
    Recency * importance, partitioned by agent. The single index in the
    benchmarked ``score_weights`` (``{"relevance": 1.0}``).
``confidence`` (ConfidenceField)
    Beta-style certainty updated by ``ObservationProtocol`` outcomes.
``content_bm25`` (BM25Field)
    The field that makes retrieval query-sensitive. Its presence is what
    flips ``retrieval_mode='auto'`` from ``composite`` to ``lexical``.
``associations`` (CoOccurrenceField)
    Entity/record co-occurrence graph. Feeds the graph arm of lexical
    retrieval and receives write-time entity links from
    ``SubconsciousMemory.extract_memories()``.

Deliberately **not** included:

``WriteFilterMixin``
    It silently discards records below ``_wf_min_threshold`` (``save()``
    returns ``False``). Silent data loss is the wrong default for a
    first-run model; the quickstart introduces it at Level 2 where the
    behavior is explained.
``EmbeddingField``
    Requires an embedding provider (an API key or a local Ollama), so it
    cannot be a zero-configuration default. Adding one to a subclass
    flips ``auto`` from ``lexical`` to ``hybrid`` with no call-site change.

All numeric behavior is left to the field defaults, which read from
``popoto.fields.constants.Defaults`` -- this module pins no constants of
its own (see the ``Defaults`` docstring for the convention).

Escape hatch: ``DefaultMemory`` is a single shared class, so every
importer writes into the ``DefaultMemory:*`` keyspace. Applications that
need their own keyspace (or extra fields) subclass it::

    class ProjectMemory(DefaultMemory):
        pass  # keys become ProjectMemory:*
"""

from ..fields.access_tracker import AccessTrackerMixin
from ..fields.bm25_field import BM25Field
from ..fields.co_occurrence_field import CoOccurrenceField
from ..fields.confidence_field import ConfidenceField
from ..fields.decaying_sorted_field import DecayingSortedField
from ..fields.shortcuts import AutoKeyField, FloatField, KeyField, StringField
from ..models.base import Model


class DefaultMemory(AccessTrackerMixin, Model):
    """Batteries-included agent memory: query-sensitive retrieval, no schema.

    Equivalent to the quickstart's Level 4 model minus ``WriteFilterMixin``.
    ``ContextAssembler(retrieval_mode='auto')`` resolves to ``"lexical"``
    over this model, so query cues actually rank results.

    Example::

        from popoto.recipes import DefaultMemory

        DefaultMemory(
            agent_id="agent-1",
            content="Deploy uses blue-green with automatic rollback",
            importance=0.9,
        ).save()

        results = DefaultMemory.query.filter(agent_id="agent-1").top_by_decay(n=5)
    """

    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = StringField(default="")
    importance = FloatField(default=1.0)
    relevance = DecayingSortedField(
        base_score_field="importance",
        partition_by="agent_id",
    )
    confidence = ConfidenceField()
    content_bm25 = BM25Field(source="content")
    associations = CoOccurrenceField()
