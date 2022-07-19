import datetime
import json
from collections import namedtuple
from decimal import Decimal
import msgpack
import pandas as pd
from ..exceptions import ModelException
from ..redis_db import ENCODING

EncoderDecoder = namedtuple("EncoderDecoder", "key, encoder, decoder")

TYPE_ENCODER_DECODERS = {
    Decimal: EncoderDecoder(
        key="__Decimal__",
        encoder=lambda obj: {"__Decimal__": True, "as_encodable": str(obj)},
        decoder=lambda obj: Decimal(obj["as_encodable"]),
    ),
    tuple: EncoderDecoder(
        key="__tuple__",
        encoder=lambda obj: {"__tuple__": True, "as_encodable": list(obj)},
        decoder=lambda obj: tuple(obj["as_encodable"]),
    ),
    set: EncoderDecoder(
        key="__set__",
        encoder=lambda obj: {"__set__": True, "as_encodable": list(obj)},
        decoder=lambda obj: set(obj["as_encodable"]),
    ),
    datetime.datetime: EncoderDecoder(
        key="__datetime__",
        encoder=lambda obj: {
            "__datetime__": True,
            "as_encodable": obj.strftime("%Y%m%dT%H:%M:%S.%f"),
        },
        decoder=lambda obj: datetime.datetime.strptime(
            obj["as_encodable"], "%Y%m%dT%H:%M:%S.%f"
        ),
    ),
    datetime.date: EncoderDecoder(
        key="__date__",
        encoder=lambda obj: {"__date__": True, "as_encodable": obj.strftime("%Y%m%d")},
        decoder=lambda obj: datetime.datetime.strptime(
            obj["as_encodable"], "%Y%m%d"
        ).date(),
    ),
    datetime.time: EncoderDecoder(
        key="__time__",
        encoder=lambda obj: {
            "__time__": True,
            "as_encodable": obj.strftime("%H:%M:%S.%f"),
        },
        decoder=lambda obj: datetime.datetime.strptime(
            obj["as_encodable"], "%H:%M:%S.%f"
        ).time(),
    ),
    pd.DataFrame: EncoderDecoder(
        key="__dataframe__",
        encoder=lambda obj: {
            "__dataframe__": True,
            "as_encodable": obj.to_json(),
        },
        decoder=lambda obj: pd.read_json(obj["as_encodable"]),
    ),
}
DECODERS_BY_KEYSTRING = {
    encoder_decoder.key: encoder_decoder.decoder
    for encoder_decoder in TYPE_ENCODER_DECODERS.values()
}


def decode_custom_types(obj):
    if isinstance(obj, dict) and "as_encodable" in obj:
        for keystring in DECODERS_BY_KEYSTRING.keys():
            if keystring in obj:
                return DECODERS_BY_KEYSTRING[keystring](obj)
    return obj


# def decode_custom_types(obj):
#     if isinstance(obj, dict) and "as_encodable" in obj:
#         for encoder_decoder in TYPE_ENCODER_DECODERS.values():
#             if encoder_decoder.key in obj:
#                 return encoder_decoder.decoder(obj)
#     return obj


def encode_popoto_model_obj(obj: "Model") -> dict:
    import msgpack_numpy as m

    m.patch()

    encoded_hashmap = dict()
    for field_name, field in obj._meta.fields.items():
        value = getattr(obj, field_name)

        # use db_key string for relationships
        from ..fields.relationship import Relationship

        if value is not None and isinstance(field, Relationship):
            if not isinstance(value, field.model):
                raise ModelException(
                    f"Relationship field requires {field.model} model instance. got {value} instead"
                )
            encoded_value = msgpack.packb(value.db_key.redis_key)
            # todo: refactor to store db_key list, not redis_key

        elif value is not None and field.type in TYPE_ENCODER_DECODERS.keys():
            encoded_value = msgpack.packb(
                TYPE_ENCODER_DECODERS[field.type].encoder(value)
            )
        else:
            encoded_value = msgpack.packb(value)

        encoded_hashmap[str(field_name).encode(ENCODING)] = encoded_value

    return encoded_hashmap


def decode_popoto_model_hashmap(
    model_class: "Model", redis_hash: dict, fields_only=False
) -> "Model":
    """
    fields_only=True return only the fields dict, not a model object
    (also skips decoding of the field keys)
    """
    if len(redis_hash):
        model_attrs = {
            key_b.decode(ENCODING)
            if not fields_only
            else key_b: decode_custom_types(msgpack.unpackb(value_b))
            for key_b, value_b in redis_hash.items()
        }
        return model_attrs if fields_only else model_class(**model_attrs)

    return None
