from abc import ABC
import logging
from ..redis_db import POPOTO_REDIS_DB, ENCODING
import msgpack

logger = logging.getLogger("POPOTO-subscriber")


class SubscriberException(Exception):
    """Raised when a subscriber's message handler fails."""

    pass


class Subscriber(ABC):
    """Abstract base class for consuming messages from Redis pub/sub channels.

    Set ``sub_channel_names`` to the list of channels to subscribe to.
    Override :meth:`handle` to process incoming messages.  Call the instance
    (``subscriber()``) in a loop to poll for new messages.
    """

    sub_channel_names: list = []

    def __init__(self, *args, **kwargs):
        self.pubsub = POPOTO_REDIS_DB.pubsub()
        logger.info(f"New pubsub for {self.__class__.__name__}")
        for channel_name in self.sub_channel_names:
            self.pubsub.subscribe(channel_name)
            logger.info(
                f"{self.__class__.__name__} subscribed to {channel_name} channel"
            )

    def __call__(self):
        """Poll for the next message and dispatch to :meth:`handle`."""
        import msgpack_numpy as m

        m.patch()
        data_event = self.pubsub.get_message()
        if not data_event:
            return
        if not data_event.get("type") == "message":
            return

        # logger.debug(f"received message: {data_event}")

        try:
            channel_name = data_event.get("channel").decode(ENCODING)
            event_data = msgpack.unpackb(data_event.get("data"), strict_map_key=False)
            logger.debug(f"handling event in {self.__class__.__name__}")
            self.pre_handle(channel_name, event_data)
            self.handle(channel_name, event_data)
        except KeyError as e:
            logger.warning(f"unexpected format: {data_event} " + str(e))
            pass  # message not in expected format, just ignore
        except msgpack.exceptions.FormatError:
            logger.warning(f'unexpected data format: {data_event["data"]}')
            pass  # message not in expected format, just ignore
        except Exception as e:
            raise SubscriberException(
                f"Error calling {self.__class__.__name__}: " + str(e)
            )

    def pre_handle(self, channel, data, *args, **kwargs):
        """Hook called before :meth:`handle`. Override for logging, filtering, etc."""
        pass

    def handle(self, channel, data, *args, **kwargs):
        """Process an incoming message. Override this in your subclass.

        Args:
            channel: The channel name the message arrived on.
            data: The deserialized (msgpack-unpacked) message payload.
        """
        logger.warning(
            f"NEW MESSAGE for "
            f"{self.__class__.__name__} subscribed to "
            f"{channel} channel "
            f"BUT HANDLER NOT DEFINED! "
            f"... message/event discarded"
        )
        pass
