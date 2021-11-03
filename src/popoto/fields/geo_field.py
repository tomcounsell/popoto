from time import time
from .field import Field
import uuid

from collections import namedtuple

from ..models.query import QueryException
from ..redis_db import POPOTO_REDIS_DB

Coordinates = namedtuple('Coordinates', 'latitude longitude')

class GeoField(Field):
    """
    A field that enables geospatial data and geospatial search
    required: latitude, longitude
    """
    type: type = tuple
    latitude: float = None
    longitude: float = None
    default: tuple = (None, None)
    null: bool = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        new_kwargs = {  # default
            'latitude': None,
            'longitude': None,
            'null': True,
        }
        new_kwargs.update(kwargs)
        for k in new_kwargs:
            setattr(self, k, new_kwargs[k])

    def get_filter_query_params(self, field_name):
        return super().get_filter_query_params(field_name) + [
            f'{field_name}_latitude',
            f'{field_name}_longitude',
            f'{field_name}_coordinates',
            f'{field_name}_radius',
            f'{field_name}_radius_units',
        ]

    @classmethod
    def filter_query(cls, model: 'Model', field_name: str, **query_params) -> set:
        """
        :param model: the popoto.Model to query from
        :param field_name: the name of the field being filtered on
        :param query_params: dict of filter args and values
        :return: set{db_key, db_key, ..}
        """

        coordinates = Coordinates(None, None)
        member, radius, units = None, 0, 'm'
        for query_param, query_value in query_params.items():

            if '_coordinates' in query_param:
                if not isinstance(query_value, tuple) or not len(query_value) == 2:
                    raise QueryException(f"{query_param} must be assigned a tuple = (latitude, longitude)")
                coordinates = Coordinates(query_value[0], query_value[1])

            elif '_latitude' in query_param:
                coordinates.latitude = query_value
            elif '_longitude' in query_param:
                coordinates.longitude = query_value

            elif '_member' in query_param:
                if not isinstance(query_value, model):
                    raise QueryException(f"{query_param} must be assigned a tuple = (latitude, longitude)")
                member = query_value

            elif query_param == 'radius':
                radius = query_value

            elif query_param == 'units':
                if query_value not in ['m', 'km', 'ft', 'mi']:
                    raise QueryException(f"{query_param} must be one of m|km|ft|mi ")
                units = query_value

        if member:
            redis_db_keys_list = POPOTO_REDIS_DB.georadiusbymember(
                model._meta.db_class_key, member=member.db_key,
                radius=radius, units=units
            )

        elif bool(coordinates.latitude and coordinates.longitude):
            redis_db_keys_list = POPOTO_REDIS_DB.georadius(
                model._meta.db_class_key,
                longitude=coordinates.longitude, latitude=coordinates.latitude,
                radius=radius, units=units
            )
        else:
            raise QueryException(f"missing one or more required parameters. "
                                 f"geofilter requires either coordinates or instance of the same model")

        return set(redis_db_keys_list)
