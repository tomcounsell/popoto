from abc import ABC
import logging

import redis

from ..redis_db import POPOTO_REDIS_DB
import msgpack

logger = logging.getLogger("POPOTO-publisher")


class PublisherException(Exception):
    """Raised when a publish operation fails (e.g. missing channel name)."""

    pass


class Publisher(ABC):
    """Abstract base class for publishing msgpack-encoded messages to Redis channels.

    Subclass and call :meth:`publish` to send data.  The channel name defaults
    to the class name but can be overridden via the constructor or per-call.
    """

    _channel_name: str = ""
    _publish_data: dict = {}

    def __init__(self, *args, **kwargs):
        self._channel_name = kwargs.get("channel_name", self.__class__.__name__)
        super().__init__(*args, **kwargs)

    @property
    def channel_name(self):
        return self._channel_name

    @channel_name.setter
    def channel_name(self, value):
        self._channel_name = value

    def publish(
        self,
        data: dict = None,
        channel_name: str = None,
        pipeline: redis.client.Pipeline = None,
    ):
        """Publish *data* as msgpack to the given (or default) channel.

        Args:
            data: Dict payload to publish. Falls back to ``_publish_data``.
            channel_name: Override the default channel for this call.
            pipeline: Optional Redis pipeline for batching.

        Returns:
            The number of subscribers that received the message, or the
            pipeline when batching.
        """
        import msgpack_numpy as m

        m.patch()
        # logger.debug(f"publish to {channel_name}: {publish_data}")
        channel_name = channel_name or self._channel_name
        self._publish_data = data or self._publish_data
        if self._publish_data is None or self._publish_data is {}:
            return None
        elif not channel_name:
            raise PublisherException("missing channel to publish to")

        if pipeline:
            return pipeline.publish(
                self._channel_name, msgpack.packb(self._publish_data)
            )
        else:
            subscriber_count = POPOTO_REDIS_DB.publish(
                channel_name, msgpack.packb(self._publish_data)
            )
            logger.debug(
                f"published data to `{channel_name}`, {subscriber_count} subscribers"
            )
            return subscriber_count
