from abc import ABC
import logging
from ..exceptions import ModelException
from ..redis_db import POPOTO_REDIS_DB
import msgpack

import msgpack_numpy as m
m.patch()
logger = logging.getLogger(__name__)


class SubscriberException(ModelException):
    pass


class SubscriberModel(ABC):
    class_describer = "subscriber"
    classes_subscribing_to = [
        # ...
    ]

    def __init__(self):
        self.pubsub = POPOTO_REDIS_DB.pubsub()
        logger.info(f'New pubsub for {self.__class__.__name__}')
        for s_class in self.classes_subscribing_to:
            self.pubsub.subscribe(s_class if isinstance(s_class, str) else s_class.__name__)
            logger.info(f'{self.__class__.__name__} subscribed to '
                        f'{s_class if isinstance(s_class, str) else s_class.__name__} channel')

    def __call__(self):
        data_event = self.pubsub.get_message()
        if not data_event:
            return
        if not data_event.get('type') == 'message':
            return

        # logger.debug(f'got message: {data_event}')

        try:
            channel_name = data_event.get('channel').decode("utf-8")
            event_data = msgpack.loads(data_event.get('data'))
            logger.debug(f'handling event in {self.__class__.__name__}')
            self.pre_handle(channel_name, event_data)
            self.handle(channel_name, event_data)
        except KeyError as e:
            logger.warning(f'unexpected format: {data_event} ' + str(e))
            pass  # message not in expected format, just ignore
        except msgpack.exceptions.FormatError:
            logger.warning(f'unexpected data format: {data_event["data"]}')
            pass  # message not in expected format, just ignore
        except Exception as e:
            raise SubscriberException(f'Error calling {self.__class__.__name__}: ' + str(e))

    def pre_handle(self, channel, data, *args, **kwargs):
        pass

    def handle(self, channel, data, *args, **kwargs):
        """
        overwrite me with some logic
        :return: None
        """
        logger.warning(f'NEW MESSAGE for '
                       f'{self.__class__.__name__} subscribed to '
                       f'{channel} channel '
                       f'BUT HANDLER NOT DEFINED! '
                       f'... message/event discarded')
        pass
