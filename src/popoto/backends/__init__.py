"""The model-level storage protocol and backend selection (#759, plan §2 and §4).

``Model`` and ``Query`` keep one public API; what stores a record is a
*backend* behind the :class:`Backend` protocol below. The seam sits at the
model/query level -- ``save``, ``load``, ``select`` -- not at Redis structures,
so each backend can be native: :mod:`popoto.backends.redis` is today's code,
**moved** (hooks, index sets and Lua unchanged, and the wire byte-identical);
:mod:`popoto.backends.postgres` stores plain models in typed tables (M1b).

Status (M1)
-----------
Groups A-C (lifecycle, records, query) are implemented by both backends and
routed: ``Model.save``, ``delete``, ``exists``, ``atomic_increment``,
``load_fields``, and ``Query.get``, ``get_many``, ``get_many_objects``,
``filter``/``all``, ``count`` and ``keys`` call the model's backend. For
Redis the query layer sends ``QueryPlan.source`` (the call, replayed through
the moved filter body); for every other backend it populates ``where`` /
``order_by`` / ``limit`` / ``project`` with
:func:`popoto.backends.planning.plan_from_call`. Groups D-H are declared here
so the protocol is complete; on Redis they are not routed yet (the field and
model code keeps calling today's implementation directly). On Postgres, M2a
implements ``touch``, ``update_confidence``, ``rank_decayed`` and
``rank_composite`` -- the field and model layer branches to them only for a
non-Redis model (:mod:`popoto.backends.routing`) -- and the rest raise
:class:`BackendCapabilityError` until their milestone.

Selection (plan §4)
-------------------
* ``Meta.backend = "redis" | "postgres"`` on a model wins.
* Otherwise the process default: :func:`set_backend` if called, else the
  ``POPOTO_BACKEND`` environment variable, else ``"redis"``. The variable is
  read on each :func:`get_backend` call that needs it -- never at import -- and
  ``POSTGRES_URL``/``DATABASE_URL`` are never read: selecting Postgres is
  always explicit.
* ``bind()`` is lazy and memoised per (backend, model): it runs on a model's
  first backend use, never at class creation, so importing a model module
  never dials a database. Class creation runs only :func:`validate_spec`, a
  pure check against static capability tables.

The stale-client trap (CLAUDE.md, #655)
---------------------------------------
:class:`~popoto.backends.redis.RedisBackend` stores no client. Every operation
calls ``get_REDIS_DB()`` when it runs, so ``set_REDIS_DB_settings()`` and the
pytest plugin's DB swap are observed by a backend instance created before
them. This package never imports ``POPOTO_REDIS_DB`` by name, and
``popoto/__init__.py`` does not re-export a cached backend instance.
"""

from __future__ import annotations

import os
import threading
from typing import (
    Any,
    Collection,
    ContextManager,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Union,
    runtime_checkable,
)

from .types import (
    RANDOM,
    And,
    BackendCapabilityError,
    BackendError,
    BackendRetryableError,
    BackendUnavailableError,
    Capabilities,
    ComputedCol,
    Cond,
    Expiry,
    FieldKind,
    FieldSpec,
    ModelSpec,
    Not,
    Op,
    Or,
    OrderTerm,
    Predicate,
    QueryCall,
    QueryPlan,
    RankTerm,
    RecordId,
    Row,
    SaveOutcome,
    SchemaDriftError,
    Scored,
    UnitOfWork,
)

__all__ = [
    "BACKEND_NAMES",
    "DEFAULT_BACKEND",
    "PROTOCOL_METHODS",
    "RANDOM",
    "And",
    "Backend",
    "BackendCapabilityError",
    "BackendError",
    "BackendRetryableError",
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
    "RankTerm",
    "RecordId",
    "Row",
    "SaveOutcome",
    "SchemaDriftError",
    "Scored",
    "UnitOfWork",
    "backend_for",
    "build_model_spec",
    "compile_where",
    "default_backend_name",
    "get_backend",
    "record_id",
    "reset_bindings",
    "set_backend",
    "validate_spec",
]


BACKEND_NAMES: tuple[str, ...] = ("redis", "postgres")
"""Every name ``Meta.backend`` and ``POPOTO_BACKEND`` accept."""

DEFAULT_BACKEND = "redis"
"""The process default when neither :func:`set_backend` nor
``POPOTO_BACKEND`` names one."""

