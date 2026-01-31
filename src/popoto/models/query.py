import logging
import asyncio
import sys
import functools

from .db_key import DB_key
from ..redis_db import POPOTO_REDIS_DB, ENCODING

logger = logging.getLogger("POPOTO.Query")

# Python 3.8 compatibility for asyncio.to_thread()
if sys.version_info >= (3, 9):
    to_thread = asyncio.to_thread
else:
    # Backport for Python 3.8
    async def to_thread(func, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, functools.partial(func, *args, **kwargs)
        )


class QueryException(Exception):
    pass


class Query:
    """
    an interface for db query operations using Popoto Models
    """

    model_class: "Model"
    options: "ModelOptions"

    def __init__(self, model_class: "Model"):
        self.model_class = model_class
        self.options = model_class._meta
        self._geo_distances = {}  # {redis_key: distance}
        self._geo_distance_unit = None  # unit for distance values

    def get(self, db_key: DB_key = None, redis_key: str = None, **kwargs) -> "Model":
        if (
            not db_key
            and not redis_key
            and all([key in kwargs for key in self.options.key_field_names])
        ):
            db_key = self.model_class(**kwargs).db_key

        if db_key and not redis_key:
            redis_key = db_key.redis_key

        if redis_key:
            from ..models.encoding import decode_popoto_model_hashmap

            hashmap = POPOTO_REDIS_DB.hgetall(redis_key)
            if not hashmap:
                return None
            instance = decode_popoto_model_hashmap(self.model_class, hashmap)

        else:
            instances = self.filter(**kwargs)
            if len(instances) > 1:
                raise QueryException(
                    f"{self.model_class.__name__} found more than one unique instance. Use `query.filter()`"
                )
            instance = instances[0] if len(instances) == 1 else None

        # or not hasattr(instance, 'db_key')
        return instance or None

    def keys(self, catchall=False, clean=False, **kwargs) -> list:
        if clean:
            logger.warning(
                "{clean} is for debugging purposes only. Not for use in production environment"
            )
            pipeline = POPOTO_REDIS_DB.pipeline()
            from ..fields.key_field_mixin import KeyFieldMixin
            from ..fields.relationship import Relationship

            for db_key in list(
                POPOTO_REDIS_DB.smembers(
                    self.model_class._meta.db_class_set_key.redis_key
                )
            ):
                hash = POPOTO_REDIS_DB.hgetall(db_key)
                if not len(hash):
                    pipeline = pipeline.srem(
                        self.model_class._meta.db_class_set_key.redis_key, db_key
                    )

            # find
            for field_name, field in self.model_class._meta.fields.items():  # 3
                if not isinstance(field, (KeyFieldMixin, Relationship)):
                    continue
                field_key_prefix = field.get_special_use_field_db_key(
                    self.model_class, field_name
                )
                for field_key in POPOTO_REDIS_DB.keys(f"{field_key_prefix}:*"):
                    for object_key in POPOTO_REDIS_DB.smembers(field_key):
                        hash = POPOTO_REDIS_DB.hgetall(object_key)
                        if not len(hash):
                            pipeline = pipeline.srem(field_key, object_key)

            pipeline.execute()

        if catchall:
            logger.warning(
                "{catchall} is for debugging purposes only. Not for use in production environment"
            )
            return list(POPOTO_REDIS_DB.keys(f"*{self.model_class.__name__}*"))
        else:
            return list(
                POPOTO_REDIS_DB.smembers(
                    self.model_class._meta.db_class_set_key.redis_key
                )
            )

    def all(self, **kwargs) -> list:
        redis_db_keys_list = self.keys()

        # Apply default order_by from Meta if not explicitly provided
        if "order_by" not in kwargs and self.model_class._meta.order_by:
            kwargs["order_by"] = self.model_class._meta.order_by

        return self.prepare_results(
            Query.get_many_objects(
                self.model_class,
                set(redis_db_keys_list),
                order_by_attr_name=kwargs.get("order_by", None),
                values=kwargs.get("values", None),
            ),
            **kwargs,
        )

    def filter_for_keys_set(self, **kwargs) -> set:
        db_keys_sets = []
        yet_employed_kwargs_set = set(kwargs.keys()).difference(
            {"limit", "order_by", "values"}
        )
        if not len(yet_employed_kwargs_set):
            return set()

        # todo: use redis.SINTER for keyfield exact match filters

        # do sorted_fields first - because they can obviate some keyfield filters
        for field_name in self.options.sorted_field_names:
            field = self.options.fields[field_name]
            if not len(
                yet_employed_kwargs_set
                & self.options.filter_query_params_by_field[field_name]
            ):
                continue  # this field cannot use any of the available filter params
            logger.debug(
                f"query on {field_name} with {self.options.filter_query_params_by_field[field_name]}"
            )
            logger.debug(
                {
                    k: kwargs[k]
                    for k in self.options.filter_query_params_by_field[field_name]
                    if k in kwargs
                }
            )
            result = field.__class__.filter_query(self.model_class, field_name, **kwargs)
            # Handle tuple return from GeoField with distances
            if isinstance(result, tuple) and len(result) == 3:
                keys_set, distances, unit = result
                self._geo_distances.update(distances)
                self._geo_distance_unit = unit
                db_keys_sets.append(keys_set)
            else:
                db_keys_sets.append(result)
            yet_employed_kwargs_set = yet_employed_kwargs_set.difference(
                self.options.filter_query_params_by_field[field_name]
            ).difference(
                set(field.sort_by)
            )  # also remove the required sort_by field names

        for field_name in self.options.filter_query_params_by_field:
            if field_name in self.options.sorted_field_names:
                continue  # already handled
            params_for_field = yet_employed_kwargs_set & set(
                self.options.filter_query_params_by_field[field_name]
            )
            if not params_for_field:
                continue  # this field cannot use any of the available filter params

            field = self.options.fields[field_name]
            logger.debug(f"query on {field_name} with {params_for_field}")
            logger.debug({k: kwargs[k] for k in params_for_field})
            result = field.__class__.filter_query(
                self.model_class, field_name, **{k: kwargs[k] for k in params_for_field}
            )
            # Handle tuple return from GeoField with distances
            if isinstance(result, tuple) and len(result) == 3:
                keys_set, distances, unit = result
                self._geo_distances.update(distances)
                self._geo_distance_unit = unit
                db_keys_sets.append(keys_set)
            else:
                db_keys_sets.append(result)
            yet_employed_kwargs_set = yet_employed_kwargs_set.difference(
                params_for_field
            )

        # raise error on additional unknown query parameters
        if yet_employed_kwargs_set:
            raise QueryException(
                f"Invalid filter parameters: {','.join(yet_employed_kwargs_set)}"
            )

        logger.debug(db_keys_sets)
        if not len(db_keys_sets):
            return set()
        # return intersection of all the db keys sets, effectively &&-ing all filters
        return set.intersection(*db_keys_sets)

    def filter(self, **kwargs) -> list:
        """
        Access any and all filters for the fields on the model_class
        Run query using the given paramters
        return a list of model_class objects
        """
        # Reset geo distances for this query
        self._geo_distances = {}
        self._geo_distance_unit = None

        db_keys_set = self.filter_for_keys_set(**kwargs)
        if not len(db_keys_set):
            return []

        # Apply default order_by from Meta if not explicitly provided
        if "order_by" not in kwargs and self.model_class._meta.order_by:
            kwargs["order_by"] = self.model_class._meta.order_by

        objects = Query.get_many_objects(
            self.model_class,
            db_keys_set,
            order_by_attr_name=kwargs.get("order_by", None),
            limit=kwargs.get("limit", None),
            values=kwargs.get("values", None),
        )

        # Attach geo distances to objects if available
        if self._geo_distances:
            # Normalize distance dict keys to strings for consistent lookup
            normalized_distances = {}
            for key, dist in self._geo_distances.items():
                if isinstance(key, bytes):
                    normalized_distances[key.decode()] = dist
                else:
                    normalized_distances[key] = dist

            for obj in objects:
                if isinstance(obj, dict):
                    # When values= is used, obj is a dict - skip distance attachment
                    continue
                redis_key = obj.db_key.redis_key
                if isinstance(redis_key, bytes):
                    redis_key = redis_key.decode()
                distance = normalized_distances.get(redis_key)
                if distance is not None:
                    obj._geo_distance = distance
                    obj._geo_distance_unit = self._geo_distance_unit

            # Sort by distance (ascending) to preserve geo-sorted order
            # Only sort model objects, not dicts
            model_objects = [o for o in objects if not isinstance(o, dict)]
            dict_objects = [o for o in objects if isinstance(o, dict)]
            model_objects.sort(key=lambda o: getattr(o, '_geo_distance', float('inf')))
            objects = model_objects + dict_objects

        return self.prepare_results(objects, **kwargs)

    def prepare_results(
        self,
        objects,
        order_by: str = "",
        values: tuple = (),
        limit: int = None,
        **kwargs,
    ):
        # Apply default order_by from Meta if not explicitly provided
        if not order_by and self.model_class._meta.order_by:
            order_by = self.model_class._meta.order_by

        reverse_order = False
        if order_by and order_by.startswith("-"):
            reverse_order = True
            order_by = order_by[1:]
        if order_by:
            order_by_attr_name = order_by
            if (
                not isinstance(order_by_attr_name, str)
            ) or order_by_attr_name not in self.model_class._meta.fields:
                raise QueryException(
                    f"order_by={order_by_attr_name} must be a field name (str)"
                )
            attr_type = self.model_class._meta.fields[order_by_attr_name].type
            if values and order_by_attr_name not in values:
                raise QueryException(
                    "field must be included in values=(fieldnames) in order to use order_by"
                )
            elif values:
                objects.sort(key=lambda item: item.get(order_by_attr_name))
            else:
                objects.sort(
                    key=lambda item: getattr(item, order_by_attr_name) or attr_type()
                )
            objects = (
                list(reversed(objects))[:limit] if reverse_order else objects[:limit]
            )

        if limit and len(objects) > limit:
            objects = objects[:limit]

        return objects

    def count(self, **kwargs) -> int:
        if not len(kwargs):
            return int(
                POPOTO_REDIS_DB.scard(self.model_class._meta.db_class_set_key.redis_key)
                or 0
            )
        return len(
            self.filter_for_keys_set(**kwargs)
        )  # maybe possible to refactor to use redis.SINTERCARD

    @classmethod
    def get_many_objects(
        cls,
        model: "Model",
        db_keys: set,
        order_by_attr_name: str = None,
        limit: int = None,
        values: tuple = None,
    ) -> list:
        from .encoding import decode_popoto_model_hashmap

        pipeline = POPOTO_REDIS_DB.pipeline()
        reverse_order = False
        # order the hashes list or objects before applying limit
        if order_by_attr_name and order_by_attr_name.startswith("-"):
            order_by_attr_name = order_by_attr_name[1:]
            reverse_order = True

        if order_by_attr_name and order_by_attr_name in model._meta.key_field_names:
            field_position = model._meta.get_db_key_index_position(order_by_attr_name)
            db_keys = list(db_keys)
            db_keys.sort(key=lambda key: key.split(b":")[field_position])
            db_keys = (
                list(reversed(db_keys))[:limit] if reverse_order else db_keys[:limit]
            )

        if values:
            if not isinstance(values, tuple):
                raise QueryException(
                    "values takes a tuple. eg. query.filter(values=('name',))"
                )
            elif set(values).issubset(model._meta.key_field_names):
                db_keys = [DB_key.from_redis_key(db_key) for db_key in db_keys]
                return [
                    {
                        field_name: (
                            model._meta.fields[field_name].type(
                                db_key[
                                    model._meta.get_db_key_index_position(field_name)
                                ]
                            )
                            if db_key[model._meta.get_db_key_index_position(field_name)]
                            else None
                        )
                        for field_name in values
                    }
                    for db_key in db_keys
                ]
            else:
                [pipeline.hmget(db_key, values) for db_key in db_keys]
                value_lists = pipeline.execute()
                hashes_list = [
                    {field_name: result[i] for i, field_name in enumerate(values)}
                    for result in value_lists
                ]

        else:
            [pipeline.hgetall(db_key) for db_key in db_keys]
            hashes_list = pipeline.execute()

        if {} in hashes_list:
            logger.error(
                "one or more redis keys points to missing objects. Debug with Model.query.keys(clean=True)"
            )

        return [
            decode_popoto_model_hashmap(model, redis_hash, fields_only=bool(values))
            for redis_hash in hashes_list
            if redis_hash
        ]

    # Async methods

    async def async_get(
        self, db_key: DB_key = None, redis_key: str = None, **kwargs
    ) -> "Model":
        """Async version of get().

        Retrieves a single model instance from Redis in a thread pool to avoid
        blocking the event loop.

        Args:
            db_key: Optional DB_key object
            redis_key: Optional Redis key string
            **kwargs: Field values to construct query

        Returns:
            Model instance or None if not found
        """
        return await to_thread(self.get, db_key=db_key, redis_key=redis_key, **kwargs)

    async def async_filter(self, **kwargs) -> list:
        """Async version of filter().

        Filters model instances based on field values in a thread pool to avoid
        blocking the event loop.

        Args:
            **kwargs: Filter parameters (field values, limit, order_by, values)

        Returns:
            List of model instances or dicts (if values= specified)
        """
        return await to_thread(self.filter, **kwargs)

    async def async_all(self, **kwargs) -> list:
        """Async version of all().

        Retrieves all model instances in a thread pool to avoid blocking
        the event loop.

        Args:
            **kwargs: Optional order_by and values parameters

        Returns:
            List of all model instances or dicts (if values= specified)
        """
        return await to_thread(self.all, **kwargs)

    async def async_count(self, **kwargs) -> int:
        """Async version of count().

        Counts model instances matching filter criteria in a thread pool to avoid
        blocking the event loop.

        Args:
            **kwargs: Optional filter parameters

        Returns:
            Count of matching instances
        """
        return await to_thread(self.count, **kwargs)

    async def async_keys(self, catchall=False, clean=False, **kwargs) -> list:
        """Async version of keys().

        Retrieves Redis keys for model instances in a thread pool to avoid
        blocking the event loop.

        Args:
            catchall: If True, use KEYS pattern (debug only, not for production)
            clean: If True, clean up orphaned keys (debug only, not for production)
            **kwargs: Additional parameters

        Returns:
            List of Redis keys
        """
        return await to_thread(self.keys, catchall=catchall, clean=clean, **kwargs)
