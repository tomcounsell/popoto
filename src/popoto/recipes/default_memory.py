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
from typing import Any

from .. import counters
from ..fields.access_tracker import AccessTrackerMixin
from ..fields.constants import Defaults, _read_default_memory_max_records
from ..fields.bm25_field import BM25Field
from ..fields.co_occurrence_field import CoOccurrenceField
from ..fields.confidence_field import ConfidenceField
from ..fields.decaying_sorted_field import DecayingSortedField
from ..fields.shortcuts import AutoKeyField, FloatField, KeyField, StringField
from ..models.base import Model
from ..privacy.never_record import NeverRecordMixin

logger = logging.getLogger("POPOTO.DefaultMemory")

EVICTION_COUNTER_PREFIX = "$popoto_memory:counter"
"""Redis key prefix for the durable eviction report (#596).

Duplicates ``popoto.integrations.service.COUNTER_KEY_PREFIX`` on purpose:
``recipes/`` must not import from ``integrations/``, so the string is
restated here and a test asserts the two are equal. Choosing the counter
prefix means ``MemoryService._read_counters()`` already surfaces the number
in ``status()``, the MCP ``memory_status`` tool, and ``popoto-memory doctor``.

Contract: ``{prefix}:{agent_id}:evicted`` records the cap **selected** for
eviction, not records deleted. It is incremented by ``excess`` before the
delete loop runs, and the loop can legitimately delete fewer (the saving
record's own key is skipped, a missing hash is routed to an orphan purge,
and a mid-loop error aborts). The invariant is ``counter >= records actually
deleted``, with equality on the clean path.
"""

#: ``(model class name, agent_id)`` pairs whose first over-cap eviction has
#: already been announced. Bounded by the number of distinct pairs seen in
#: this process -- fine for a hook process; a long-lived multi-tenant server
#: with unbounded agent ids grows it slowly. Accepted; no LRU.
_EVICTION_WARNED: set[tuple[str, str]] = set()


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

    def save(self, pipeline: Any = None, *args: Any, **kwargs: Any) -> Any:
        """Save, then evict the stalest records past ``_max_records_per_agent``.

        Staleness is the ``relevance`` decay timestamp, so a memory that is
        touched (recalled, acted on) stays; one nobody has refreshed goes
        first. Eviction is a full ``delete()`` so every index is cleaned.
        Costs one ``ZCARD`` per save when under the cap.
        """
        result = super().save(pipeline, *args, **kwargs)
        if pipeline is not None or result is False:
            # Inside a caller's pipeline the record's own ZADD is still
            # queued, so the cap cannot be evaluated yet; a False result is
            # a write-filter drop with nothing to evict for.
            return result
        # Precedence is asymmetric on purpose (#596): the env var may lower,
        # raise, or disable the *default* cap, but a falsy class attribute is
        # an explicit library-author opt-out that a positive env value must
        # never re-arm -- that opt-out is the escape hatch the recipes and
        # harness docs advertise.
        attr = self._max_records_per_agent
        env = _read_default_memory_max_records()
        cap: int | None
        if not attr:
            cap = attr
        elif env is not None:
            cap = env
        else:
            cap = attr
        if not cap:
            return result
        try:
            field = self._meta.fields["relevance"]
            excess = field.count(self, "relevance") - cap
            if excess <= 0:
                return result
            # Announce BEFORE deleting: the records are unrecoverable (no
            # tombstone) once the loop runs, and a mid-loop error would
            # otherwise be swallowed by the enclosing ``except``, making the
            # loudest case the quietest log. ``_EVICTION_WARNED`` is marked
            # here too, so a partial-failure path still leaves the notice.
            warn_key = (type(self).__name__, str(self.agent_id))
            if warn_key in _EVICTION_WARNED:
                logger.debug(
                    "%s eviction: agent_id=%s deleting %s record(s) over the "
                    "cap of %s",
                    warn_key[0],
                    warn_key[1],
                    excess,
                    cap,
                )
            else:
                _EVICTION_WARNED.add(warn_key)
                logger.warning(
                    "%s cap exceeded for agent_id=%s: deleting %s record(s) "
                    "to reach the cap of %s. This is permanent data loss "
                    "(no tombstone). Set "
                    "POPOTO_DEFAULT_MEMORY_MAX_RECORDS to change the cap, or "
                    "to 0/off to disable eviction.",
                    warn_key[0],
                    warn_key[1],
                    excess,
                    cap,
                )
            # Durable marker: a log record does not reach a hook subprocess
            # whose stderr the harness suppresses. Counts records *selected*
            # for eviction (see EVICTION_COUNTER_PREFIX). Inside the
            # enclosing try, so a counter failure never fails a save.
            counters.increment(
                f"{EVICTION_COUNTER_PREFIX}:{self.agent_id}:evicted", excess
            )
            own_key = self.db_key.redis_key
            for victim in field.members(self, "relevance", 0, excess - 1):
                if victim == own_key:
                    continue
                # Untracked load: a tracked get would fire on_read and stage
                # an access for a record about to be deleted.
                doomed = type(self).query.get(redis_key=victim, _no_track=True)
                if doomed is not None:
                    doomed.delete()
                else:
                    self._purge_orphan_keys([victim])
        except Exception as exc:  # eviction must never fail a save
            logger.warning("DefaultMemory eviction skipped: %s", exc)
        return result
