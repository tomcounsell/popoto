import sys
import os
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

time_checkpoints = {'started': time.time()}

from src.popoto.redis_db import POPOTO_REDIS_DB
from src import popoto

time_checkpoints['popoto imported'] = time.time()

class KeyValueModel(popoto.Model):
    key = popoto.KeyField()
    value = popoto.Field()

time_checkpoints['models defined'] = time.time()

kv_objects = [
    KeyValueModel(
        key=f"key{i}",
        value=f"value{i}"
    )
    for i in range(1000)
]

time_checkpoints['1000 objects generated'] = time.time()

for kv_object in kv_objects:
    kv_object.save()

time_checkpoints['1000 objects saved'] = time.time()

kv_objects = KeyValueModel.objects.all()

time_checkpoints['1000 objects queried'] = time.time()

for kv_object in kv_objects:
    kv_object.value += "modified"
    kv_object.save()

time_checkpoints['1000 objects modified'] = time.time()

for kv_object in KeyValueModel.objects.all():
    kv_object.delete()

time_checkpoints['1000 objects deleted'] = time.time()

######################################
###############  DONE  ###############
######################################

for checkpoint, timestamp in time_checkpoints.items():
    if checkpoint == 'started':
        print(f"{'%8.1f' % 0.0} starting performance test")
    else:
        print(f"{'%8.1f' % ((timestamp - last_timestamp)*1000)} ms for {checkpoint}")
    last_timestamp = timestamp
