from abc import ABC
import logging
from ..redis_db import POPOTO_REDIS_DB
import msgpack

import msgpack_numpy as m
m.patch()
logger = logging.getLogger('POPOTO-publisher')


class PublisherException(Exception):
    pass


class Publisher(ABC):
    channel_name: str = ""
    publish_data: dict = {}

    def __init__(self, channel_name="", *args, **kwargs):
        self.channel_name = kwargs.get('channel_name', self.__class__.__name__)

    def publish(self, data: dict = None, channel_name: str = None, pipeline=None):
        # logger.debug(f"publish to {channel_name}: {publish_data}")
        channel_name = channel_name or self.channel_name
        self.publish_data = data or self.publish_data
        if self.publish_data is None or self.publish_data is {}:
            return None
        elif not channel_name:
            raise PublisherException("missing channel to publish to")

        if pipeline:
            return pipeline.publish(self.channel_name, msgpack.dumps(self.publish_data))
        else:
            subscriber_count = POPOTO_REDIS_DB.publish(channel_name, msgpack.dumps(self.publish_data))
            logger.debug(f"published data to `{channel_name}`, {subscriber_count} subscribers")
            return subscriber_count
