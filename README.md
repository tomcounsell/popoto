# Popoto - A Redis ORM

Object Types
 - document storage (like Mongo)
 - key, value (like base Redis)
 - timeseries (like TimescaleDB)
 - graph relationships (like Neo4j)

Popoto is ideal for streaming data. The pub/sub utilities allow you to trigger data state updates in real time.
Currently being used for:
 - trigger buy/sell actions from streaming price data
 - trigger user notifications from real time analytics
 - robots sending each other messages for teamwork
 - compressing sensor data and training neural networks

![](/static/popoto.png)

Popoto gets it's name from the [Māui dolphin](https://en.wikipedia.org/wiki/M%C4%81ui_dolphin) subspeciesis - the world's smallest dolphin subspecies.
Because dolphins are fast moving, agile, and work together in social groups. In the same way, Popoto wraps Redis and RedisGraph to make it easy to manage streaming timeseries data on a social graph.
