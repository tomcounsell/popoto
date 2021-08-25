from abc import ABC
import logging
from ..exceptions import ModelException
from ..redis_db import POPOTO_REDIS_DB
import msgpack

import msgpack_numpy as m
m.patch()
logger = logging.getLogger(__name__)


class PublisherException(ModelException):
    pass


class PublisherModel(ABC):
    def __init__(self, *args, **kwargs):
        self.publish_data = None
        self.channel_name = kwargs.get('channel_name', self.__class__.__name__)

    def publish(self, data=None, pipeline=None, *args, **kwargs):
        # logger.debug(f"publish to {self.channel_name}: {publish_data}")
        self.publish_data = data or self.publish_data
        if not self.publish_data:
            return
        if not self.channel_name:
            raise PublisherException("missing channel to publish to")

        if pipeline:
            return pipeline.publish(self.__class__.__name__, json.dumps(self.get_z_add_data()))
        else:
            subscriber_count = POPOTO_REDIS_DB.publish(self.channel_name, msgpack.dumps(publish_data))
            logger.debug(f"published data to `{self.channel_name}`, {subscriber_count} subscribers")
