from .exceptions import (
    ModelException,
    QueryException,
    PublisherException,
    SubscriberException,
    SkipSaveException,
    KeyMutationError,
)
from .fields.field import Field
from .fields.shortcuts import (
    IntField,
    FloatField,
    DecimalField,
    StringField,
    BooleanField,
    ListField,
    DictField,
    SetField,
    TupleField,
    BytesField,
    TimeField,
    DateField,
    KeyField,
    AutoKeyField,
    UniqueKeyField,
    SortedField,
    SortedKeyField,
    IndexedField,
    UniqueField,
)
from .fields.decaying_sorted_field import DecayingSortedField
from .fields.access_tracker import AccessTrackerMixin
from .fields.write_filter import WriteFilterMixin
from .fields.event_stream import EventStreamMixin
from .fields.cyclic_decay_field import CyclicDecayField
from .fields.confidence_field import ConfidenceField
from .fields.co_occurrence_field import CoOccurrenceField
from .fields.existence_filter import ExistenceFilter, FrequencySketch
from .fields.bm25_field import BM25Field
from .fields.constants import TemporalPeriod, InteractionWeight, Defaults
from .fields.observation import ObservationProtocol, RecallProposal
from .fields.prediction_ledger import PredictionLedgerMixin
from .fields.geo_field import GeoField

from .fields.content_field import ContentField

try:
    from .fields.dataframe_field import DataFrameField
except ImportError:
    pass

try:
    from .fields.embedding_field import EmbeddingField
except ImportError:
    pass

from .fields.datetime_field import DatetimeField
from .fields.relationship import Relationship
from .models.base import Model, ModelBase
from .models.q import Q
from .models.expressions import Expression, CombinedExpression
from .pubsub.publisher import Publisher
from .pubsub.subscriber import Subscriber
from .recipes.context_assembler import (
    AssemblyResult,
    ContextAssembler,
    RetrievalQuality,
)
from .streams import StreamConsumer
from .redis_db import POPOTO_REDIS_DB, get_async_redis_db
from ._error_reporting import enable_error_reporting


def get_redis():
    """Get the Redis connection for direct Redis operations.

    Use this when you need Redis primitives not exposed by Popoto models:
    - Sets: SADD, SISMEMBER, SMEMBERS
    - Lists: RPUSH, LPOP, LRANGE
    - Pub/Sub: PUBLISH, SUBSCRIBE

    Returns:
        redis.Redis: The shared Redis connection

    Example:
        import popoto
        redis = popoto.get_redis()
        redis.sadd("my_set", "value")
        redis.rpush("my_queue", "item")
    """
    return POPOTO_REDIS_DB


def configure(
    embedding_provider=None,
    content_store=None,
    content_path: str = None,
):
    """Configure global defaults for ContentField and EmbeddingField.

    Call this once at application startup to set the default embedding
    provider and content store used by all fields that don't specify
    their own.

    Args:
        embedding_provider: An AbstractEmbeddingProvider instance for
            generating embeddings. Required for EmbeddingField and
            semantic_search() to work.
        content_store: An AbstractContentStore instance for content
            storage. Defaults to FilesystemStore if not specified.
        content_path: Base directory for filesystem content storage.
            Overrides POPOTO_CONTENT_PATH env var. Only applies when
            using the default FilesystemStore.

    Example:
        import popoto
        from popoto.embeddings.voyage import VoyageProvider

        popoto.configure(
            embedding_provider=VoyageProvider(api_key="your-key"),
            content_path="/data/popoto-content",
        )
    """
    if embedding_provider is not None:
        from .fields.embedding_field import set_default_provider

        set_default_provider(embedding_provider)

    if content_store is not None:
        from .fields.content_field import set_default_store

        set_default_store(content_store)
    elif content_path is not None:
        from .stores.filesystem import FilesystemStore
        from .fields.content_field import set_default_store

        set_default_store(FilesystemStore(base_path=content_path))


__all__ = [
    "Field",
    "IntField",
    "FloatField",
    "DecimalField",
    "StringField",
    "BooleanField",
    "BytesField",
    "ListField",
    "DictField",
    "SetField",
    "TupleField",
    "TimeField",
    "DateField",
    "DatetimeField",
    "KeyField",
    "AutoKeyField",
    "UniqueKeyField",
    "SortedField",
    "SortedKeyField",
    "IndexedField",
    "UniqueField",
    "DecayingSortedField",
    "AccessTrackerMixin",
    "WriteFilterMixin",
    "EventStreamMixin",
    "SkipSaveException",
    "CyclicDecayField",
    "ConfidenceField",
    "CoOccurrenceField",
    "ExistenceFilter",
    "FrequencySketch",
    "TemporalPeriod",
    "InteractionWeight",
    "Defaults",
    "ObservationProtocol",
    "RecallProposal",
    "PredictionLedgerMixin",
    "GeoField",
    "DataFrameField",
    "Relationship",
    "Model",
    "ModelBase",
    "Q",
    "Expression",
    "CombinedExpression",
    "Publisher",
    "Subscriber",
    "ModelException",
    "KeyMutationError",
    "QueryException",
    "PublisherException",
    "SubscriberException",
    "get_redis",
    "StreamConsumer",
    "get_async_redis_db",
    "ContextAssembler",
    "AssemblyResult",
    "RetrievalQuality",
    "ContentField",
    "EmbeddingField",
    "BM25Field",
    "configure",
    "enable_error_reporting",
]
