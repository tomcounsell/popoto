# PubSub Features

The PubSub feature in Popoto is a powerful tool that allows for publish-subscribe pattern messaging. This is particularly useful in a distributed system setup where components need to communicate with each other without knowing the other's identity.  

## Subscriber

The `Subscriber` class is an abstract base class that provides the basic structure for a subscriber in the PubSub system. It subscribes to one or more channels and handles incoming messages from these channels.  

Here is a basic usage of the `Subscriber` class:

``` python
from popoto.pubsub.subscriber import Subscriber

class MySubscriber(Subscriber):
    sub_channel_names = ['channel1', 'channel2']

    def handle(self, channel, data, *args, **kwargs):
        print(f"Received message from {channel}: {data}")

my_subscriber = MySubscriber()
my_subscriber()
```

In the above example, `MySubscriber` is a subclass of `Subscriber` that subscribes to 'channel1' and 'channel2'. It defines a `handle` method that prints out the channel name and the data received from the channel.  

## Publisher

The `Publisher` class is used to publish messages to channels. Any Subscriber instances subscribed to the channel will receive these messages.  

Here is a basic usage of the `Publisher` class:

``` python
from popoto.pubsub.publisher import Publisher

publisher = Publisher()
publisher.publish('channel1', 'Hello, world!')
```

In the above example, a Publisher instance is created and used to publish a message 'Hello, world!' to 'channel1'. Any Subscriber instances subscribed to 'channel1' will receive this message.  

## Exception Handling

The `Subscriber` class includes exception handling for unexpected message formats and other exceptions that may occur during message handling. If a message is not in the expected format, the message is ignored. If an exception occurs during message handling, a `SubscriberException` is raised.  

## Logging

The PubSub system includes logging for important events such as the creation of a new PubSub, a `Subscriber` subscribing to a channel, and the handling of an event. This can be useful for debugging and understanding the flow of messages in the system.  
