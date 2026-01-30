# PubSub Features

The PubSub feature in Popoto provides publish-subscribe pattern messaging using Redis. This is useful in distributed systems where components need to communicate without knowing each other's identity.

Data is serialized with [msgpack](https://msgpack.org/) (with numpy support), so you can publish dicts containing numbers, strings, lists, and numpy arrays.

## Publisher

The `Publisher` class publishes messages to Redis channels. Subclass `Publisher` and call `publish()` to send data.

```python
from popoto.pubsub.publisher import Publisher

class PricePublisher(Publisher):
    pass

publisher = PricePublisher(channel_name="prices")
publisher.publish(data={"symbol": "BTC", "price": 45000.0})
```

The default channel name is the class name. You can override it at init or publish time:

```python
# Channel name defaults to class name
publisher = PricePublisher()  # channel_name = "PricePublisher"

# Override at init
publisher = PricePublisher(channel_name="live_prices")

# Override at publish time
publisher.publish(data={"symbol": "ETH", "price": 3000.0}, channel_name="alt_prices")
```

`publish()` returns the number of subscribers that received the message:

```python
subscriber_count = publisher.publish(data={"event": "update"})
print(f"{subscriber_count} subscribers received the message")
```

### Pipeline Support

Use a Redis pipeline for batch publishing:

```python
from popoto.redis_db import POPOTO_REDIS_DB

pipeline = POPOTO_REDIS_DB.pipeline()
publisher.publish(data={"price": 100}, pipeline=pipeline)
publisher.publish(data={"price": 101}, pipeline=pipeline)
pipeline.execute()  # Both messages sent in one round-trip
```

## Subscriber

The `Subscriber` class listens for messages on one or more channels. Subclass it and override `handle()` to process incoming messages.

```python
from popoto.pubsub.subscriber import Subscriber

class PriceSubscriber(Subscriber):
    sub_channel_names = ['prices', 'alt_prices']

    def handle(self, channel, data, *args, **kwargs):
        print(f"Received on {channel}: {data}")
        # data is a dict, already deserialized from msgpack
```

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

### The pre_handle Hook

Override `pre_handle()` to run logic before the main handler (e.g., logging, validation):

```python
class PriceSubscriber(Subscriber):
    sub_channel_names = ['prices']

    def pre_handle(self, channel, data, *args, **kwargs):
        print(f"About to process message from {channel}")

    def handle(self, channel, data, *args, **kwargs):
        print(f"Price update: {data}")
```

### Multi-Channel Subscription

A single subscriber can listen to multiple channels. The `channel` argument in `handle()` tells you which channel the message came from:

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

## Exception Handling

The `Subscriber` handles exceptions at two levels:

- **Format errors**: Messages with unexpected formats (bad msgpack, missing fields) are silently ignored with a warning log
- **Handler errors**: Exceptions raised in `handle()` or `pre_handle()` are wrapped in a `SubscriberException`

```python
from popoto.pubsub.subscriber import Subscriber, SubscriberException

try:
    subscriber()
except SubscriberException as e:
    print(f"Error processing message: {e}")
```

## Logging

The PubSub system logs to standard Python loggers:

- `POPOTO-publisher`: Logs publish events and subscriber counts
- `POPOTO-subscriber`: Logs subscription setup, message handling, and warnings

```python
import logging
logging.getLogger("POPOTO-publisher").setLevel(logging.DEBUG)
logging.getLogger("POPOTO-subscriber").setLevel(logging.DEBUG)
```
