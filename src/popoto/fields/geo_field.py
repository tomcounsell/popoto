from collections import namedtuple
import redis
from .field import Field
import logging
from ..models.db_key import DB_key

logger = logging.getLogger('POPOTO.GeoField')
from ..redis_db import POPOTO_REDIS_DB


class GeoField(Field):
    """
    A field that stores geospatial coordinates and enables geospatial search
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
        geofield_defaults = {
            'type': tuple,
            'latitude': None,
            'longitude': None,
            'null': True,
            'default': GeoField.Coordinates(None, None),
        }
        self.field_defaults.update(geofield_defaults)
        # set field options, let kwargs override
        for k, v in geofield_defaults.items():
            setattr(self, k, kwargs.get(k, v))

    def get_filter_query_params(self, field_name):
        return super().get_filter_query_params(field_name) + [
            f'{field_name}',
            f'{field_name}__isnull',
            f'{field_name}_latitude',
            f'{field_name}_longitude',
            f'{field_name}_radius',
            f'{field_name}_radius_unit',
        ]

    @classmethod
    def is_valid(cls, field, value, null_check=True, **kwargs) -> bool:
        if not super().is_valid(field, value, null_check):
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
        elif null_check and not all([value.latitude, value.longitude]):
            logger.error(f"latitude is {value.latitude} and longitude is {value.longitude}")
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

    def format_value_pre_save(self, field_value):
        """
        format field_value before saving to db
        return corrected field_value
        assumes validation is already passed
        """
        if isinstance(field_value, GeoField.Coordinates):
            return field_value
        if isinstance(field_value, tuple):
            return GeoField.Coordinates(field_value[0], field_value[1])
        if self.null:
            return GeoField.Coordinates(None, None)
        return field_value

    @classmethod
    def get_geo_db_key(cls, model, field_name: str) -> DB_key:
        return cls.get_special_use_field_db_key(model, field_name)

    @classmethod
    def on_save(cls, model_instance: 'Model', field_name: str, field_value: 'GeoField.Coordinates', pipeline=None, **kwargs):
        geo_db_key = cls.get_geo_db_key(model_instance, field_name)
        geo_member = model_instance.db_key.redis_key
        if not field_value or not (field_value.longitude and field_value.latitude):
            if pipeline:
                return pipeline.zrem(geo_db_key.redis_key, geo_member)
            else:
                return POPOTO_REDIS_DB.zrem(geo_db_key.redis_key, geo_member)
        if pipeline:
            return pipeline.geoadd(geo_db_key.redis_key, field_value.longitude, field_value.latitude, geo_member)
        else:
            return POPOTO_REDIS_DB.geoadd(geo_db_key.redis_key, field_value.longitude, field_value.latitude, geo_member)

    @classmethod
    def on_delete(cls, model_instance: 'Model', field_name: str, field_value, pipeline: redis.client.Pipeline = None, **kwargs):
        geo_db_key = cls.get_geo_db_key(model_instance, field_name)
        geo_member = model_instance.db_key.redis_key
        if pipeline:
            return pipeline.zrem(geo_db_key.redis_key, geo_member)
        else:
            return POPOTO_REDIS_DB.zrem(geo_db_key.redis_key, geo_member)

    @classmethod
    def filter_query(cls, model: 'Model', field_name: str, **query_params) -> set:
        """
        :param model: the popoto.Model to query from
        :param field_name: the name of the field being filtered on
        :param query_params: dict of filter args and values
        :return: set{db_key, db_key, ..}
        """
        field = model._meta.fields[field_name]
        geo_db_key = cls.get_geo_db_key(model, field_name)
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
                geo_db_key.redis_key, member=member.db_key.redis_key,
                radius=radius, unit=unit  # , withdist=True, sort='asc'
            )

        elif coordinates.latitude and coordinates.longitude:
            # logger.debug(f"geo query on {dict(model=model._meta.db_class_key, longitude=coordinates.longitude, latitude=coordinates.latitude, radius=radius, unit=unit)}")
            redis_db_keys_list = POPOTO_REDIS_DB.georadius(
                geo_db_key.redis_key,
                longitude=coordinates.longitude, latitude=coordinates.latitude,
                radius=radius, unit=unit
            )
            # logger.debug(f"geo query returned {redis_db_keys_list}")
        else:
            from ..models.query import QueryException
            raise QueryException(f"missing one or more required parameters. "
                                 f"geofilter requires either coordinates or instance of the same model")

        return set(redis_db_keys_list)