BACKEND_ENV_VAR = "POPOTO_BACKEND"

# Names, never values: a non-Redis backend must still answer these with
# BackendCapabilityError rather than AttributeError.
PROTOCOL_METHODS: dict[str, tuple[str, ...]] = {
    "A": ("bind", "transaction", "close"),
    "B": ("save", "load", "delete", "exists", "increment"),
    "C": ("select", "count"),
    "D": ("touch", "update_confidence", "supersede", "chain"),
    "E": ("rank_decayed", "rank_composite", "vector_search", "keyword_search"),
    "F": ("graph_update", "graph_expand"),
    "G": ("membership_add", "membership_query"),
    "H": ("maintain", "field_call"),
}
"""The 24 protocol methods by plan §2 group. Groups A-C are routed (M1)."""


@runtime_checkable
class Backend(Protocol):
    """The 24 model-level storage operations (plan §2).

    Signatures follow the plan's sketch. Where a Redis implementation needs
    more than the sketch carries to stay a *move* -- the field hooks' own
    ``ignore_errors``/``**kwargs``, and which of two wire shapes today's caller
    used -- the method takes ``**options``. Each documents the options the
    Redis backend reads; a backend ignores options it does not know.
    """

    name: str

    # -- A. lifecycle (3) ----------------------------------------------------

    def bind(self, spec: ModelSpec) -> Capabilities:
        """LAZY: first backend use per model (memoised by :func:`get_backend`).
        Connect, version check, DDL, schema check. Redis: a no-op."""
        ...

    def transaction(self) -> ContextManager[UnitOfWork]:
        """``popoto.batch()`` and every ``pipeline=`` kwarg. Redis: a
        ``MULTI``/``EXEC`` pipeline, executed on a clean exit."""
        ...

    def close(self) -> None: ...

    # -- B. records (5) --------------------------------------------------------

    def save(
        self,
        obj: Any,
        *,
        fields: Optional[Sequence[str]] = None,
        previous_id: Optional[RecordId] = None,
        expiry: Optional[Expiry] = None,
        uow: Optional[UnitOfWork] = None,
        **options: Any,
    ) -> SaveOutcome:
        """Persist ``obj``; ``fields`` is ``update_fields``. ``expiry=None``
        means the instance's own TTL settings (``Meta.ttl``, ``save(ttl=)``).
        Redis options: ``ignore_errors`` and the caller's hook ``**kwargs``."""
        ...

    def load(
        self,
        spec: ModelSpec,
        ids: Sequence[RecordId],
        *,
        fields: Optional[Sequence[str]] = None,
        **options: Any,
    ) -> list[Optional[Row]]:
        """One row per id, in order; ``None`` where there is no record.
        Redis option ``pipelined`` (default ``True``): ``False`` reproduces a
        bare ``HGETALL``/``HGET``/``HMGET`` for a single-record caller."""
        ...

    def delete(
        self,
        spec: ModelSpec,
        ids: Sequence[RecordId],
        *,
        uow: Optional[UnitOfWork] = None,
        **options: Any,
    ) -> int:
        """Delete; returns how many existed (``0`` when queued on ``uow``).
        Redis option ``objs``: the instances, aligned with ``ids``, whose
        ``on_delete`` hooks clean the indexes; plus the hook ``**kwargs``."""
        ...

    def exists(self, spec: ModelSpec, ids: Sequence[RecordId]) -> list[bool]: ...

    def increment(
        self,
        spec: ModelSpec,
        id: RecordId,
        field: str,
        delta: Union[int, float],
        *,
        uow: Optional[UnitOfWork] = None,
        **options: Any,
    ) -> Union[int, float]:
        """Atomically add ``delta``; returns the new value (on Redis with a
        ``uow``, the queued pipeline -- today's return). Redis option ``obj``:
        the instance whose in-memory value and sorted index follow."""
        ...

    # -- C. query (2) ----------------------------------------------------------

    def select(self, spec: ModelSpec, plan: QueryPlan) -> list[Row]:
        """Rows matching ``plan``; ``project=()`` returns id-only rows."""
        ...

    def count(self, spec: ModelSpec, plan: QueryPlan) -> int: ...

    # -- D. memory state (4) ---------------------------------------------------

    def touch(
        self,
        spec: ModelSpec,
        id: RecordId,
        field: str,
        *,
        at: float,
        uow: Optional[UnitOfWork] = None,
    ) -> float: ...

    def update_confidence(
        self,
        spec: ModelSpec,
        id: RecordId,
        field: str,
        signal: float,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> Any: ...

    def supersede(
        self,
        spec: ModelSpec,
        field: str,
        *,
        successor: RecordId,
        incumbent: Optional[RecordId],
        identity: Optional[str],
        mode: Any,
        valid_from: Optional[float],
        invalid_at: Optional[float],
        now: float,
        uow: Optional[UnitOfWork] = None,
    ) -> Any: ...

    def chain(self, spec: ModelSpec, field: str, id: RecordId) -> list[RecordId]: ...

    # -- E. ranking and retrieval (4) -----------------------------------------

    def rank_decayed(
        self,
        spec: ModelSpec,
        field: str,
        *,
        now: float,
        n: Optional[int],
        where: Optional[Predicate] = None,
        as_of: Optional[float] = None,
        decay_rate: Optional[float] = None,
        base_score_field: Optional[str] = None,
        confidence_field: Optional[str] = None,
        validity_field: Optional[str] = None,
    ) -> Scored: ...

    def rank_composite(
        self,
        spec: ModelSpec,
        terms: Sequence[Any],
        *,
        limit: int,
        aggregate: Literal["SUM", "MAX", "MIN"],
        min_score: Optional[float],
        where: Optional[Predicate],
        as_of: Optional[float],
        temperature: float,
    ) -> Scored: ...

    def vector_search(
        self,
        spec: ModelSpec,
        field: str,
        query: Sequence[float],
        *,
        limit: int,
        where: Optional[Predicate] = None,
        min_score: Optional[float] = None,
    ) -> Scored: ...

    def keyword_search(
        self,
        spec: ModelSpec,
        field: str,
        tokens: Sequence[str],
        *,
        limit: int,
        where: Optional[Predicate] = None,
        allowed: Optional[Collection[RecordId]] = None,
    ) -> Scored: ...

    # -- F. graph (2) ----------------------------------------------------------

    def graph_update(
        self,
        spec: ModelSpec,
        field: str,
        op: Any,
        src: RecordId,
        dst: Optional[RecordId],
        amount: Optional[float],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> None: ...

    def graph_expand(
        self,
        spec: ModelSpec,
        field: str,
        seeds: Sequence[RecordId],
        *,
        depth: int,
        decay_per_hop: float,
        threshold: float,
        fanout: Optional[int],
    ) -> Scored: ...

    # -- G. membership (2) -----------------------------------------------------

    def membership_add(
        self,
        spec: ModelSpec,
        field: str,
        tokens: Sequence[str],
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> None: ...

    def membership_query(
        self,
        spec: ModelSpec,
        field: str,
        tokens: Sequence[str],
        *,
        mode: Literal["any", "each", "count"],
    ) -> Union[list[int], list[bool], bool]: ...

    # -- H. maintenance and extension (2) -------------------------------------

    def maintain(
        self,
        spec: ModelSpec,
        op: Literal["check", "clean", "rebuild"],
        *,
        batch_size: int = 1000,
    ) -> Any: ...

    def field_call(
        self,
        spec: ModelSpec,
        field: str,
        op: str,
        /,
        *args: Any,
        uow: Optional[UnitOfWork] = None,
        **kwargs: Any,
    ) -> Any: ...


# -- static capability tables (validate_spec) ---------------------------------

#: Field kinds each backend can store, by popoto class name. ``None`` = all.
#: Postgres lists the M1 plain-model slice (plan §5 M1); later milestones add
#: to it. Redis stores everything popoto ships.
STATIC_FIELD_KINDS: dict[str, Optional[frozenset[str]]] = {
    "redis": None,
    "postgres": frozenset(
        {
            "Field",
            "KeyField",
            "UniqueKeyField",
            "AutoKeyField",
            "IntField",
            "FloatField",
            "StringField",
            "DatetimeField",
            "SortedField",
            # Same §3 type-map row as IntField/FloatField; scalar columns
            # with no index, so they come with the M1 slice (#759 M1b).
            "DecimalField",
            "BooleanField",
            # A SortedField that is also part of the key: the same column
            # and B-tree, under the key UNIQUE.
            "SortedKeyField",
            # M1.1, plain-field breadth (plan §5 M1.1): a typed column with a
            # B-tree (UNIQUE when unique), text[] + GIN for tags, the target
            # _pk for a Relationship, jsonb for the collections, bytea/date/
            # time for the remaining scalars.
            "IndexedField",
            "UniqueField",
            "TagField",
            "Relationship",
            "ListField",
            "DictField",
            "SetField",
            "TupleField",
            "BytesField",
            "DateField",
            "TimeField",
            # M2a, Valor's ranking and memory state (plan §5 M2): the decay
            # clock is the field's own column, confidence adds four state
            # columns (.postgres.memory).
            "DecayingSortedField",
            "ConfidenceField",
        }
    ),
}

#: M2b (search, plan §5 M2): the BM25 postings, the pgvector column and the
#: exact membership tables (:mod:`popoto.backends.postgres.search`), plus
#: ``ContentField`` as a ``text`` column -- the M2b gate files' models take
#: their text from one (pulled forward from M5).
SEARCH_FIELD_KINDS: frozenset[str] = frozenset(
    {
        "BM25Field",
        "EmbeddingField",
        "ExistenceFilter",
        "FrequencySketch",
        "ContentField",
    }
)
STATIC_FIELD_KINDS["postgres"] = (
    STATIC_FIELD_KINDS["postgres"] or frozenset()
) | SEARCH_FIELD_KINDS
#: Popoto mixins a Postgres model may not declare yet, with the milestone that
#: brings each. ``PredictionLedgerMixin`` keeps its ledger in Redis structures
#: (``RESOLVE_PREDICTION_LUA``); on a Postgres model ``record_prediction`` and
#: ``auto_resolve`` would issue Redis commands for a record Redis does not
#: hold, so it is refused at bind rather than silently doing nothing
#: (#773 review). Its companion table arrives in M5 (plan §5).
_POSTGRES_REFUSED_MIXINS: dict[str, str] = {
    "PredictionLedgerMixin": "M5",
}

#: ``type=`` values an ``IndexedField`` / ``UniqueField`` may carry on
#: Postgres: the scalar column types (a B-tree on a collection is refused).
_POSTGRES_INDEXED_TYPES: frozenset[str] = frozenset(
    {"int", "float", "str", "bool", "datetime", "Decimal", "date", "time"}
)

#: ``type=`` values a ``SortedField`` may carry on Postgres: the sortable
#: types ``SortedFieldMixin.convert_to_numeric`` scores.
_POSTGRES_SORTED_TYPES: frozenset[str] = frozenset(
    {"int", "float", "Decimal", "datetime", "date", "time"}
)

#: ``type=`` values a plain ``Field`` may carry on Postgres (M1, plus the
#: M1.1 collection and scalar types).
_POSTGRES_PLAIN_TYPES: frozenset[str] = frozenset(
    {
        "int",
        "float",
        "str",
        "bool",
        "datetime",
        "Decimal",
        "date",
        "time",
        "bytes",
        "list",
        "dict",
        "set",
        "tuple",
    }
)

#: Field hook methods; a user subclass overriding one is Redis-only (plan
#: architect decision 4).
FIELD_HOOKS: tuple[str, ...] = (
    "on_save",
    "on_delete",
    "filter_query",
    "format_value_pre_save",
    "pre_save_validate",
)


def _is_popoto_class(cls: type) -> bool:
    module = getattr(cls, "__module__", "") or ""
    return module == "popoto" or module.startswith(("popoto.", "src.popoto."))


def _field_spec(name: str, field: Any) -> FieldSpec:
    """Describe one ``Field`` instance. Pure attribute reads; never raises
    for an unusual field, because it runs on every model."""
    cls = type(field)
    base = next((c for c in cls.__mro__ if _is_popoto_class(c)), cls)
    options: dict[str, Any] = {}
    for attr in ("partition_by", "max_length", "auto", "unique"):
        value = getattr(field, attr, None)
        if value not in (None, (), [], False):
            options[attr] = value
    if base.__name__ in SEARCH_FIELD_KINDS:
        # The search compilers (postgres/search.py) read the live field at
        # first use -- its source, provider dimensions, fingerprint_fn --
        # rather than a snapshot from class creation, when a provider that
        # popoto.configure() sets later is not known yet.
        options["field_ref"] = field
        source = getattr(field, "source", None)
        if source:
            options["source"] = source
    for attr in ("decay_rate", "base_score_field", "initial_confidence"):
        # M2a memory fields: what the Postgres ranking and confidence SQL
        # needs from the field definition (.postgres.memory).
        value = getattr(field, attr, None)
        if value is not None and not callable(value):
            options[attr] = value
    if getattr(field, "evidence_cap", None) is not None:
        options["evidence_cap"] = field.evidence_cap
    if getattr(field, "_capped", False):
        # ListField(max_length=N): Redis keeps it in its own list key; on
        # Postgres it is a jsonb column with type-tagged elements.
        options["capped"] = True
    if base is not cls:
        options["custom_class"] = f"{cls.__module__}.{cls.__qualname__}"
        # Look in the user classes' own namespaces: hooks are classmethods on
        # some fields, so attribute identity is not a reliable comparison.
        user_classes = cls.__mro__[: cls.__mro__.index(base)]
        overridden = tuple(
            hook for hook in FIELD_HOOKS if any(hook in vars(c) for c in user_classes)
        )
        if overridden:
            options["overrides_hooks"] = overridden
    py_type = getattr(field, "type", None)
    return FieldSpec(
        name=name,
        kind=base.__name__,
        py_type=py_type if isinstance(py_type, type) else None,
        null=bool(getattr(field, "null", False)),
        options=options,
    )


def build_model_spec(meta: Any) -> ModelSpec:
    """Build a :class:`ModelSpec` from a ``ModelOptions`` (``Model._meta``)."""
    fields = {name: _field_spec(name, f) for name, f in meta.fields.items()}
    return ModelSpec(
        name=meta.model_name,
        key_fields=tuple(sorted(meta.key_field_names)),
        fields=fields,
        order_by=meta.order_by,
        ttl=meta.ttl,
        indexes=tuple(tuple(names) for names, _unique in (meta.indexes or ())),
        backend=getattr(meta, "backend", None),
        abstract=bool(getattr(meta, "abstract", False)),
        unique_indexes=tuple(
            tuple(names) for names, unique in (meta.indexes or ()) if unique
        ),
        mixins=frozenset(getattr(meta, "mixins", ()) or ()),
    )


def validate_spec(spec: ModelSpec, backend_name: str) -> None:
    """Refuse, statically, a model the named backend cannot store.

    Pure: no network, no import of a backend driver. Runs at class creation
    for a model with an explicit ``Meta.backend`` (plan, "Declaration versus
    bind" step 1), and at first use for one that takes the process default.
    Raises :class:`BackendCapabilityError` naming the field and the reason.
    """
    if backend_name not in STATIC_FIELD_KINDS:
        raise BackendCapabilityError(
            f"unknown backend {backend_name!r}; expected one of {BACKEND_NAMES}"
        )
    kinds = STATIC_FIELD_KINDS[backend_name]
    if kinds is None:
        return
    problems: list[str] = []
    for fs in spec.fields.values():
        if fs.kind not in kinds:
            problems.append(f"{fs.name} ({fs.kind}) is not supported yet")
        elif fs.options.get("overrides_hooks"):
            hooks = ", ".join(fs.options["overrides_hooks"])
            problems.append(
                f"{fs.name} ({fs.options.get('custom_class')}) overrides {hooks}; "
                "hook-overriding custom fields are Redis-only"
            )
        elif fs.kind == "Field" and (
            fs.py_type is None or fs.py_type.__name__ not in _POSTGRES_PLAIN_TYPES
        ):
            type_name = getattr(fs.py_type, "__name__", fs.py_type)
            problems.append(f"{fs.name} (Field, type={type_name}) is not supported yet")
        elif fs.kind in ("IndexedField", "UniqueField") and (
            fs.py_type is None or fs.py_type.__name__ not in _POSTGRES_INDEXED_TYPES
        ):
            type_name = getattr(fs.py_type, "__name__", fs.py_type)
            problems.append(
                f"{fs.name} ({fs.kind}, type={type_name}) is not supported: an "
                "indexed field needs a scalar column type on Postgres"
            )
        elif fs.kind == "ConfidenceField" and fs.options.get("partition_by"):
            problems.append(
                f"{fs.name} (ConfidenceField, partition_by=) is not supported "
                "yet: partitioned confidence arrives in M3"
            )
        elif fs.kind in ("SortedField", "SortedKeyField") and (
            fs.py_type is None or fs.py_type.__name__ not in _POSTGRES_SORTED_TYPES
        ):
            type_name = getattr(fs.py_type, "__name__", fs.py_type)
            problems.append(
                f"{fs.name} (SortedField, type={type_name}) is not supported yet"
            )
    for mixin in sorted(spec.mixins):
        if mixin in _POSTGRES_REFUSED_MIXINS:
            problems.append(
                f"{mixin} is not supported yet (it arrives in "
                f"{_POSTGRES_REFUSED_MIXINS[mixin]})"
            )
    if spec.ttl is not None:
        problems.append("Meta.ttl (record expiry arrives in M5)")
    if problems:
        raise BackendCapabilityError(
            f"{spec.name} cannot use the {backend_name!r} backend: "
            + "; ".join(problems)
        )


# -- predicate compiler -------------------------------------------------------

_RESULT_MODIFIERS = frozenset({"limit", "order_by", "values"})
_OPS_BY_SUFFIX = {op.value: op for op in Op}


def _cond(lookup: str, value: Any) -> Cond:
    name, sep, suffix = lookup.rpartition("__")
    if sep and suffix in _OPS_BY_SUFFIX:
        return Cond(field=name, op=_OPS_BY_SUFFIX[suffix], value=value)
    return Cond(field=lookup, op=Op.EXACT, value=value)


def _compile_kwargs(kwargs: Mapping[str, Any]) -> list[Predicate]:
    return [
        _cond(key, value)
        for key, value in kwargs.items()
        if key not in _RESULT_MODIFIERS and not key.startswith("_")
    ]


def _compile_q(q: Any) -> Optional[Predicate]:
    children = getattr(q, "children", None) or []
    if children:
        compiled = [c for c in (_compile_q(child) for child in children) if c]
        node: Optional[Predicate]
        if not compiled:
            node = None
        elif len(compiled) == 1:
            node = compiled[0]
        elif getattr(q, "connector", "AND") == "OR":
            node = Or(tuple(compiled))
        else:
            node = And(tuple(compiled))
    else:
        leaves = _compile_kwargs(getattr(q, "filters", {}) or {})
        node = (
            None
            if not leaves
            else leaves[0] if len(leaves) == 1 else And(tuple(leaves))
        )
    if node is not None and getattr(q, "negated", False):
        node = Not(node)
    return node


def compile_where(call: QueryCall) -> Optional[Predicate]:
    """Compile a :class:`QueryCall`'s filters and ``Q`` objects to a
    :data:`Predicate` tree (``None`` = no filter).

    Pure. The Redis backend never calls it -- it replays the call through
    today's filter code. With a model on the call (``call.query``) it is the
    *validated* compile the query layer uses for every other backend
    (:func:`popoto.backends.planning.plan_from_call`): an unknown field or
    ``__operator`` raises ``QueryException`` (#768 review). Without one it is
    the purely syntactic compile M1a shipped, which keeps an unknown suffix as
    part of the field name and leaves the refusal to the backend.
    """
    model_class = getattr(call.query, "model_class", None)
    if model_class is not None and hasattr(model_class, "_meta"):
        from .planning import plan_from_call

        return plan_from_call(call).where
    parts = _compile_kwargs(call.kwargs)
    parts.extend(p for p in (_compile_q(q) for q in call.q_objects) if p is not None)
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else And(tuple(parts))


# -- selection ----------------------------------------------------------------

_lock = threading.RLock()
_default: Union[str, Backend, None] = None
_instances: dict[str, Backend] = {}
_bound: dict[tuple[int, str, int], tuple[Any, Capabilities]] = {}
"""Memoised ``bind()`` results, keyed by (backend, model name, model class).

Keyed by the class itself (its ``id``, with the class held in the value so
the ``id`` cannot be recycled while the entry lives), not by the name alone:
two classes that share a name -- the same model name declared in two modules
-- must each bind, or the second would ride on the first's checked schema
(#768 review). The name stays in the key so :func:`reset_bindings` can match
it."""


def _check_name(name: str) -> str:
    if name not in BACKEND_NAMES:
        raise ValueError(
            f"unknown popoto backend {name!r}; expected one of "
            f"{', '.join(BACKEND_NAMES)}"
        )
    return name


def default_backend_name() -> str:
    """The process default's name: :func:`set_backend`, else
    ``POPOTO_BACKEND``, else ``"redis"``. Reads the environment on each call."""
    current = _default
    if isinstance(current, str):
        return current
    if current is not None:
        return getattr(current, "name", type(current).__name__)
    raw = os.environ.get(BACKEND_ENV_VAR, "").strip().lower()
    return _check_name(raw) if raw else DEFAULT_BACKEND


def _instance(name: str) -> Backend:
    with _lock:
        backend = _instances.get(name)
        if backend is not None:
            return backend
        if name == "redis":
            from .redis import RedisBackend

            backend = RedisBackend()
        elif name == "postgres":
            # Imported here, not at module scope: psycopg is an optional
            # extra and `import popoto` must never need it. The DSN comes
            # from POPOTO_POSTGRES_URL only (never POSTGRES_URL/DATABASE_URL).
            from .postgres import backend_from_env

            backend = backend_from_env()
        else:  # pragma: no cover - _check_name refuses first
            raise ValueError(name)
        _instances[name] = backend
        return backend


def _resolve(model: Any = None) -> Backend:
    meta = getattr(model, "_meta", None) if model is not None else None
    explicit = getattr(meta, "backend", None)
    if explicit:
        return _instance(_check_name(explicit))
    current = _default
    if current is not None and not isinstance(current, str):
        return current
    return _instance(default_backend_name())


def get_backend(model: Any = None) -> Backend:
    """The backend for ``model`` (a ``Model`` class or instance), or the
    process default when ``model`` is ``None``.

    For a model, the backend is bound to it first if it has not been yet:
    :func:`validate_spec` (for a model that took the process default), then
    ``backend.bind(spec)``, memoised per (backend, model).
    """
    backend = _resolve(model)
    if model is not None:
        model_cls = model if isinstance(model, type) else type(model)
        _ensure_bound(backend, model_cls)
    return backend


backend_for = get_backend


def _ensure_bound(backend: Backend, model_cls: Any) -> Capabilities:
    meta = model_cls._meta
    memo = (id(backend), meta.model_name, id(model_cls))
    entry = _bound.get(memo)
    if entry is not None:
        return entry[1]
    with _lock:
        entry = _bound.get(memo)
        if entry is None:
            spec = meta.spec
            if spec.backend is None and backend.name in STATIC_FIELD_KINDS:
                validate_spec(spec, backend.name)
            entry = (model_cls, backend.bind(spec))
            _bound[memo] = entry
    return entry[1]


def set_backend(backend: Union[str, Backend, None]) -> Union[str, Backend, None]:
    """Set the process default backend (a name or an instance); ``None``
    returns to ``POPOTO_BACKEND``/``"redis"``. Returns the previous setting,
    so a caller can restore it. Discards memoised bindings."""
    global _default
    if isinstance(backend, str):
        _check_name(backend)
    with _lock:
        previous = _default
        _default = backend
        _bound.clear()
    return previous


def _swap_instance(name: str, backend: Optional[Backend]) -> Optional[Backend]:
    """Install ``backend`` as the instance ``Meta.backend = name`` resolves
    to (``None`` removes it, so the next use builds one from the
    environment); returns the previous one. For the pytest plugin's
    conformance legs. Discards memoised bindings."""
    _check_name(name)
    with _lock:
        previous = _instances.pop(name, None)
        if backend is not None:
            _instances[name] = backend
        _bound.clear()
    return previous


def reset_bindings(models: Optional[Sequence[Any]] = None) -> None:
    """Discard memoised ``bind()`` state -- for ``models`` (classes or names),
    or for every model -- so the next use binds again."""
    with _lock:
        if models is None:
            _bound.clear()
            return
        names = {m if isinstance(m, str) else m._meta.model_name for m in models}
        for memo in [k for k in _bound if k[1] in names]:
            del _bound[memo]


def record_id(obj: Any, key: Any = None) -> RecordId:
    """The :class:`RecordId` of a model instance (``key`` overrides which key
    string it carries, e.g. the key it was last saved under)."""
    meta = obj._meta
    values = tuple(getattr(obj, name, None) for name in sorted(meta.key_field_names))
    if key is None:
        key = obj.db_key.redis_key
    return RecordId.from_key(meta.model_name, key, values)
