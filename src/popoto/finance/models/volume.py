import logging
from src.popoto.exceptions import FinanceException
from ...finance import TickerStorage

#  timestamp_is_near_5min, get_nearest_5min_timestamp
# default_volume_indexes, derived_volume_indexes
# VOLUME_INDEXES

logger = logging.getLogger(__name__)


class VolumeException(FinanceException):
    pass


class VolumeStorage(TickerStorage):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.index = kwargs.get('index', "close_volume")
        self.value = kwargs.get('value')
        self.db_key_suffix = f':{self.index}'

    def save(self, *args, **kwargs):

        # meets basic requirements for saving
        if not all([self.ticker, self.publisher,
                   self.index, self.value,
                   self.unix_timestamp]):
            logger.error("incomplete information, cannot save \n" + str(self.__dict__))
            raise VolumeException("save error, missing data")

        if not self.force_save:
            if not self.index in VOLUME_INDEXES:
                logger.error("volume index not in approved list, raising exception...")
                raise VolumeException("unknown index")

        if self.unix_timestamp % 3600 != 0:
            raise VolumeException("price timestamp should be % 3600")

        self.db_key_suffix = self.db_key_suffix or f':{self.index}'
        return super().save(*args, **kwargs)

    @classmethod
    def query(cls, *args, **kwargs):

        if kwargs.get("periods_key", None):
            raise PriceException("periods_key is not usable in PriceStorage query")

        key_suffix = kwargs.get("key_suffix", "")
        index = kwargs.get("index", "close_price")
        kwargs["key_suffix"] = f'{index}' + (f':{key_suffix}' if key_suffix else "")

        results_dict = super().query(*args, **kwargs)

        results_dict['index'] = index
        return results_dict
