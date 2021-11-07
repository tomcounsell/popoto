import msgpack
from ..fields.geo_field import GeoField

from ..redis_db import ENCODING


def encode_custom_types(obj):
    if isinstance(obj, GeoField.Coordinates):
        return {'__Coordinates__': True, 'latitude': obj.latitude, 'longitude': obj.longitude}
    return obj


def decode_custom_types(obj):
    if '__Coordinates__' in obj:
        return GeoField.Coordinates(obj.latitude, obj.longitude)
    return obj


def encode_popoto_model_obj(obj: 'Model') -> dict:
    import msgpack_numpy as m
    m.patch()

    encoded_hashmap = dict()
    for field_name, field in obj._meta.fields.items():
        value = getattr(obj, field_name)

        if field.type in [GeoField.Coordinates,]:
            encoded_value = msgpack.packb(value, default=encode_custom_types)
        else:
            encoded_value = msgpack.packb(value)

        encoded_hashmap[str(field_name).encode(ENCODING)] = encoded_value

    return encoded_hashmap


def decode_popoto_model_hashmap(model_class: 'Model', redis_hash: dict) -> 'Model':
    return model_class(**{
        key_b.decode("utf-8"): msgpack.unpackb(value_b, object_hook=decode_custom_types, raw=False)
        for key_b, value_b in redis_hash.items()
    })
