from settings import LOAD_TALIB

if LOAD_TALIB:
    import talib

from apps.TA import HORIZONS
from apps.TA.storages.abstract.indicator import IndicatorStorage
from apps.TA.storages.abstract.indicator_subscriber import IndicatorSubscriber
from apps.TA.storages.data.price import PriceStorage
from settings import logger


class AdxStorage(IndicatorStorage):
    def produce_signal(self):
        pass


class AdxSubscriber(IndicatorSubscriber):
    classes_subscribing_to = [PriceStorage]

    def handle(self, channel, data, *args, **kwargs):

        self.index = self.key_suffix

        if str(self.index) is not "close_price":
            logger.debug(f"index {self.index} is not close_price ...ignoring...")
            return

        new_adx_storage = AdxStorage(
            ticker=self.ticker, exchange=self.exchange, timestamp=self.timestamp
        )

        for horizon in HORIZONS:
            periods = horizon * 14

            high_value_np_array = new_adx_storage.get_denoted_price_array(
                "high_price", periods
            )
            low_value_np_array = new_adx_storage.get_denoted_price_array(
                "low_price", periods
            )
            close_value_np_array = new_adx_storage.get_denoted_price_array(
                "close_price", periods
            )

            timeperiod = min(
                [
                    len(high_value_np_array),
                    len(low_value_np_array),
                    len(close_value_np_array),
                    periods,
                ]
            )
            adx_value = talib.ADX(
                high_value_np_array,
                low_value_np_array,
                close_value_np_array,
                timeperiod=timeperiod,
            )[-1]
            # logger.debug(f'savingAdx value {adx_value} for {self.ticker} on {periods} periods')

            new_adx_storage.periods = periods
            new_adx_storage.value = float(adx_value)
            new_adx_storage.save()
