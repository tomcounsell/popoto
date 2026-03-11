"""
Redis Pub/Sub subscriber base class for reactive event handling.

This module provides the abstract Subscriber class, which enables a reactive,
event-driven architecture within Popoto. While Popoto Models handle data persistence,
Subscribers handle real-time data flow--allowing components to react to changes
as they happen rather than polling for updates.

Design Philosophy:
    The Subscriber implements the Observer pattern over Redis pub/sub channels.
    This decouples publishers (who emit events) from subscribers (who react to them),
    enabling loose coupling between system components. A price update, for example,
    can trigger multiple independent calculations (moving averages, RSI, etc.)
    without the price publisher needing to know about any of them.

    The callable interface (via __call__) is intentionally designed for polling loops.
    Rather than blocking on a message, each call checks for a message and returns
    immediately if none is available. This allows subscribers to be integrated into
    event loops, async frameworks, or simple while-true loops with sleep intervals.

Key Design Decisions:
    - Non-blocking by default: get_message() returns immediately, giving the caller
      control over timing and concurrency.
    - Two-phase handling: pre_handle() runs before handle(), allowing subclasses to
      extract and validate data before the main logic executes. This separation keeps
      parsing concerns separate from business logic.
    - Graceful error handling: Malformed messages are logged and discarded rather than
      crashing the subscriber. Only unexpected errors raise SubscriberException.
    - msgpack serialization: Uses msgpack (with numpy support) for efficient binary
      serialization, matching the Publisher's format.

Integration with Finance Module:
    The finance module extends this pattern with specialized subscribers like
    IndicatorSubscriber that automatically compute technical indicators when
    new price data arrives. This creates a reactive pipeline: price updates
    flow through channels, triggering cascading indicator calculations.

Usage Example:
    class PriceAlertSubscriber(Subscriber):
        sub_channel_names = ['PriceStorage']

        def handle(self, channel, data):
            if data['price'] > self.threshold:
                send_alert(f"Price crossed {self.threshold}")

    # In your event loop:
    subscriber = PriceAlertSubscriber()
    while True:
        subscriber()  # Check for and process one message
        time.sleep(0.1)

See Also:
    - Publisher: The counterpart that emits messages to channels
    - popoto.finance.subscribers: Concrete implementations for financial data
"""

from abc import ABC
import logging
from ..redis_db import POPOTO_REDIS_DB, ENCODING
import msgpack

logger = logging.getLogger("POPOTO-subscriber")


class SubscriberException(Exception):
    """Raised when a subscriber's message handler fails.

    This exception wraps errors that occur within handle() or pre_handle() methods,
    providing context about which subscriber class failed. It is intentionally NOT
    raised for malformed messages (which are silently discarded) but only for
    unexpected errors that indicate a bug in the subscriber implementation.

    The exception message includes the subscriber class name to aid debugging in
    systems with many subscriber types running concurrently.
    """

    pass


