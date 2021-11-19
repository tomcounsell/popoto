import msgpack
import datetime
from ..fields.geo_field import GeoField

from ..redis_db import ENCODING


def encode_custom_types(obj):
    if isinstance(obj, GeoField.Coordinates):
        return {'__Coordinates__': True, 'latitude': obj.latitude, 'longitude': obj.longitude}
    if isinstance(obj, datetime.datetime):
        return {'__datetime__': True, 'as_str': obj.strftime("%Y%m%dT%H:%M:%S.%f")}
    if isinstance(obj, datetime.date):
        return {'__date__': True, 'as_str': obj.strftime("%Y%m%d")}
    if isinstance(obj, datetime.time):
        return {'__time__': True, 'as_str': obj.strftime("%H:%M:%S.%f")}
    return obj


def decode_custom_types(obj):
    if isinstance(obj, dict) and 'as_str' in obj:
        if '__Coordinates__' in obj:
            return GeoField.Coordinates(obj.latitude, obj.longitude)
        if '__datetime__' in obj:
            return datetime.datetime.strptime(obj['as_str'], "%Y%m%dT%H:%M:%S.%f")
        if '__date__' in obj:
            return datetime.datetime.strptime(obj['as_str'], "%Y%m%d").date()
        if '__time__' in obj:
            return datetime.datetime.strptime(obj['as_str'], "%H:%M:%S.%f").time()
    return obj


def encode_popoto_model_obj(obj: 'Model') -> dict:
    import msgpack_numpy as m
    m.patch()

    encoded_hashmap = dict()
    for field_name, field in obj._meta.fields.items():
        value = getattr(obj, field_name)

        if field.type in [GeoField.Coordinates, datetime.datetime, datetime.date, datetime.time]:
            encoded_value = msgpack.packb(value, default=encode_custom_types)
        else:
            encoded_value = msgpack.packb(value)

        encoded_hashmap[str(field_name).encode(ENCODING)] = encoded_value

    return encoded_hashmap


def decode_popoto_model_hashmap(model_class: 'Model', redis_hash: dict) -> 'Model':
    return model_class(**{
        key_b.decode("utf-8"): decode_custom_types(msgpack.unpackb(value_b))
        for key_b, value_b in redis_hash.items()
    })
