import logging

from .key_value import KeyValueStorage
from ..redis_db import POPOTO_REDIS_DB, BEGINNING_OF_TIME
from ..models import ModelException

logger = logging.getLogger(__name__)


class VolumeseriesException(ModelException):
    pass


class VolumeseriesStorage(KeyValueStorage):
    """
    stores things in a sorted set unique to each ticker and publisher
    ordered by blocks of volume instead of time
    """
    class_describer = "volumeseries"
