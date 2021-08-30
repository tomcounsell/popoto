import logging

from ..models.key_value import KeyValueModel
from ..models.publisher import PublisherModel
from ..redis_db import POPOTO_REDIS_DB, BEGINNING_OF_TIME
from ..exceptions import ModelException

logger = logging.getLogger(__name__)


class VolumeseriesException(ModelException):
    pass


class VolumeseriesModel(KeyValueModel):
    """
    stores things in a sorted set unique to each ticker and publisher
    ordered by blocks of volume instead of time
    """
    class_describer = "volumeseries"
