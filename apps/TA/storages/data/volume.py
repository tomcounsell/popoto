import logging

from apps.TA import TAException, VOLUME_INDEXES
from apps.TA.storages.abstract.ticker import TickerStorage
from apps.TA.storages.abstract.ticker_subscriber import TickerSubscriber, timestamp_is_near_5min, \
    get_nearest_5min_timestamp
from apps.TA.storages.data.pv_history import PriceVolumeHistoryStorage, default_volume_indexes, derived_volume_indexes

logger = logging.getLogger(__name__)


class VolumeException(TAException):
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



class VolumeSubscriber(TickerSubscriber):

    classes_subscribing_to = [
        PriceVolumeHistoryStorage
    ]

    def handle(self, channel, data, *args, **kwargs):

        # parse timestamp from data
        # f'{data_history.ticker}:{data_history.publisher}:{data_history.timestamp}'
        [ticker, publisher, timestamp] = data.split(":")

        if not timestamp_is_near_5min(timestamp):
            return

        # logger.debug("near to a 5 min time marker")
        timestamp = get_nearest_5min_timestamp(timestamp)

        volume = VolumeStorage(ticker=ticker, publisher=publisher, timestamp=timestamp)
        index_values = {}

        for index in default_volume_indexes:
            logger.debug(f'process volume for ticker: {ticker}')

            # example key = "XPM_BTC:poloniex:PriceVolumeHistoryStorage:close_price"
            sorted_set_key = f'{ticker}:{publisher}:PriceVolumeHistoryStorage:{index}'

            index_values[index] = [
                float(db_value.decode("utf-8").split(":")[0])
                for db_value
                in self.database.zrangebyscore(sorted_set_key, timestamp - 300, timestamp + 45) # todo: update to scores
            ]

            try:
                if not len(index_values[index]):
                    volume.value = None

                elif index == "close_volume":
                    volume.value = index_values["close_volume"][-1]


            except IndexError:
                pass  # couldn't find a useful value
            except ValueError:
                pass  # couldn't find a useful value
            else:
                if volume.value:
                    volume.index = index
                    volume.save()
                    # logger.info("saved new thing: " + volume.get_db_key())

            # all_values_set = (
            #         set(index_values["open_volume"])
            # )
            #
            # if not len(all_values_set):
            #     return
            #
            # for index in derived_volume_indexes:
            #     volume.value = None
            #     values_set = all_values_set.copy()
            #
            #     if index == "open_volume":
            #         volume.value = index_values["open_volume"][0]
            #
            #     elif index == "low_volume":
            #         volume.value = min(index_values["low_volume"])
            #     elif index == "high_volume":
            #         volume.value = max(index_values["high_volume"])
            #
            #     if volume.value:
            #         volume.index = index
            #         volume.save(publish=True)
