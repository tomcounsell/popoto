import logging
from src.popoto.exceptions import FinanceException
from ...models import TimeseriesStorage

logger = logging.getLogger(__name__)


class TickerException(FinanceException):
    pass


class TickerStorage(TimeseriesStorage):
    """
    stores timeseries data on tickers in a sorted set unique to each ticker and publisher (data source)
    todo: split the db by each publisher source
    """
    class_describer = "ticker"
    value_sig_figs = 6

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # 'ticker' REQUIRED
        # 'publisher EXPECTED BUT CAN STILL SAVE WITHOUT
        try:
            self.ticker = str(kwargs['ticker'])  # str eg. BTC_USD
            self.publisher = str(kwargs['publisher'])  # str eg. binance
        except KeyError:
            raise Exception("Indicator requires a ticker and publisher as parameters")
        except Exception as e:
            raise Exception(str(e))
        else:
            if self.ticker.find("_") <= 0:
                raise Exception("ticker should be like GOOG_USD or LTC_BTC")


    def get_db_key(self):
        self.db_key_prefix = f'{self.ticker}:{self.publisher}:'
        # by default will return "{ticker}:{publisher}:{class_name}"
        return super().get_db_key()


    @classmethod
    def query(cls, *args, **kwargs):

        ticker = kwargs.get("ticker", None)
        if not ticker:
            raise IndicatorException("ticker required for ticker query")
        publisher = kwargs.get("publisher", "CMC")
        kwargs["key_prefix"] = f'{ticker}:{publisher}'

        results_dict = super().query(*args, **kwargs)
        if results_dict:
            results_dict['publisher'] = publisher
            results_dict['ticker'] = ticker
        return results_dict

    def save(self, publish=False, pipeline=None, *args, **kwargs):
        self.value = '{:g}'.format(float('{:.{p}g}'.format(self.value, p=self.value_sig_figs)))
        return super().save(publish=publish, pipeline=pipeline, *args, **kwargs)
