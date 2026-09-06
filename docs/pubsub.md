# PubSub

Popoto provides real-time messaging through Redis pub/sub channels. In a food delivery app, order status
changes need to reach drivers, customers, and restaurant dashboards the moment they happen. The pub/sub
pattern lets you broadcast these updates without tight coupling between services -- publishers send messages
to named channels, and all active subscribers receive them simultaneously.

Data is serialized with [msgpack](https://msgpack.org/) (with numpy support), so you can publish dicts
containing numbers, strings, lists, and numpy arrays.

## Publisher

The `Publisher` class sends messages to Redis channels. Subclass it and call `publish()` to broadcast
order status changes across your system.

```python
from popoto.pubsub import Publisher

class OrderStatusPublisher(Publisher):
    pass

publisher = OrderStatusPublisher(channel_name="order_updates")
publisher.publish(data={
    "order_id": "abc123",
    "status": "picked_up",
    "driver": "Maria",
})
```

The `publish()` method returns the number of active subscribers that received the message.

```python
subscriber_count = publisher.publish(data={
    "order_id": "abc123",
    "status": "delivered",
})
print(subscriber_count)
# => 3
```

## Custom Channel Names

By default, the channel name is the class name. You can override it at initialization with the
`channel_name` parameter, or on a per-call basis when publishing.

```python
# Defaults to class name
publisher = OrderStatusPublisher()
print(publisher.channel_name)
# => "OrderStatusPublisher"

# Override at init
publisher = OrderStatusPublisher(channel_name="order_updates")
print(publisher.channel_name)
# => "order_updates"

# Override per call
publisher.publish(
    data={"order_id": "abc123", "status": "cancelled"},
    channel_name="order_cancellations",
)
```

!!! note
    The per-call `channel_name` override only applies to that single publish. Subsequent calls
    without a `channel_name` argument revert to the instance default.

## Pipeline Support

Use a Redis pipeline to batch multiple order updates into a single round-trip to Redis. This is
useful when processing a batch of orders at once, such as marking all orders from a closing
restaurant as delayed.

```python
import popoto

publisher = OrderStatusPublisher(channel_name="order_updates")
pipeline = popoto.batch()

publisher.publish(data={"order_id": "order_1", "status": "delayed"}, pipeline=pipeline)
publisher.publish(data={"order_id": "order_2", "status": "delayed"}, pipeline=pipeline)
publisher.publish(data={"order_id": "order_3", "status": "delayed"}, pipeline=pipeline)

pipeline.execute()  # All three messages sent in one round-trip
```

!!! tip
    Pipeline batching significantly improves throughput when publishing many messages in quick
    succession. Instead of one network round-trip per message, you pay the latency cost once
    for the entire batch.

`popoto.batch()` returns the shared connection's pipeline, so publishing does not need to
import the Redis client. A pipeline built directly off the client
(`popoto.get_redis().pipeline()`) is the same object and still works. See
[Batching Commands into One Transaction](query.md#batching-commands-into-one-transaction).

## Subscriber

The `Subscriber` class listens for messages on one or more channels. Subclass it and override
`handle()` to process incoming order updates.

```python
from popoto.pubsub import Subscriber

class OrderStatusSubscriber(Subscriber):
    sub_channel_names = ["order_updates"]

    def handle(self, channel, data, *args, **kwargs):
        order_id = data.get("order_id")
        status = data.get("status")
        print(f"Order {order_id} is now: {status}")
```

The `data` argument is automatically deserialized from msgpack, so you receive the original
Python dict that was published. See [Publisher](#publisher) for how messages are sent.

## Subscriber Lifecycle

A subscriber follows three steps: initialize, poll, and handle.

1. **Init** -- Creates a Redis pubsub connection and subscribes to all channels in
   `sub_channel_names`
2. **Poll** -- Call the instance to check for the next message (non-blocking)
3. **Handle** -- If a message is available, `pre_handle()` then `handle()` are called

```python
subscriber = OrderStatusSubscriber()

# Poll for messages in a loop
import time
while True:
    subscriber()  # Checks for next message, calls handle() if available
    time.sleep(0.01)  # Small delay to avoid busy-waiting
```

Each call to the subscriber instance polls Redis once. If no message is waiting, the call
returns immediately without invoking the handler.

!!! warning
    The poll loop is blocking in the sense that your code must actively call the subscriber.
    If your application needs to do other work between polls, keep the `sleep` interval short
    or integrate the poll into your event loop.

## pre_handle Hook

Override `pre_handle()` to run logic before the main handler. This is useful for logging
incoming events, filtering out irrelevant updates, or collecting metrics.

```python
class OrderStatusSubscriber(Subscriber):
    sub_channel_names = ["order_updates"]

    def pre_handle(self, channel, data, *args, **kwargs):
        print(f"[{channel}] Incoming: order {data.get('order_id')}")

    def handle(self, channel, data, *args, **kwargs):
        if data.get("status") == "delivered":
            print(f"Order {data['order_id']} delivered successfully")
```

Both `pre_handle()` and `handle()` receive the same `channel` and `data` arguments. If you
need to filter messages, raise an exception in `pre_handle()` to prevent `handle()` from
running (though the exception will be wrapped in a `SubscriberException`).

## Multi-Channel Subscription

A single subscriber can listen to multiple channels. The `channel` argument in `handle()` tells
you which channel the message arrived on, so you can route to different processing logic.

```python
class DeliverySubscriber(Subscriber):
    sub_channel_names = ["order_updates", "driver_location", "restaurant_alerts"]

    def handle(self, channel, data, *args, **kwargs):
        if channel == "order_updates":
            self.process_order_update(data)
        elif channel == "driver_location":
            self.update_driver_position(data)
        elif channel == "restaurant_alerts":
            self.handle_restaurant_alert(data)

    def process_order_update(self, data):
        print(f"Order {data['order_id']}: {data['status']}")

    def update_driver_position(self, data):
        print(f"Driver {data['driver']}: lat={data['lat']}, lon={data['lon']}")

    def handle_restaurant_alert(self, data):
        print(f"Alert from {data['restaurant']}: {data['message']}")
```

!!! tip
    Using one subscriber for related channels is more efficient than running separate instances
    when you need coordinated handling, such as matching a driver location update with an
    active order.

## Exception Handling

The subscriber handles exceptions at two levels. Format errors from malformed msgpack data or
missing fields are logged as warnings and silently discarded. Exceptions raised in your
`handle()` or `pre_handle()` methods are wrapped in a `SubscriberException`.

```python
from popoto.pubsub import Subscriber, SubscriberException

subscriber = OrderStatusSubscriber()

while True:
    try:
        subscriber()
    except SubscriberException as e:
        print(f"Error processing order update: {e}")
        # Log the error, notify ops, or add to a dead letter queue
    time.sleep(0.01)
```

!!! warning
    An unhandled `SubscriberException` will break your poll loop. Always wrap `subscriber()`
    calls in a try/except block in production to keep the listener running.

## Logging

The pub/sub system uses two standard Python loggers that you can configure independently.

- `POPOTO-publisher` -- Logs publish events and subscriber counts at DEBUG level
- `POPOTO-subscriber` -- Logs subscription setup, message handling, and format warnings

```python
import logging
logging.getLogger("POPOTO-publisher").setLevel(logging.DEBUG)
logging.getLogger("POPOTO-subscriber").setLevel(logging.DEBUG)
```

Enable DEBUG level during development to see every message published and received. In
production, INFO level shows subscription setup without the per-message noise.