class Subscriber(ABC):
    """Abstract base class for consuming messages from Redis pub/sub channels.

    Set ``sub_channel_names`` to the list of channels to subscribe to.
    Override :meth:`handle` to process incoming messages. Call the instance
    (``subscriber()``) in a loop to poll for new messages.

    Subscriber provides a framework for building reactive components that respond
    to messages published on Redis channels. Subclasses define which channels to
    listen to and implement handle() to process incoming messages.

    The class follows a pull-based model: you call the subscriber instance as a
    callable to check for and process one message at a time. This gives you full
    control over when and how often to check for messages, making it easy to
    integrate with various concurrency models (threads, asyncio, simple loops).

    Attributes:
        sub_channel_names: List of Redis channel names to subscribe to. Define this
            as a class attribute in your subclass. Multiple channels allow a single
            subscriber to react to events from different sources.

    Design Notes:
        - Each Subscriber instance creates its own Redis pubsub connection. This
          isolation prevents subscribers from interfering with each other.
        - The ABC inheritance signals that this class should not be instantiated
          directly; subclasses must implement handle().
        - Channel subscriptions happen at construction time, so the subscriber
          is immediately ready to receive messages after __init__ completes.

    Example:
        class OrderSubscriber(Subscriber):
            sub_channel_names = ['new_orders', 'order_updates']

            def handle(self, channel, data):
                if channel == 'new_orders':
                    process_new_order(data)
                else:
                    update_order(data)
    """

    sub_channel_names: list = []

    def __init__(self, *args, **kwargs):
        """
        Initialize the subscriber and subscribe to all configured channels.

        Creates a dedicated Redis pubsub connection for this subscriber instance
        and subscribes to each channel listed in sub_channel_names. The subscriber
        is ready to receive messages immediately after construction.

        Args:
            *args: Passed to parent classes (for cooperative multiple inheritance).
            **kwargs: Passed to parent classes.

        Note:
            Each subscriber gets its own pubsub connection. In high-throughput
            scenarios with many subscribers, consider whether the connection
            overhead is acceptable or if a shared subscription model would be
            more appropriate.
        """
        self.pubsub = POPOTO_REDIS_DB.pubsub()
        logger.info(f"New pubsub for {self.__class__.__name__}")
        for channel_name in self.sub_channel_names:
            self.pubsub.subscribe(channel_name)
            logger.info(
                f"{self.__class__.__name__} subscribed to {channel_name} channel"
            )

    def __call__(self):
        """Poll for the next message and dispatch to :meth:`handle`.

        This method implements the core message processing loop. It is designed
        to be called repeatedly (e.g., in a while loop or event scheduler) to
        process messages as they arrive. The non-blocking design returns
        immediately if no message is available, giving callers control over
        timing and concurrency.

        The processing pipeline is:
            1. Check for pending message (returns immediately if none)
            2. Filter out non-message events (subscriptions, pongs, etc.)
            3. Decode channel name and deserialize message data via msgpack
            4. Call pre_handle() for preprocessing/validation
            5. Call handle() for main business logic

        Returns:
            None. Message processing happens via side effects in handle().
            Returns early (no return value) if no message is available.

        Raises:
            SubscriberException: If handle() or pre_handle() raises an unexpected
                error. Malformed messages (KeyError, msgpack.FormatError) are
                logged and silently discarded to maintain subscriber stability.

        Note:
            msgpack_numpy is patched on each call to ensure numpy array support.
            This is safe to call repeatedly and ensures compatibility even if
            the subscriber is used in environments where msgpack_numpy might
            not be globally patched.
        """
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
            logger.warning(f"unexpected data format: {data_event['data']}")
            pass  # message not in expected format, just ignore
        except Exception as e:
            raise SubscriberException(
                f"Error calling {self.__class__.__name__}: " + str(e)
            )

    def pre_handle(self, channel, data, *args, **kwargs):
        """Hook called before :meth:`handle`. Override for logging, filtering, etc.

        Override this method to perform data extraction, validation, or
        transformation before the main handle() logic runs. This separation
        allows subclasses to standardize data parsing (e.g., extracting
        ticker symbols, timestamps) independently of business logic.

        Args:
            channel: The channel name the message was received on (decoded string).
            data: The deserialized message payload (typically a dict).
            *args: Reserved for future use.
            **kwargs: Reserved for future use.

        Note:
            Errors raised here will propagate as SubscriberException, stopping
            message processing. For validation that should skip invalid messages
            without crashing, catch exceptions and return early from handle().
        """
        pass

    def handle(self, channel, data, *args, **kwargs):
        """Process an incoming message. Override this in your subclass.

        This is the main extension point for Subscriber subclasses. When a
        message arrives on any subscribed channel, this method is called with
        the decoded channel name and deserialized data payload. Implement your
        business logic here.

        The default implementation logs a warning and discards the message,
        serving as a reminder to implement this method in subclasses.

        Args:
            channel: The channel name the message arrived on.
            data: The deserialized (msgpack-unpacked) message payload.
            *args: Reserved for future use.
            **kwargs: Reserved for future use.

        Returns:
            None. Results should be achieved through side effects (saving to
            database, sending alerts, updating state, etc.).

        Example:
            def handle(self, channel, data):
                price = Price(
                    ticker=data['ticker'],
                    value=data['price'],
                    timestamp=data['timestamp']
                )
                price.save()
        """
        logger.warning(
            f"NEW MESSAGE for "
            f"{self.__class__.__name__} subscribed to "
            f"{channel} channel "
            f"BUT HANDLER NOT DEFINED! "
            f"... message/event discarded"
        )
        pass
