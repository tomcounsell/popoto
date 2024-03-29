import os
import sys
from datetime import datetime, timedelta
from src import popoto
from src.popoto.redis_db import POPOTO_REDIS_DB

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))


class Person(popoto.Model):
    uuid = popoto.AutoKeyField()
    username = popoto.UniqueKeyField()
    title = popoto.KeyField()
    level = popoto.SortedField(type=int)
    last_active = popoto.SortedField(type=datetime)
    location = popoto.GeoField()


lisa = Person(username="@LalisaManobal", title="Queen", last_active=datetime.now())
lisa.level = 99
lisa.location = (48.856373, 2.353016)
lisa.save()

same_lisa = Person.query.get(redis_key=lisa.db_key.redis_key)
assert same_lisa == lisa

paris_latitude, paris_longitude = (
    48.864716,
    2.349014,
)  # Hôtel de Ville, Fashion Week 2021
query_results = Person.query.filter(
    title__startswith="Queen",
    level__lt=100,
    last_active__gt=(datetime.now() - timedelta(days=1)),
    location_latitude=paris_latitude,
    location_longitude=paris_longitude,
    location_radius=5,
    location_radius_unit="km",
)

p = Person()

pipeline = POPOTO_REDIS_DB.pipeline()
for p in Person.query.all():
    # pipeline = p.save(pipeline=pipeline)
    pipeline = p.delete(pipeline=pipeline)
pipeline.execute()
