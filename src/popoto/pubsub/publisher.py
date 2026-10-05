"""
Publisher module for Redis pub/sub messaging in Popoto.

This module provides the Publisher mixin class that enables any Python object
to broadcast messages to Redis channels. It forms one half of Popoto's pub/sub
system (paired with Subscriber), enabling decoupled, event-driven architectures
where components communicate without direct knowledge of each other.

Design Philosophy:
    The Publisher is designed as an abstract base class (ABC) that can be mixed
    into any class hierarchy. This allows Model subclasses to gain publishing
    capabilities simply by inheriting from Publisher alongside Model. The mixin
    approach keeps publishing logic separate from persistence logic while allowing
    seamless integration.

    Channel names default to the class name, supporting the convention that each
    model type broadcasts on its own channel. This creates natural topic boundaries
    without requiring explicit configuration.

Serialization:
    Messages are serialized using msgpack with numpy support (msgpack-numpy),
    making this publisher suitable for high-performance numerical data streams
    such as financial time-series or scientific computing applications.

Example:
    Publishing from a custom model on save::

        class StockPrice(popoto.Model, popoto.Publisher):
            symbol = popoto.KeyField()
            price = popoto.FloatField()

            def save(self, pipeline=None, **kwargs):
                super().save(pipeline=pipeline, **kwargs)
                self.publish({"symbol": self.symbol, "price": self.price},
                            pipeline=pipeline)

Backends (#759 M5):
    A publisher publishes on its backend: a ``Model`` subclass on the
    model's, otherwise ``backend=`` (a name or instance) at construction,
    otherwise the process default. On Postgres a message is a ``pg_notify``
    (delivered when its transaction commits; inside the backend's unit of
    work passed as ``pipeline=``, when *that* commits) and must encode to
    under 8000 bytes -- see ``popoto.backends.postgres.pubsub``.

See Also:
    - Subscriber: The receiving end of the pub/sub system
    - redis_db: Connection management for Redis
"""

import typing
from abc import ABC
import logging

import redis

from ..redis_db import get_REDIS_DB
import msgpack

logger = logging.getLogger("POPOTO-publisher")


class PublisherException(Exception):
    """Raised when a publish operation fails (e.g. missing channel name).

    This exception indicates a problem with the publish operation itself,
    such as attempting to publish without specifying a channel. It is distinct
    from Redis connection errors, which propagate as redis.exceptions.
    """

    pass


