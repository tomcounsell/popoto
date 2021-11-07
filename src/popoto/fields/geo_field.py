import logging
from time import time
from .field import Field, logger
import uuid

from collections import namedtuple
from ..redis_db import POPOTO_REDIS_DB


class GeoField(Field):
    """
    A field that enables geospatial data and geospatial search
    required: latitude, longitude
    """
    Coordinates = namedtuple('Coordinates', 'latitude longitude')

    type: type = tuple
    latitude: float = None
    longitude: float = None
    default: tuple = Coordinates(None, None)
    null: bool = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        geofield_options = {  # default
            'type': tuple,
            'latitude': None,
            'longitude': None,
            'null': True,
            'default': GeoField.Coordinates(None, None),
        }
        # set geofield_options, let kwargs override
        for k, v in geofield_options.items():
            setattr(self, k, kwargs.get(k, v))

    def get_filter_query_params(self, field_name):
        return super().get_filter_query_params(field_name) + [
            f'{field_name}_latitude',
            f'{field_name}_longitude',
            f'{field_name}_radius',
            f'{field_name}_radius_unit',
        ]

    @classmethod
    def is_valid(cls, field, value) -> bool:
        if not super().is_valid(field, value):
            return False
        if value is None:
            return True
        if isinstance(value, GeoField.Coordinates):
            pass
        elif isinstance(value, tuple):
            value = GeoField.Coordinates(value[0], value[1])
        else:
            logger.error(f"GeoField MUST be type GeoField.Coordinates or tuple, NOT {type(value)}")
            return False
        if field.null and not any([value.latitude, value.longitude]):
            return True
        elif bool(value.latitude) != bool(value.longitude):
            logger.error(f"latitude is {value.latitude} and longitude is {value.longitude}")
            logger.error(f"BOTH latitude AND longitude MUST have a value or be None")
            return False
        elif not all([value.latitude, value.longitude]):
            logger.error(f"BOTH latitude AND longitude MUST have a value")
            return False
        try:
            float(value.latitude), float(value.longitude)
        except ValueError as e:
            logger.error(e)
            return False
        except TypeError as e:
            logger.error(e)
            return False
        return True

    @classmethod
    def format_value_pre_save(cls, field_value):
        """
        format field_value before saving to db
        return corrected field_value
        assumes validation is already passed
        """
        if isinstance(field_value, GeoField.Coordinates):
            return field_value
        if isinstance(field_value, tuple):
            return GeoField.Coordinates(field_value[0], field_value[1])
        if cls.null:
            return GeoField.Coordinates(None, None)
        return field_value

    @classmethod
    def on_save(cls, model: 'Model', field_name: str, field_value: 'GeoField.Coordinates', pipeline=None):
        field_db_key = f"{cls.field_class_key}:{model._meta.db_class_key}:{field_name}"
        longitude = field_value.longitude
        latitude = field_value.latitude
        member = model.db_key
        if pipeline:
            return pipeline.geoadd(field_db_key,  longitude, latitude, member)
        else:
            return POPOTO_REDIS_DB.geoadd(field_db_key,  longitude, latitude, member)

    @classmethod
    def on_delete(cls, model: 'Model', field_name: str, pipeline=None):
        field_db_key = f"{cls.field_class_key}:{model._meta.db_class_key}:{field_name}"
        member = model.db_key
        if pipeline:
            return pipeline.zrem(field_db_key, member)
        else:
            return POPOTO_REDIS_DB.zrem(field_db_key, member)

    @classmethod
    def filter_query(cls, model: 'Model', field_name: str, **query_params) -> set:
        """
        :param model: the popoto.Model to query from
        :param field_name: the name of the field being filtered on
        :param query_params: dict of filter args and values
        :return: set{db_key, db_key, ..}
        """
        field = model._meta.fields[field_name]
        field_db_key = f"{cls.field_class_key}:{model._meta.db_class_key}:{field_name}"
        coordinates = GeoField.Coordinates(None, None)
        member, radius, unit = None, 1, 'm'
        for query_param, query_value in query_params.items():

            if query_param == f'{field_name}':
                if isinstance(query_value, GeoField.Coordinates):
                    coordinates = query_value
                elif not isinstance(query_value, tuple) or not len(query_value) == 2:
                    from ..models.query import QueryException
                    raise QueryException(f"{query_param} must be assigned a tuple = (latitude, longitude)")
                else:
                    coordinates = GeoField.Coordinates(query_value[0], query_value[1])

            elif query_param.endswith('_latitude'):
                coordinates = GeoField.Coordinates(query_value, coordinates.longitude)
            elif query_param.endswith('_longitude'):
                coordinates = GeoField.Coordinates(coordinates.latitude, query_value)

            elif '_member' in query_param:
                if not isinstance(query_value, model):
                    from ..models.query import QueryException
                    raise QueryException(f"{query_param} must be assigned a tuple = (latitude, longitude)")
                member = query_value

            elif query_param.endswith('_radius'):
                radius = query_value

            elif query_param.endswith('_radius_unit'):
                if query_value not in ['m', 'km', 'ft', 'mi']:
                    from ..models.query import QueryException
                    raise QueryException(f"{query_param} must be one of m|km|ft|mi ")
                unit = query_value

        if member:
            redis_db_keys_list = POPOTO_REDIS_DB.georadiusbymember(
                field_db_key, member=member.db_key,
                radius=radius, unit=unit
            )

        elif coordinates.latitude and coordinates.longitude:
            # logger.debug(f"geo query on {dict(model=model._meta.db_class_key, longitude=coordinates.longitude, latitude=coordinates.latitude, radius=radius, unit=unit)}")
            redis_db_keys_list = POPOTO_REDIS_DB.georadius(
                field_db_key,
                longitude=coordinates.longitude, latitude=coordinates.latitude,
                radius=radius, unit=unit
            )
            # logger.debug(f"geo query returned {redis_db_keys_list}")
        else:
            from ..models.query import QueryException
            raise QueryException(f"missing one or more required parameters. "
                                 f"geofilter requires either coordinates or instance of the same model")

        return set(redis_db_keys_list)
