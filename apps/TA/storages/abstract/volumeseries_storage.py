import logging
from apps.TA import TAException
from apps.TA import KeyValueStorage

logger = logging.getLogger(__name__)


class StorageException(TAException):
    pass

class VolumeseriesException(TAException):
    pass


class VolumeseriesStorage(KeyValueStorage):
    """
    stores things in a sorted set unique to each ticker and publisher
    ordered by blocks of volume as opposed to time
    """
    class_describer = "volumeseries"
