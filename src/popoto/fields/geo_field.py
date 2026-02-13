"""
Geospatial field implementation for Popoto Redis ORM.

This module provides GeoField, which leverages Redis's native geospatial indexing
capabilities (GEOADD, GEORADIUS, GEORADIUSBYMEMBER) to enable location-based
queries on Popoto models.

Design Philosophy:
    Redis stores geospatial data in sorted sets using geohash encoding, making
    radius queries extremely efficient (O(N+log(M)) where N is the number of
    elements in the radius and M is total elements). By exposing this through
    a Django-like field interface, Popoto enables powerful location-based
    applications with minimal boilerplate.

Key Design Decisions:
    1. Separate geo index: Coordinates are stored in a dedicated Redis sorted set
       (not in the model's hash), enabling efficient spatial queries without
       loading full model data.

    2. Latitude-first convention: The Coordinates namedtuple uses (latitude, longitude)
       order, matching common geographic convention (e.g., Google Maps), even though
       Redis GEOADD expects (longitude, latitude) internally.

    3. Null-safe design: Both coordinates must be present or both must be None.
       Partial coordinates (only lat or only lng) are explicitly rejected to
       prevent data integrity issues.

Example:
    class Restaurant(Model):
        name = KeyField()
        location = GeoField()

    restaurant = Restaurant.create(
        name="Pizza Place",
        location=GeoField.Coordinates(latitude=40.7128, longitude=-74.0060)
    )

    # Find all restaurants within 5km
    nearby = Restaurant.query.filter(
        location=(40.7128, -74.0060),
        location_radius=5,
        location_radius_unit='km'
    )
"""

from collections import namedtuple
import redis
from .field import Field
import logging
from ..models.db_key import DB_key

logger = logging.getLogger("POPOTO.GeoField")
from ..redis_db import POPOTO_REDIS_DB


