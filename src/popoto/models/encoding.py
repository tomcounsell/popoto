from decimal import Decimal
import msgpack
import datetime
from collections.abc import Iterable

from ..exceptions import ModelException
from ..fields.geo_field import GeoField
from ..redis_db import ENCODING, POPOTO_REDIS_DB

from collections import namedtuple

EncoderDecoder = namedtuple("EncoderDecoder", "key, encoder, decoder")

TYPE_ENCODER_DECODERS = {
    Decimal: EncoderDecoder(
        key='__Decimal__',
        encoder=lambda obj: {'__Decimal__': True, 'as_str': str(obj)},
        decoder=lambda obj: Decimal(obj['as_str'])
    ),
    set: EncoderDecoder(
        key='__set__',
        encoder=lambda obj: {'__set__': True, 'as_str': str(list(obj))},
        decoder=lambda obj: Decimal(obj['as_str'])
    ),
    datetime.datetime: EncoderDecoder(
        key='__datetime__',
        encoder=lambda obj: {'__datetime__': True, 'as_str': obj.strftime("%Y%m%dT%H:%M:%S.%f")},
        decoder=lambda obj: datetime.datetime.strptime(obj['as_str'], "%Y%m%dT%H:%M:%S.%f")
    ),
    datetime.date: EncoderDecoder(
        key='__date__',
        encoder=lambda obj: {'__date__': True, 'as_str': obj.strftime("%Y%m%d")},
        decoder=lambda obj: datetime.datetime.strptime(obj['as_str'], "%Y%m%d").date()
    ),
    datetime.time: EncoderDecoder(
        key='__time__',
        encoder=lambda obj: {'__time__': True, 'as_str': obj.strftime("%H:%M:%S.%f")},
        decoder=lambda obj: datetime.datetime.strptime(obj['as_str'], "%H:%M:%S.%f").time()
    ),
    GeoField.Coordinates: EncoderDecoder(
        key='__Coordinates__',
        encoder=lambda obj: {'__Coordinates__': True, 'latitude': obj.latitude, 'longitude': obj.longitude},
        decoder=lambda obj: GeoField.Coordinates(obj['latitude'], obj['longitude'])
    ),
}
TYPE_DECODER_KEYSTRINGS = [ed.key for ed in TYPE_ENCODER_DECODERS.values()]


def decode_custom_types(obj):
    if isinstance(obj, dict) and 'as_str' in obj:
        for encoder_decoder in TYPE_ENCODER_DECODERS.values():
            if encoder_decoder.key in obj:
                return encoder_decoder.decoder(obj)
    return obj


def encode_popoto_model_obj(obj: 'Model') -> dict:
    import msgpack_numpy as m
    m.patch()

    encoded_hashmap = dict()
    for field_name, field in obj._meta.fields.items():
        value = getattr(obj, field_name)

        # use db_key string for relationships
        from ..fields.relationship import Relationship
        if value and isinstance(field, Relationship):
            if not isinstance(value, field.model):
                raise ModelException(f"Relationship field requires {field.model} model instance. got {value} insteadd")
            encoded_value = msgpack.packb(value.db_key)
        elif field.type in TYPE_ENCODER_DECODERS.keys():
            encoded_value = msgpack.packb(value, default=TYPE_ENCODER_DECODERS[field.type].encoder)
        else:
            encoded_value = msgpack.packb(value)

        encoded_hashmap[str(field_name).encode(ENCODING)] = encoded_value

    return encoded_hashmap


def decode_popoto_model_hashmap(model_class: 'Model', redis_hash: dict) -> 'Model':
    if len(redis_hash):
        return model_class(**{
            key_b.decode(ENCODING): decode_custom_types(msgpack.unpackb(value_b))
            for key_b, value_b in redis_hash.items()
        })
    else:
        return None


class DB_key(list):
    def __init__(self, *key_partials):
        def flatten(yet_flat):
            for item in yet_flat:
                if isinstance(item, Iterable) and not isinstance(item, (str, bytes)):
                    yield from flatten(item)
                else:
                    yield item

        super().__init__(flatten(key_partials))

    @classmethod
    def clean(cls, value: str, ignore_colons: bool = False) -> str:
        value = value.replace('/', '//')
        for char in "'?*^[]-":
            value = value.replace(char, f"/{char}")
        if not ignore_colons:
            value = value.replace(':', '_')
        return value

    def __str__(self):
        return ":".join([self.clean(partial) for partial in self])

    @property
    def redis_key(self):
        return str(self)

    def exists(self):
        return True if POPOTO_REDIS_DB.exists(self.redis_key) > 0 else False

    def get_instance(self, model_class):
        redis_hash = POPOTO_REDIS_DB.hgetall(self.redis_key)
        return decode_popoto_model_hashmap(model_class, redis_hash)
