"""Backend-neutral types that cross the model-level storage protocol (#759).

Everything here is plain data: no ``redis``, no ``psycopg``, no network. The
shapes are the plan's §2 (``docs/plans/sdlc-631-v2.md``); where an
implementation detail had to be pinned that §2 leaves open, the docstring says
so and why.

Values cross the protocol **decoded** -- no msgpack, key strings or index names
-- with one deliberate exception on the Redis side, :class:`RecordId.native`,
which carries the caller's own key object so a ``bytes`` key reaches the Redis
wire as the same token it always did (the #751 ``str`` boundary lesson).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncContextManager,
    Callable,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Union,
    runtime_checkable,
)

from ..redis_db import OUTAGE_ERRORS as _REDIS_OUTAGE_ERRORS
from ..redis_db import PopotoException

__all__ = [
    "ASYNC_PROTOCOL_METHODS",
    "And",
    "AsyncBackend",
    "BackendCapabilityError",
    "BackendError",
    "BackendUnavailableError",
    "Capabilities",
    "ComputedCol",
    "Cond",
    "Expiry",
    "FieldKind",
    "FieldSpec",
    "ModelSpec",
    "Not",
    "Op",
    "Or",
    "OrderTerm",
    "Predicate",
    "QueryCall",
    "QueryPlan",
    "RANDOM",
    "RankTerm",
    "RecordId",
    "Row",
    "SaveOutcome",
    "SchemaDriftError",
    "BackendRetryableError",
    "BackendBusyError",
    "MaintenanceIncompleteError",
    "OUTAGE_ERRORS",
    "Scored",
    "UnitOfWork",
]


# -- errors -------------------------------------------------------------------


class BackendError(PopotoException):
    """Base class for every error the storage-backend layer raises.

    ``PopotoException`` logs its message but does not pass it to
    ``Exception``, so ``str(exc)`` would be empty; this class keeps it.
    """

    def __init__(self, message: Any) -> None:
        super().__init__(message)
        self.args = (message,)


class BackendCapabilityError(BackendError, NotImplementedError):
    """The selected backend cannot do this.

    Raised for a protocol method a backend does not implement (on a non-Redis
    backend, groups D-H until their milestone), for a field the backend's
    static capability table refuses (:func:`popoto.backends.validate_spec`),
    and for a ``[PG-only]`` capability called on a Redis-bound model.
    ``NotImplementedError`` is a base so ``except NotImplementedError`` keeps
    catching the "not on this backend" case.
    """


class BackendUnavailableError(BackendError, ConnectionError):
    """The backend cannot be reached, or is not installed or configured.

    The popoto-level outage type both backends share (plan §1.1, M1): an
    unreachable server or a connect/statement timeout on Postgres, and
    selecting ``postgres`` without ``POPOTO_POSTGRES_URL`` or the ``postgres``
    extra.
    """


#: Exceptions that mean "the store is unreachable", as opposed to a bad query,
#: on **either** backend (#816): Redis's ``ConnectionError`` / ``TimeoutError``
#: pair (:data:`popoto.redis_db.OUTAGE_ERRORS`, unchanged) plus
#: :class:`BackendUnavailableError`, which a Postgres-bound model raises.
#: ``BackendUnavailableError`` subclasses the *builtin* ``ConnectionError``, not
#: redis-py's, so the Redis pair alone misses a Postgres outage. This is the
#: one answer to "is this an outage?" for every consumer in ``src/``: the
#: recipes re-raise it instead of reading an outage as "no memories", the
#: integrations service trips its breaker on it, and ``popoto-transfer``
#: reports it as a one-line error. :class:`BackendRetryableError` (deadlock,
#: serialization failure, pool contention) is deliberately **not** a member:
#: it is contention, and running the work again is safe.
OUTAGE_ERRORS: tuple[type[BaseException], ...] = _REDIS_OUTAGE_ERRORS + (
    BackendUnavailableError,
)


class SchemaDriftError(BackendError):
    """The stored schema and the model disagree in a way popoto will not
    reconcile automatically (plan §3, Migrations). Raised by ``bind()``."""


class BackendRetryableError(BackendError):
    """A deadlock or serialization failure rolled the work back; running it
    again is safe (plan §6, TD-2), so a caller catches one popoto type instead
    of a driver error (#759 M2a). Raised ``from`` the driver error:

    - inside a caller-owned ``transaction()`` (a statement in it, or its
      COMMIT), at once -- the whole unit was rolled back and only the caller
      can run its block again, so popoto does not retry it;
    - by a single statement outside a unit of work, and by
      ``ObservationProtocol.on_context_used``, once their own bounded retries
      (``Defaults.PG_TRANSACTION_RETRIES``) are spent.

    Before #773's patch a caller-owned transaction saw the raw psycopg
    ``DeadlockDetected`` / ``SerializationFailure``; code that caught those
    should catch this instead. A statement whose completion is unknown
    (40003) is not this error: it may have committed (#769)."""


class BackendBusyError(BackendRetryableError):
    """No pooled connection became free in time: every connection the
    backend's pool may open is checked out by other callers (#759 M5).

    Raised when a caller waited ``Defaults.PG_CONNECT_TIMEOUT_SECONDS`` for a
    connection and none was returned -- many concurrent ``async_*`` calls or
    ``transaction()`` blocks on one event loop, or more threads than
    ``Defaults.PG_POOL_MAX_SIZE`` on the sync pool. Every connection is in
    use, so this is contention, **not** an outage: :class:`BackendUnavailableError` is not
    raised, the backend's ``health`` record is not touched and no dropped
    write is counted. Nothing was sent, so running the call again is safe,
    which is why it is a :class:`BackendRetryableError`. A connection that
    cannot be *opened* (the server is down or unreachable) is still
    :class:`BackendUnavailableError`, and so is a wait for a slot held by a
    caller stuck connecting to an unresponsive server."""


class MaintenanceIncompleteError(BackendRetryableError):
    """``rebuild_indexes()`` on Postgres repaired what it could, then a
    ``REINDEX TABLE CONCURRENTLY`` / ``ANALYZE`` step did not finish (#759 M5,
    #788 review): it waited longer than ``Defaults.PG_MAINTAIN_LOCK_TIMEOUT_MS``
    on another session's lock (an idle-in-transaction session that wrote the
    table is the usual one), ran past ``Defaults.PG_MAINTAIN_STATEMENT_TIMEOUT_MS``,
    was cancelled, or failed on the server.

    This is maintenance that stopped early, **not** an outage: the backend's
    ``health`` record is untouched and no dropped write is counted. Every
    companion-row repair and orphan removal before the failed step has
    already committed, and running ``rebuild_indexes()`` again is safe (it is
    idempotent, and it first drops the ``*_ccnew`` indexes a failed
    ``CONCURRENTLY`` leaves behind), which is why it is a
    :class:`BackendRetryableError`.

    Attributes:
        indexed: records the side-row pass covered (the int a successful
            ``rebuild_indexes()`` returns).
        diverged_keys: rows skipped because their key columns no longer
            produce their stored ``_pk``.
        completed: the steps that finished, in order (``"side_rows"``,
            ``"orphans"``, then ``"reindex <table>"`` per table).
        failed_step: the step that did not finish.
        invalid_indexes: INVALID indexes the failure left on the model's
            tables that could not be dropped at once (the next
            ``rebuild_indexes()`` or ``clean_indexes()`` drops them, and
            ``check_indexes()`` reports them as ``invalid_indexes``).
    """

    def __init__(
        self,
        message: Any,
        *,
        indexed: int = 0,
        diverged_keys: Any = (),
        completed: Any = (),
        failed_step: str = "",
        invalid_indexes: Any = (),
    ) -> None:
        super().__init__(message)
        self.indexed = int(indexed)
        self.diverged_keys = list(diverged_keys)
        self.completed = tuple(completed)
        self.failed_step = failed_step
        self.invalid_indexes = list(invalid_indexes)


# -- identity -----------------------------------------------------------------


@dataclass(frozen=True)
class RecordId:
    """The identity of one record, backend-neutral.

    ``canonical`` is today's ``DB_key`` string -- ``Model.pk`` on both backends,
    and the Postgres ``_pk`` column (plan §3). ``values`` are the KeyField
    values in ``DB_key`` order (KeyField names sorted, as
    :meth:`ModelOptions.get_db_key_index_position` numbers them); it is ``()``
    when the id was built from a key string that was never decomposed, which no
    backend needs for a lookup by ``canonical``.

    ``native`` is not part of identity (``compare=False``): it is the key object
    the caller held -- ``str`` or the ``bytes`` a raw Redis reply produced -- and
    the Redis backend sends it unchanged, so routing a call through the protocol
    never changes the token on the wire or the type a caller gets back.
    """

    model: str
    values: tuple[Any, ...]
    canonical: str
    native: Any = field(default=None, compare=False, repr=False)

    @classmethod
    def from_key(cls, model: str, key: Any, values: tuple[Any, ...] = ()) -> RecordId:
        """Build an id from a key the caller holds (``str``, ``bytes`` or a
        ``DB_key``), keeping that object as :attr:`native`."""
        if isinstance(key, bytes):
            canonical = key.decode("utf-8", errors="surrogateescape")
        elif isinstance(key, str):
            canonical = key
        else:  # DB_key and friends render themselves
            canonical = str(getattr(key, "redis_key", key))
        return cls(model=model, values=values, canonical=canonical, native=key)

    @property
    def key(self) -> Any:
        """What the Redis backend sends: the caller's object, else the string."""
        return self.native if self.native is not None else self.canonical


# -- schema -------------------------------------------------------------------

FieldKind = str
"""The name of the popoto field class a field is (or, for a user subclass, the
nearest popoto class it derives from): ``"KeyField"``, ``"SortedField"``,
``"Field"``... A closed enum would have to grow with every field module; the
backends' capability tables key on these names instead (``validate_spec``)."""


@dataclass(frozen=True)
class FieldSpec:
    """One field, as a backend sees it. ``options`` carries what a backend may
    need beyond the type (``partition_by``, ``max_length``, ``auto``...) and,
    for a user-defined subclass, ``custom_class`` and ``overrides_hooks``."""

    name: str
    kind: FieldKind
    py_type: Optional[type]
    null: bool
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelSpec:
    """A model's storage-relevant shape, built from ``Model._meta``.

    Built lazily by ``ModelOptions.spec`` and rebuilt when a field is added.
    (The implicit ``_auto_key`` is registered at class creation, #826, so it
    is in the first spec built.) ``backend`` is the explicit ``Meta.backend`` or ``None`` (the
    process default applies).
    """

    name: str
    key_fields: tuple[str, ...]
    fields: Mapping[str, FieldSpec]
    order_by: Optional[str]
    ttl: Optional[int]
    indexes: tuple[tuple[str, ...], ...]
    backend: Optional[str] = None
    abstract: bool = False
    unique_indexes: tuple[tuple[str, ...], ...] = ()
    """The subset of :attr:`indexes` declared unique (``Meta.indexes``'s
    ``is_unique`` flag). Added in M1.1 beside §2's ``indexes``, which carries
    the field names only."""
    mixins: frozenset[str] = frozenset()
    """Names of the popoto model mixins in the model's MRO
    (``AccessTrackerMixin``, ``WriteFilterMixin``...). Added in M2a: a mixin
    that keeps per-record state needs columns of its own on Postgres."""


# -- predicates and plans -----------------------------------------------------


class Op(str, enum.Enum):
    """The closed set of lookup operators: the field-lookup suffixes of plan
    §1 plus ``valid_at`` and ``within``. ``EXACT`` is a bare ``field=value``."""

    EXACT = "exact"
    IN = "in"
    ISNULL = "isnull"
    STARTSWITH = "startswith"
    ENDSWITH = "endswith"
    CONTAINS = "contains"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    BETWEEN = "between"
    ANY = "any"
    ALL = "all"
    VALID_AT = "valid_at"
    WITHIN = "within"


@dataclass(frozen=True)
class Cond:
    field: str
    op: Op
    value: Any


@dataclass(frozen=True)
class And:
    items: tuple["Predicate", ...]


@dataclass(frozen=True)
class Or:
    items: tuple["Predicate", ...]


@dataclass(frozen=True)
class Not:
    item: "Predicate"


Predicate = Union[Cond, And, Or, Not]


@dataclass(frozen=True)
class OrderTerm:
    """A field with a direction, or :data:`RANDOM` (``sample_related_keys``)."""

    field: Optional[str]
    descending: bool = False
    random: bool = False


RANDOM = OrderTerm(field=None, random=True)


@dataclass(frozen=True)
class ComputedCol:
    """A named backend-computed value (geo distance) carried on ``Row``."""

    name: str
    expr: Any


@dataclass(frozen=True)
class QueryCall:
    """The query exactly as the public API received it.

    Not in §2's sketch; pinned here because the Redis backend's ``select`` and
    ``count`` are today's filter bodies, *moved*, and those bodies consume the
    call's own kwargs and ``Q`` objects -- re-deriving them from a compiled
    :data:`Predicate` would re-order set algebra and change the wire, which the
    POC proved is exactly how a "move" stops being one (#735, #746). A backend
    that needs a predicate tree compiles :attr:`QueryPlan.where` from this
    (``popoto.backends.compile_where``); the Redis backend replays it.

    ``query`` is the model's ``Query``; ``kind`` names which public body the
    call came from (``"filter"``, ``"count"`` or ``"keys"``); ``options`` holds
    the private execution flags those bodies take (``_no_track``,
    ``_allow_pushdown``).
    """

    query: Any
    kind: Literal["filter", "count", "keys"]
    kwargs: Mapping[str, Any]
    q_objects: Sequence[Any] = ()
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryPlan:
    """One read, as a backend executes it. ``project=()`` asks for id-only
    rows; ``None`` asks for every field."""

    where: Optional[Predicate] = None
    order_by: tuple[OrderTerm, ...] = ()
    limit: Optional[int] = None
    offset: int = 0
    project: Optional[tuple[str, ...]] = None
    as_of: Optional[float] = None
    compute: tuple[ComputedCol, ...] = ()
    source: Optional[QueryCall] = None


Row = Mapping[str, Any]
"""Decoded Python values; always carries ``"_id"`` (a :class:`RecordId`, or
``None`` for a Redis projection row, which has never carried its key)."""

Scored = list[tuple[RecordId, float]]


_dataclass_field = field  # RankTerm has a member named ``field``


@dataclass(frozen=True)
class RankTerm:
    """One arm of ``rank_composite`` (plan §2 group E): a per-record score
    and the weight it carries in the composite.

    ``kind`` names where the score comes from: ``"decay"`` (a
    ``DecayingSortedField``'s decayed score, the ``rank_decayed`` expression),
    ``"confidence"`` (a ``ConfidenceField``'s stored confidence),
    ``"access"`` (``AccessTrackerMixin``'s confirmed read count; only records
    read at least once), ``"sorted"`` (a ``SortedField``'s score), or a
    caller-supplied ``scores`` mapping (``"similarity"``, ``"co_occurrence"``).
    ``where`` is the arm's own domain -- the partition its index covers --
    because each arm of today's ``ZUNIONSTORE`` is a separate set: a record
    scores on the arms whose set holds it, and is ranked when any one does.
    """

    kind: str
    weight: float
    field: Optional[str] = None
    where: Optional["Predicate"] = None
    scores: Optional[Mapping[str, float]] = None
    options: Mapping[str, Any] = _dataclass_field(default_factory=dict)
    """Per-arm settings: a decay arm's ``confidence_field`` (modulation)."""


# -- writes -------------------------------------------------------------------


@dataclass(frozen=True)
class Expiry:
    """A record expiry: ``ttl`` seconds, or ``expire_at`` epoch seconds."""

    ttl: Optional[int] = None
    expire_at: Optional[float] = None


@dataclass(frozen=True)
class SaveOutcome:
    """What ``save`` reports. ``result`` is what ``Model.save`` returns to its
    caller: on Redis, the unit of work's pipeline when one was given, else the
    ``HSET`` reply (today's return value, kept exactly)."""

    id: Optional[RecordId]
    result: Any = None


@dataclass(frozen=True)
class Capabilities:
    """What a bound backend can do for one model (returned by ``bind``)."""

    backend: str
    groups: frozenset[str]
    field_kinds: frozenset[str]
    key_migration: bool = True
    record_ttl: bool = True

    def supports(self, group: str) -> bool:
        return group in self.groups


# -- unit of work -------------------------------------------------------------


class UnitOfWork:
    """A transaction handle: what every ``pipeline=`` kwarg becomes at the seam.

    A wrapper, not a duck-typed pipeline (POC decision 1; TD-10). ``__bool__``
    is always ``True``, so the 73 ``pipeline if pipeline`` sites in the field
    layer can never mistake an empty unit of work for "no pipeline" -- which is
    what made the POC's Postgres unit of work, with a ``__len__`` and no
    ``__bool__``, falsy while empty.

    On Redis it holds the caller's pipeline as :attr:`pipeline`. The Redis
    backend rebinds :attr:`pipeline` to whatever the field hooks hand back, so
    ``Model.save(pipeline=p)`` keeps returning exactly the object it always
    returned.
    """

    __slots__ = ("pipeline", "backend")

    def __init__(self, pipeline: Any = None, *, backend: str = "redis") -> None:
        self.pipeline = pipeline
        self.backend = backend

    def __bool__(self) -> bool:
        return True

    @property
    def is_redis_pipeline(self) -> bool:
        import redis

        return isinstance(self.pipeline, redis.client.Pipeline)

    def commit(self) -> list[Any]:
        """Execute what was queued. On Redis, ``pipeline.execute()``."""
        if self.pipeline is None:
            return []
        return list(self.pipeline.execute())

    def __enter__(self) -> UnitOfWork:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self.commit()
        elif self.pipeline is not None and hasattr(self.pipeline, "reset"):
            self.pipeline.reset()

    def __repr__(self) -> str:
        return f"<UnitOfWork {self.backend} {self.pipeline!r}>"


# -- the async twin (#759 M5) -------------------------------------------------

ASYNC_PROTOCOL_METHODS: tuple[str, ...] = (
    "bind",
    "transaction",
    "close",
    "save",
    "load",
    "delete",
    "exists",
    "increment",
    "select",
    "count",
    "field_call",
)
"""The protocol methods :class:`AsyncBackend` mirrors: groups A-C, which
today's ``async_*`` callers reach (``async_save``/``async_create``/the bulk
twins through ``save``, ``async_get``/``async_get_many`` through ``load``,
``async_filter``/``async_all``/``async_keys`` through ``select``,
``async_count`` through ``count``, ``async_delete``/``async_delete_all``
through ``delete``), plus ``field_call``, which a hydrating read reaches for
``AccessTrackerMixin`` staging. ``increment`` has no ``async_*`` caller yet
and is mirrored because it is group B."""


@runtime_checkable
class AsyncBackend(Protocol):
    """The async twin of :class:`popoto.backends.Backend` (plan §2, TD-6).

    Same arguments and results as the sync methods of
    :data:`ASYNC_PROTOCOL_METHODS`, awaited. ``transaction()`` is an *async*
    context manager whose unit of work is passed as ``pipeline=`` to the
    ``async_*`` model methods. ``run(fn, ...)`` runs sync popoto code (a
    ``Model.save``, a ``Query.get``) with its storage I/O on this backend's
    async driver: it is how the ``async_*`` model methods are routed, so the
    model layer above the seam exists once.

    Redis has no implementation: a Redis-bound model's ``async_*`` methods
    keep today's ``redis.asyncio`` / worker-thread paths unchanged.
    """

    name: str

    async def run(
        self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> Any: ...

    async def bind(self, spec: ModelSpec) -> Capabilities: ...

    def transaction(self) -> AsyncContextManager[UnitOfWork]: ...

    async def close(self) -> None: ...

    async def save(
        self,
        obj: Any,
        *,
        fields: Optional[Sequence[str]] = None,
        previous_id: Optional[RecordId] = None,
        expiry: Optional[Expiry] = None,
        uow: Optional[UnitOfWork] = None,
        **options: Any,
    ) -> SaveOutcome: ...

    async def load(
        self,
        spec: ModelSpec,
        ids: Sequence[RecordId],
        *,
        fields: Optional[Sequence[str]] = None,
        **options: Any,
    ) -> list[Optional[Row]]: ...

    async def delete(
        self,
        spec: ModelSpec,
        ids: Sequence[RecordId],
        *,
        uow: Optional[UnitOfWork] = None,
        **options: Any,
    ) -> int: ...

    async def exists(self, spec: ModelSpec, ids: Sequence[RecordId]) -> list[bool]: ...

    async def increment(
        self,
        spec: ModelSpec,
        id: RecordId,
        field: str,
        delta: Union[int, float],
        *,
        uow: Optional[UnitOfWork] = None,
        **options: Any,
    ) -> Union[int, float]: ...

    async def select(self, spec: ModelSpec, plan: QueryPlan) -> list[Row]: ...

    async def count(self, spec: ModelSpec, plan: QueryPlan) -> int: ...

    async def field_call(
        self,
        spec: ModelSpec,
        field: str,
        op: str,
        /,
        *args: Any,
        uow: Optional[UnitOfWork] = None,
        **kwargs: Any,
    ) -> Any: ...