class GeoField(Field):
    """
    A field that stores geospatial coordinates and enables radius-based queries.

    GeoField provides a bridge between Python's (latitude, longitude) representation
    and Redis's powerful GEOSPATIAL commands. Each GeoField on a model creates a
    separate Redis sorted set that acts as a spatial index, enabling efficient
    "find all objects within X distance" queries.

    Values are ``GeoField.Coordinates(latitude, longitude)`` namedtuples
    (plain tuples also accepted). Backed by a Redis GEO set.

    The field stores coordinates both in the model's Redis hash (for retrieval)
    and in a dedicated geo index (for spatial queries). This dual-storage approach
    optimizes for both direct access and radius searches.

    Filter lookups: coordinates, ``_latitude``, ``_longitude``, ``_radius``,
    ``_radius_unit`` (m/km/ft/mi), ``_member``, ``_with_distances``.

    Attributes:
        Coordinates: A namedtuple for type-safe coordinate representation.
            Provides self-documenting code and prevents lat/lng order confusion.
        type: Always tuple; coordinates are stored as (latitude, longitude).
        latitude: Current latitude value (float or None).
        longitude: Current longitude value (float or None).
        null: Defaults to True, allowing models without location data.

    Example:
        class City(Model):
            name = KeyField()
            coordinates = GeoField()

        rome = City.create(
            name="Rome",
            coordinates=GeoField.Coordinates(latitude=41.9028, longitude=12.4964)
        )

        # Query by coordinates and radius
        nearby_cities = City.query.filter(
            coordinates=rome.coordinates,
            coordinates_radius=100,
            coordinates_radius_unit='km'
        )
    """

    Coordinates = namedtuple("Coordinates", "latitude longitude")

    type: type = tuple
    latitude: float = None
    longitude: float = None
    default: tuple = Coordinates(None, None)
    null: bool = True

    def __init__(self, **kwargs):
        """
        Initialize a GeoField with optional configuration overrides.

        GeoField defaults to nullable (null=True) since many models may have
        optional location data. The default value is Coordinates(None, None)
        rather than None itself, providing a consistent type for coordinate
        access even when no location is set.

        Args:
            **kwargs: Field configuration options. Common options include:
                - null (bool): Whether the field accepts None. Defaults to True.
                - default: Default value if none provided. Defaults to
                  Coordinates(None, None).

        Note:
            Unlike other fields, GeoField's type is always tuple and cannot
            be overridden, as geospatial operations require coordinate pairs.
        """
        super().__init__(**kwargs)
        geofield_defaults = {
            "type": tuple,
            "latitude": None,
            "longitude": None,
            "null": True,
            "default": GeoField.Coordinates(None, None),
        }
        self.field_defaults.update(geofield_defaults)
        # set field options, let kwargs override
        for k, v in geofield_defaults.items():
            setattr(self, k, kwargs.get(k, v))

    def get_filter_query_params(self, field_name) -> set:
        """
        Return the set of valid query parameter names for filtering on this field.

        GeoField extends the base query parameters with geospatial-specific
        options that enable radius-based searches. The parameter naming follows
        a consistent pattern using the field name as prefix.

        Args:
            field_name: The name of this field on the model (e.g., 'location').

        Returns:
            A set of valid parameter names for Query.filter(). For a field
            named 'location', this includes:
            - location: Coordinates tuple or (lat, lng) tuple
            - location__isnull: Boolean to filter by presence of coordinates
            - location_latitude: Latitude for radius search center
            - location_longitude: Longitude for radius search center
            - location_radius: Search radius (default: 1)
            - location_radius_unit: Unit of measure ('m', 'km', 'ft', 'mi')
        """
        return (
            super()
            .get_filter_query_params(field_name)
            .union(
                {
                    f"{field_name}",
                    f"{field_name}__isnull",
                    f"{field_name}_latitude",
                    f"{field_name}_longitude",
                    f"{field_name}_radius",
                    f"{field_name}_radius_unit",
                    f"{field_name}_member",
                    f"{field_name}_with_distances",
                }
            )
        )

    @classmethod
    def is_valid(cls, field, value, null_check=True, **kwargs) -> bool:
        """
        Validate that a value is acceptable for this GeoField.

        Enforces the "all-or-nothing" coordinate rule: either both latitude
        and longitude must be provided, or both must be None. This prevents
        partial coordinate data that would be meaningless for geospatial
        operations.

        Args:
            field: The GeoField instance being validated against.
            value: The value to validate. May be a Coordinates namedtuple,
                a plain (lat, lng) tuple, or None.
            null_check: If True, requires non-null values when field.null
                is False. Defaults to True.
            **kwargs: Additional arguments (unused, for API compatibility).

        Returns:
            True if the value is valid for this field, False otherwise.
            Logs specific error messages when validation fails.

        Validation Rules:
            1. None is valid if field.null is True
            2. Must be a tuple or Coordinates namedtuple
            3. Both lat and lng must be present, or both must be absent
            4. Both values must be convertible to float
        """
        if not super().is_valid(field, value, null_check):
            return False
        if value is None:
            return True
        if isinstance(value, GeoField.Coordinates):
            pass
        elif isinstance(value, tuple):
            value = GeoField.Coordinates(value[0], value[1])
        else:
            logger.error(
                f"GeoField MUST be type GeoField.Coordinates or tuple, NOT {type(value)}"
            )
            return False
        if field.null and not any([value.latitude, value.longitude]):
            return True
        elif bool(value.latitude) != bool(value.longitude):
            logger.error(
                f"latitude is {value.latitude} and longitude is {value.longitude}"
            )
            logger.error("BOTH latitude AND longitude MUST have a value or be None")
            return False
        elif null_check and not all([value.latitude, value.longitude]):
            logger.error(
                f"latitude is {value.latitude} and longitude is {value.longitude}"
            )
            logger.error("BOTH latitude AND longitude MUST have a value")
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

    def format_value_pre_save(self, field_value, **kwargs):
        """
        Normalize coordinate values to the Coordinates namedtuple before saving.

        This method ensures consistent storage format regardless of how
        coordinates were provided (plain tuple vs Coordinates namedtuple).
        This normalization simplifies downstream code that can always expect
        the Coordinates type with named .latitude and .longitude attributes.

        Args:
            field_value: The value being saved. May be a Coordinates namedtuple,
                a plain (lat, lng) tuple, or None/invalid.
            **kwargs: Additional keyword arguments for forward compatibility
                (e.g., skip_auto_now used by other field types).

        Returns:
            A GeoField.Coordinates namedtuple. Returns Coordinates(None, None)
            for nullable fields with missing data.

        Note:
            Called automatically during model.save(). Assumes validation has
            already passed via is_valid().
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
        """
        Generate the Redis key for this field's geospatial index.

        Each GeoField maintains a separate Redis sorted set (used internally
        by Redis's GEO commands) that maps model instance keys to their
        geohash-encoded coordinates. This method returns the key for that
        sorted set.

        Args:
            model: The Model class or instance this field belongs to.
            field_name: The name of the GeoField on the model.

        Returns:
            A DB_key pointing to the Redis sorted set storing the geo index.
            Format: "$GeoF:{ModelClassName}:{field_name}"

        Example:
            For a model `Restaurant` with field `location`, returns a key like:
            "$GeoF:Restaurant:location"
        """
        return cls.get_special_use_field_db_key(model, field_name)

    @classmethod
    def on_save(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value: "GeoField.Coordinates",
        pipeline=None,
        **kwargs,
    ):
        """
        Update the geospatial index when a model instance is saved.

        This hook maintains the Redis geo index in sync with model data.
        When coordinates are present, adds/updates the instance's position
        in the geo sorted set. When coordinates are null, removes the
        instance from the index to prevent stale location data in queries.

        Args:
            model_instance: The Model instance being saved.
            field_name: The name of the GeoField being processed.
            field_value: The Coordinates being saved (may be null).
            pipeline: Optional Redis pipeline for batching commands. When
                provided, commands are queued rather than executed immediately.
            **kwargs: Additional arguments (unused, for API compatibility).

        Returns:
            The pipeline (if provided) or the Redis command result.

        Note:
            Redis GEOADD expects (longitude, latitude) order, but Popoto's
            Coordinates uses (latitude, longitude). This method handles the
            conversion internally.
        """
        geo_db_key = cls.get_geo_db_key(model_instance, field_name)
        geo_member = model_instance.db_key.redis_key
        if not field_value or not (field_value.longitude and field_value.latitude):
            if pipeline:
                return pipeline.zrem(geo_db_key.redis_key, geo_member)
            else:
                return POPOTO_REDIS_DB.zrem(geo_db_key.redis_key, geo_member)
        if pipeline:
            return pipeline.geoadd(
                name=geo_db_key.redis_key,
                values=[field_value.longitude, field_value.latitude, geo_member],
            )
        else:
            return POPOTO_REDIS_DB.geoadd(
                name=geo_db_key.redis_key,
                values=[field_value.longitude, field_value.latitude, geo_member],
            )

    @classmethod
    def on_delete(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value,
        pipeline: redis.client.Pipeline = None,
        **kwargs,
    ):
        """
        Remove a model instance from the geospatial index when deleted.

        This hook ensures the geo index stays clean when models are deleted.
        Without this cleanup, deleted instances would remain in radius query
        results, returning keys that no longer exist in Redis.

        Args:
            model_instance: The Model instance being deleted.
            field_name: The name of the GeoField being processed.
            field_value: The current coordinates (unused, removal is unconditional).
            pipeline: Optional Redis pipeline for batching commands.
            **kwargs: Additional arguments (unused, for API compatibility).

        Returns:
            The pipeline (if provided) or the Redis command result.

        Note:
            Always removes the instance from the geo index, regardless of
            whether coordinates were set. This is a safe no-op if the
            instance wasn't in the index.
        """
        geo_db_key = cls.get_geo_db_key(model_instance, field_name)
        # Use saved_redis_key if provided, otherwise fall back to current db_key
        geo_member = kwargs.get("saved_redis_key", model_instance.db_key.redis_key)
        if pipeline:
            return pipeline.zrem(geo_db_key.redis_key, geo_member)
        else:
            return POPOTO_REDIS_DB.zrem(geo_db_key.redis_key, geo_member)

    @classmethod
    def filter_query(cls, model: "Model", field_name: str, **query_params):
        """
        Execute a geospatial radius query and return matching model keys.

        This is the core query method that translates Popoto's Django-like
        filter syntax into Redis GEORADIUS or GEORADIUSBYMEMBER commands.
        It supports two query modes:

        1. Coordinate-based: Search around a specific (lat, lng) point
        2. Member-based: Search around an existing model instance's location

        Args:
            model: The Model class to query.
            field_name: The name of the GeoField to filter on.
            **query_params: Filter parameters including:
                - {field_name}: Coordinates tuple for search center
                - {field_name}_latitude: Latitude of search center
                - {field_name}_longitude: Longitude of search center
                - {field_name}_member: Model instance to search around
                - {field_name}_radius: Search radius (default: 1)
                - {field_name}_radius_unit: 'm', 'km', 'ft', or 'mi' (default: 'm')

        Returns:
            set{db_key, db_key, ..} or tuple(set, distances_dict, unit) if with_distances=True.
            A set of Redis keys (bytes) for model instances within the
            specified radius. These keys can be used to fetch full model
            objects via Query.get_many_objects().

        Raises:
            QueryException: If required coordinate parameters are missing,
                if radius_unit is invalid, or if query_params are malformed.

        Example:
            # Find restaurants within 5km of Times Square
            keys = GeoField.filter_query(
                Restaurant,
                'location',
                location=(40.7580, -73.9855),
                location_radius=5,
                location_radius_unit='km'
            )
        """
        field = model._meta.fields[field_name]
        geo_db_key = cls.get_geo_db_key(model, field_name)
        coordinates = GeoField.Coordinates(None, None)
        member, radius, unit = None, 1, "m"
        with_distances = False
        for query_param, query_value in query_params.items():

            if query_param == f"{field_name}":
                if isinstance(query_value, GeoField.Coordinates):
                    coordinates = query_value
                elif not isinstance(query_value, tuple) or not len(query_value) == 2:
                    from ..models.query import QueryException

                    raise QueryException(
                        f"{query_param} must be assigned a tuple = (latitude, longitude)"
                    )
                else:
                    coordinates = GeoField.Coordinates(query_value[0], query_value[1])

            elif query_param.endswith("_latitude"):
                coordinates = GeoField.Coordinates(query_value, coordinates.longitude)
            elif query_param.endswith("_longitude"):
                coordinates = GeoField.Coordinates(coordinates.latitude, query_value)

            elif "_member" in query_param:
                if not isinstance(query_value, model):
                    from ..models.query import QueryException

                    raise QueryException(
                        f"{query_param} must be assigned a tuple = (latitude, longitude)"
                    )
                member = query_value

            elif query_param.endswith("_radius"):
                radius = query_value

            elif query_param.endswith("_radius_unit"):
                if query_value not in ["m", "km", "ft", "mi"]:
                    from ..models.query import QueryException

                    raise QueryException(f"{query_param} must be one of m|km|ft|mi ")
                unit = query_value

            elif query_param.endswith("_with_distances"):
                with_distances = bool(query_value)

        if member:
            redis_db_keys_list = POPOTO_REDIS_DB.georadiusbymember(
                geo_db_key.redis_key,
                member=member.db_key.redis_key,
                radius=radius,
                unit=unit,
                withdist=with_distances,
                sort="ASC" if with_distances else None,
            )

        elif coordinates.latitude is not None and coordinates.longitude is not None:
            redis_db_keys_list = POPOTO_REDIS_DB.georadius(
                geo_db_key.redis_key,
                longitude=coordinates.longitude,
                latitude=coordinates.latitude,
                radius=radius,
                unit=unit,
                withdist=with_distances,
                sort="ASC" if with_distances else None,
            )
        else:
            from ..models.query import QueryException

            raise QueryException(
                "missing one or more required parameters. "
                "geofilter requires either coordinates or instance of the same model"
            )

        if with_distances:
            # Result is [(key, distance), ...] when withdist=True
            distances = {item[0]: item[1] for item in redis_db_keys_list}
            return set(distances.keys()), distances, unit
        else:
            return set(redis_db_keys_list)
