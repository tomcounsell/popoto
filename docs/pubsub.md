# PubSub Features

Popoto provides publish-subscribe pattern messaging through Redis channels. The pub/sub pattern allows components in a distributed system to communicate asynchronously without tight coupling — publishers send messages to named channels, and subscribers receive them without knowing who published them.

Use Redis pub/sub when you need real-time event broadcasting across multiple services or processes. Common scenarios include price feeds, notification systems, live dashboards, and distributed task coordination. Unlike queuing systems, pub/sub delivers messages to all active subscribers simultaneously, making it ideal for fan-out messaging patterns.

Data is serialized with [msgpack](https://msgpack.org/) (with numpy support), so you can publish dicts containing numbers, strings, lists, and numpy arrays.

## Publisher

The `Publisher` class publishes messages to Redis channels. Subclass `Publisher` and call `publish()` to send data.

```python
from popoto.pubsub import Publisher

class PricePublisher(Publisher):
    pass

publisher = PricePublisher(channel_name="prices")
publisher.publish(data={"symbol": "BTC", "price": 45000.0})
```

The default channel name is the class name. You can override it at initialization or when calling `publish()`.

```python
# Channel name defaults to class name
publisher = PricePublisher()  # channel_name = "PricePublisher"

# Override at init
publisher = PricePublisher(channel_name="live_prices")

# Override at publish time
publisher.publish(data={"symbol": "ETH", "price": 3000.0}, channel_name="alt_prices")
```

The `publish()` method returns the number of active subscribers that received the message.

```python
subscriber_count = publisher.publish(data={"event": "update"})
print(subscriber_count)
# => 3
```

### Pipeline Support

Use a Redis pipeline to batch multiple publish operations into a single round-trip to Redis.

```python
from popoto.redis_db import POPOTO_REDIS_DB

pipeline = POPOTO_REDIS_DB.pipeline()
publisher.publish(data={"price": 100}, pipeline=pipeline)
publisher.publish(data={"price": 101}, pipeline=pipeline)
pipeline.execute()  # Both messages sent in one round-trip
```

!!! tip
    Pipeline batching significantly improves performance when publishing many messages in quick succession. Instead of one network round-trip per message, you pay the latency cost once for the entire batch.

## Subscriber

The `Subscriber` class listens for messages on one or more channels. Subclass it and override `handle()` to process incoming messages.

```python
from popoto.pubsub import Subscriber

class PriceSubscriber(Subscriber):
    sub_channel_names = ['prices', 'alt_prices']

    def handle(self, channel, data, *args, **kwargs):
        print(f"Received on {channel}: {data}")
        # data is a dict, already deserialized from msgpack
```

The `data` argument is automatically deserialized from msgpack, so you receive the original Python dict that was published.

### Subscriber Lifecycle

1. **Initialize**: Creates a Redis pubsub connection and subscribes to all channels in `sub_channel_names`
2. **Poll**: Call the instance to check for the next message (non-blocking)
3. **Handle**: If a message is available, `pre_handle()` then `handle()` are called

```python
subscriber = PriceSubscriber()

# Poll for messages in a loop
import time
while True:
    subscriber()  # Check for next message, calls handle() if available
    time.sleep(0.01)  # Small delay to avoid busy-waiting
```

Each call to the subscriber instance polls Redis once. If a message is waiting, it triggers the handler methods.

### The pre_handle Hook

Override `pre_handle()` to run logic before the main handler. This is useful for logging, validation, or metrics collection that should happen for every message.

```python
class PriceSubscriber(Subscriber):
    sub_channel_names = ['prices']

    def pre_handle(self, channel, data, *args, **kwargs):
        print(f"About to process message from {channel}")

    def handle(self, channel, data, *args, **kwargs):
        print(f"Price update: {data}")
```

Both `pre_handle()` and `handle()` receive the same arguments, allowing you to filter or transform data in the pre-handler before main processing.

### Multi-Channel Subscription

A single subscriber can listen to multiple channels. The `channel` argument in `handle()` tells you which channel the message came from, allowing you to route messages to different processing logic.

```python
class MultiSubscriber(Subscriber):
    sub_channel_names = ['prices', 'alerts', 'orders']

    def handle(self, channel, data, *args, **kwargs):
        if channel == "prices":
            self.process_price(data)
        elif channel == "alerts":
            self.process_alert(data)
        elif channel == "orders":
            self.process_order(data)
```

This pattern is more efficient than running separate subscriber instances when you need coordinated handling across multiple channels.

## Exception Handling

!!! note
    The `Subscriber` handles exceptions at two levels:

    - **Format errors**: Messages with unexpected formats (bad msgpack, missing fields) are silently ignored with a warning log
    - **Handler errors**: Exceptions raised in `handle()` or `pre_handle()` are wrapped in a `SubscriberException`

Catch `SubscriberException` to handle errors in your message processing logic.

```python
from popoto.pubsub import Subscriber, SubscriberException

try:
    subscriber()
except SubscriberException as e:
    print(f"Error processing message: {e}")
```

This allows you to implement retry logic, dead letter queues, or custom error handling around your subscriber polling loop.

## Logging

!!! note
    The PubSub system logs to standard Python loggers:

    - `POPOTO-publisher`: Logs publish events and subscriber counts
    - `POPOTO-subscriber`: Logs subscription setup, message handling, and warnings

Configure these loggers to control visibility of pub/sub operations.

```python
import logging
logging.getLogger("POPOTO-publisher").setLevel(logging.DEBUG)
logging.getLogger("POPOTO-subscriber").setLevel(logging.DEBUG)
```

Enable DEBUG level to see every message published and received, useful during development and troubleshooting.
