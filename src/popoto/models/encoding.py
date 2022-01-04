import datetime
from collections import namedtuple
from decimal import Decimal

import msgpack

from ..exceptions import ModelException
from ..fields.geo_field import GeoField
from ..redis_db import ENCODING

EncoderDecoder = namedtuple("EncoderDecoder", "key, encoder, decoder")

TYPE_ENCODER_DECODERS = {
    Decimal: EncoderDecoder(
        key='__Decimal__',
        encoder=lambda obj: {'__Decimal__': True, 'as_encodable': str(obj)},
        decoder=lambda obj: Decimal(obj['as_encodable'])
    ),
    tuple: EncoderDecoder(
        key='__tuple__',
        encoder=lambda obj: {'__tuple__': True, 'as_encodable': list(obj)},
        decoder=lambda obj: tuple(obj['as_encodable'])
    ),
    set: EncoderDecoder(
        key='__set__',
        encoder=lambda obj: {'__set__': True, 'as_encodable': list(obj)},
        decoder=lambda obj: set(obj['as_encodable'])
    ),
    datetime.datetime: EncoderDecoder(
        key='__datetime__',
        encoder=lambda obj: {'__datetime__': True, 'as_encodable': obj.strftime("%Y%m%dT%H:%M:%S.%f")},
        decoder=lambda obj: datetime.datetime.strptime(obj['as_encodable'], "%Y%m%dT%H:%M:%S.%f")
    ),
    datetime.date: EncoderDecoder(
        key='__date__',
        encoder=lambda obj: {'__date__': True, 'as_encodable': obj.strftime("%Y%m%d")},
        decoder=lambda obj: datetime.datetime.strptime(obj['as_encodable'], "%Y%m%d").date()
    ),
    datetime.time: EncoderDecoder(
        key='__time__',
        encoder=lambda obj: {'__time__': True, 'as_encodable': obj.strftime("%H:%M:%S.%f")},
        decoder=lambda obj: datetime.datetime.strptime(obj['as_encodable'], "%H:%M:%S.%f").time()
    ),
}
TYPE_DECODER_KEYSTRINGS = [ed.key for ed in TYPE_ENCODER_DECODERS.values()]


def decode_custom_types(obj):
    if isinstance(obj, dict) and 'as_encodable' in obj:
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
                raise ModelException(f"Relationship field requires {field.model} model instance. got {value} instead")
            encoded_value = msgpack.packb(value.db_key.redis_key)
            # todo: refactor to store db_key list, not redis_key

        elif value and field.type in TYPE_ENCODER_DECODERS.keys():
            encoded_value = msgpack.packb(TYPE_ENCODER_DECODERS[field.type].encoder(value))
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
