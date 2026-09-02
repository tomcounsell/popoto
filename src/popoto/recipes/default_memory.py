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

``NeverRecordMixin``
    The never-record firewall (#561). Included where ``WriteFilterMixin``
    is not, and the difference is not inconsistency: the write filter drops
    records it *guesses* are unimportant, which is the wrong default because
    a wrong guess is silent data loss. The firewall drops content that is
    *deterministically* a credential or explicitly marked off-the-record,
    where a wrong guess costs one memory and the alternative costs a leaked
    secret. It also drops loudly -- every drop increments an auditable
    per-reason counter (``DefaultMemory.never_record_counts()``).
    This matters most because the Claude Code / Codex / Hermes / OpenClaw
    harness writes through this model on every turn using raw turn ingestion
    (#515), so a pasted API key would otherwise be persisted verbatim.
    Deploy-level escape hatch: ``POPOTO_NEVER_RECORD_DISABLE=1``.

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

import logging

from ..fields.access_tracker import AccessTrackerMixin
from ..fields.constants import Defaults
from ..redis_db import POPOTO_REDIS_DB
from ..fields.bm25_field import BM25Field
from ..fields.co_occurrence_field import CoOccurrenceField
from ..fields.confidence_field import ConfidenceField
from ..fields.decaying_sorted_field import DecayingSortedField
from ..fields.shortcuts import AutoKeyField, FloatField, KeyField, StringField
from ..models.base import Model
from ..privacy.never_record import NeverRecordMixin

logger = logging.getLogger("POPOTO.DefaultMemory")


class DefaultMemory(NeverRecordMixin, AccessTrackerMixin, Model):
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

    #: Cap on records per ``agent_id``. See ``Defaults`` for the rationale.
    _max_records_per_agent = Defaults.DEFAULT_MEMORY_MAX_RECORDS_PER_AGENT

    def save(self, *args, **kwargs):
        """Save, then evict the stalest records past ``_max_records_per_agent``.

        Staleness is the ``relevance`` decay timestamp, so a memory that is
        touched (recalled, acted on) stays; one nobody has refreshed goes
        first. Eviction is a full ``delete()`` so every index is cleaned.
        Costs one ``ZCARD`` per save when under the cap.
        """
        result = super().save(*args, **kwargs)
        if result is False or "pipeline" in kwargs:
            return result
        cap = self._max_records_per_agent
        if not cap:
            return result
        try:
            field = self._meta.fields["relevance"]
            zset_key = field.get_partitioned_sortedset_db_key(
                self, "relevance"
            ).redis_key
            excess = POPOTO_REDIS_DB.zcard(zset_key) - cap
            if excess <= 0:
                return result
            own_key = self.db_key.redis_key
            for victim in POPOTO_REDIS_DB.zrange(zset_key, 0, excess - 1):
                victim = victim.decode() if isinstance(victim, bytes) else victim
                if victim == own_key:
                    continue
                stale = type(self).query.get(redis_key=victim)
                if stale is not None:
                    stale.delete()
                else:
                    self._purge_orphan_keys([victim])
        except Exception as exc:  # eviction must never fail a save
            logger.warning("DefaultMemory eviction skipped: %s", exc)
        return result