class Publisher(ABC):
    """Abstract base class for publishing msgpack-encoded messages to Redis channels.

    Subclass and call :meth:`publish` to send data. The channel name defaults
    to the class name but can be overridden via the constructor or per-call.

    Publisher enables any class to broadcast messages to Redis channels, forming
    the sender side of Popoto's publish-subscribe messaging system. It is designed
    to be mixed into class hierarchies (especially with Model) to add event
    broadcasting without coupling business logic to specific subscribers.

    The class uses cooperative multiple inheritance via super().__init__(), allowing
    it to be safely combined with Model and other base classes. Channel names
    default to the class name, establishing a convention where each model type
    has its own broadcast channel.

    Design Decisions:
        - ABC inheritance signals this is meant to be subclassed, not instantiated
          directly in production code
        - Class-level _publish_data allows stateful publishing where data can be
          set incrementally before calling publish()
        - Pipeline support enables atomic publish-with-save operations, ensuring
          messages are only sent if the associated database write succeeds

    Attributes:
        _channel_name: The Redis channel to publish to. Defaults to the class name.
        _publish_data: Cached data for the next publish operation. Useful for
            building up complex messages across multiple method calls.

    Example:
        Basic standalone publisher::

            publisher = Publisher(channel_name="price_updates")
            publisher.publish({"symbol": "AAPL", "price": 150.25})

        Mixin with Model for automatic save notifications::

            class Order(popoto.Model, Publisher):
                def save(self, **kwargs):
                    super().save(**kwargs)
                    self.publish({"event": "order_created", "id": self.db_key})
    """

    _channel_name: str = ""
    _publish_data: dict = {}

    def __init__(self, *args, **kwargs):
        """
        Initialize the publisher with an optional channel name.

        Uses cooperative inheritance to work correctly in multiple inheritance
        scenarios. If no channel_name is provided, defaults to the class name,
        establishing the convention that each class publishes to its own channel.

        Args:
            *args: Passed to parent classes via super().
            **kwargs: May contain 'channel_name' to override the default channel.
                All kwargs are passed to parent classes.
        """
        self._pubsub_backend = kwargs.pop("backend", None)
        # Popped, not read (#759 M5): passed on, it reached object.__init__,
        # so Publisher(channel_name=...) -- the documented call -- raised
        # TypeError.
        self._channel_name = kwargs.pop("channel_name", self.__class__.__name__)
        super().__init__(*args, **kwargs)

    @property
    def channel_name(self):
        """
        The Redis channel name this publisher broadcasts to.

        Defaults to the class name, supporting the convention that each model
        type has its own dedicated channel. Can be overridden per-instance for
        custom routing scenarios.

        Returns:
            str: The channel name for Redis pub/sub operations.
        """
        return self._channel_name

    @channel_name.setter
    def channel_name(self, value):
        """
        Set a custom channel name for this publisher instance.

        Use this to override the default class-name-based channel, for example
        when routing messages to environment-specific channels or when multiple
        instances should publish to different topics.

        Args:
            value: The new channel name string.
        """
        self._channel_name = value

    def publish(
        self,
        data: dict | None = None,
        channel_name: str | None = None,
        pipeline: redis.client.Pipeline | None = None,
    ):
        """Publish *data* as msgpack to the given (or default) channel.

        Serializes the provided data using msgpack (with numpy array support)
        and publishes it to the specified Redis channel. Supports both immediate
        publishing and deferred publishing via Redis pipelines.

        Pipeline Support:
            When a pipeline is provided, the publish command is queued within
            that pipeline rather than executed immediately. This enables atomic
            operations where a model save and its corresponding notification
            either both succeed or both fail together. This is critical for
            maintaining consistency between persisted state and published events.

        Args:
            data: Dict payload to publish. Falls back to ``_publish_data``.
            channel_name: Override the default channel for this call.
            pipeline: Optional Redis pipeline for batching.

        Returns:
            The number of subscribers that received the message, or the
            pipeline when batching. Returns None if no data to publish.

        Raises:
            PublisherException: If no channel name is available (neither
                provided nor set on the instance).

        Example:
            Immediate publish::

                publisher.publish({"event": "updated", "value": 42})

            Atomic save-and-publish with pipeline::

                pipeline = redis_conn.pipeline()
                model.save(pipeline=pipeline)
                model.publish({"saved": True}, pipeline=pipeline)
                pipeline.execute()  # Both operations succeed or fail together
        """
        # numpy support when msgpack-numpy is installed (the `dataframe`
        # extra); plain msgpack otherwise, which encodes everything but
        # arrays to the same bytes (#759 M5: publishing used to raise
        # ModuleNotFoundError without the extra).
        _patch_msgpack_numpy()
        # logger.debug(f"publish to {channel_name}: {publish_data}")
        channel_name = channel_name or self._channel_name
        self._publish_data = data or self._publish_data
        if self._publish_data is None or self._publish_data == {}:
            return None
        elif not channel_name:
            raise PublisherException("missing channel to publish to")

        backend = _native_backend(self, pipeline)
        if backend is not None:
            # #759 M5: NOTIFY on the backend -- inside its unit of work when
            # one is passed, or inside a popoto.batch()'s transaction (which
            # it joins, so the message is delivered at execute()'s COMMIT and
            # never after reset()). A pipeline publish names the instance's
            # channel, as the Redis pipeline branch below does.
            from ..batch import unit_of

            uow = unit_of(pipeline, backend)
            target = self._channel_name if pipeline else channel_name
            subscriber_count = backend.publish(
                target, msgpack.packb(self._publish_data), uow=uow
            )
            if pipeline:
                return pipeline
            logger.debug(
                f"published data to `{channel_name}`, {subscriber_count} subscribers"
            )
            return subscriber_count
        if pipeline:
            return pipeline.publish(
                self._channel_name, msgpack.packb(self._publish_data)
            )
        else:
            subscriber_count = get_REDIS_DB().publish(
                channel_name, msgpack.packb(self._publish_data)
            )
            logger.debug(
                f"published data to `{channel_name}`, {subscriber_count} subscribers"
            )
            return subscriber_count


def _native_backend(obj: object, pipeline: object = None) -> "typing.Any":
    """The non-Redis backend ``obj`` publishes or subscribes on, or ``None``
    for Redis (#759 M5): a plain Redis pipeline means Redis; otherwise
    ``obj``'s model backend, its ``backend=`` choice, or the process default.
    A ``popoto.batch()`` is not a plain pipeline: it carries whichever
    backend ``obj`` publishes on (a Postgres publish joins its transaction)."""
    from ..batch import Batch

    if isinstance(pipeline, redis.client.Pipeline) and not isinstance(pipeline, Batch):
        return None
    from ..backends import UnitOfWork, _instance, get_backend

    if isinstance(pipeline, UnitOfWork) and pipeline.backend != "redis":
        return _instance(pipeline.backend)

    if hasattr(type(obj), "_meta") and hasattr(obj, "save"):
        backend = get_backend(type(obj))
    else:
        choice = getattr(obj, "_pubsub_backend", None)
        if isinstance(choice, str):
            backend = _instance(choice)
        elif choice is not None:
            backend = choice
        else:
            backend = get_backend()
    return None if getattr(backend, "name", "redis") == "redis" else backend


def _patch_msgpack_numpy() -> None:
    try:
        import msgpack_numpy
    except ImportError:
        return
    msgpack_numpy.patch()
