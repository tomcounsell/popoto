import logging

from src.popoto.exceptions import FinanceException
from ...finance import TickerStorage, PriceVolumeHistoryStorage, TickerSubscriber
# score_is_near_5min, clear_pv_history_values, PRICE_INDEXES, generate_pv_storages


logger = logging.getLogger(__name__)


class PriceException(FinanceException):
    pass


class PriceStorage(TickerStorage):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.index = kwargs.get('index', "close_price")
        self.value = kwargs.get('value')
        self.db_key_suffix = f':{self.index}'

    def save(self, *args, **kwargs):

        # meets basic requirements for saving
        if not all([self.ticker, self.publisher,
                    self.index, self.value,
                    self.unix_timestamp]):
            logger.error("incomplete information, cannot save \n" + str(self.__dict__))
            raise PriceException("save error, missing data")

        if not self.force_save:
            if self.index not in PRICE_INDEXES:
                logger.error("price index not in approved list, raising exception...")
                raise PriceException("unknown index")

        if self.unix_timestamp % 3600 != 0:
            raise PriceException("price timestamp should be % 3600")

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


class PriceSubscriber(TickerSubscriber):
    classes_subscribing_to = [
        PriceVolumeHistoryStorage
    ]

    def handle(self, channel, data, *args, **kwargs):
        # parse data like...
        # {
        #     "key": "POLY_BTC:binance:PriceVolumeHistoryStorage:open_price",
        #     "name": "3665:176255.873",
        #     "score": "176255.873"
        # }

        # eg. sorted_set_key = data["key"]

        [ticker, publisher, object_class, index] = data["key"].split(":")
        if not object_class == channel == PriceVolumeHistoryStorage.__name__:
            logger.warning(f'Unexpected that these are not the same:'
                           f'object_class: {object_class}, '
                           f'channel: {channel}, '
                           f'subscribing class: {PriceVolumeHistoryStorage.__name__}')
        [value, name_score] = data["name"].split(":")

        score = float(data["score"])

        if not float(name_score) == float(data["score"]):
            logger.warning(f'Unexpected that score in name {name_score}'
                           f'is different than score {score}')

        if score_is_near_5min(score):
            if generate_pv_storages(ticker, publisher, index, score):
                if index == "close_price":
                    clear_pv_history_values(ticker, publisher, score)
