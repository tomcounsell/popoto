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
from typing import Any, Literal, Mapping, Optional, Sequence, Union

from ..redis_db import PopotoException

__all__ = [
    "And",
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
    "RecordId",
    "Row",
    "SaveOutcome",
    "SchemaDriftError",
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


class SchemaDriftError(BackendError):
    """The stored schema and the model disagree in a way popoto will not
    reconcile automatically (plan §3, Migrations). Raised by ``bind()``."""


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

    Built lazily by ``ModelOptions.spec`` and rebuilt when a field is added
    (the ``_auto_key`` field is added at first instantiation, after class
    creation). ``backend`` is the explicit ``Meta.backend`` or ``None`` (the
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
