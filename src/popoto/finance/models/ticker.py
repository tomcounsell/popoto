import logging
from src.popoto.exceptions import FinanceException
from src.popoto._archive.timeseries import TimeseriesModel

logger = logging.getLogger(__name__)


class TickerException(FinanceException):
    pass


class TickerStorage(TimeseriesModel):
    """
    stores timeseries data on tickers in a sorted set unique to each ticker and publisher (data source)
    todo: split the db by each publisher source
    """
    class_describer = "ticker"
    value_sig_figs = 6

    def __init__(self, *args, **kwargs):
        """
        'ticker' REQUIRED
        'publisher EXPECTED BUT CAN STILL SAVE WITHOUT
        """
        super().__init__(*args, **kwargs)
        try:
            self.ticker = str(kwargs['ticker'])  # str eg. BTC_USD
            self.publisher = str(kwargs['publisher'])  # str eg. binance
        except KeyError:
            raise Exception("Indicator requires a ticker and publisher as parameters")
        except Exception as e:
            raise Exception(str(e))

        if self.ticker.find("_") <= 0:
            raise Exception("ticker should be like GOOG_USD or LTC_BTC")

        self._db_key_prefix = f'{self.ticker}:{self.publisher}:'
        self._db_key = self.build_db_key()


    @classmethod
    def query(cls, *args, **kwargs):

        ticker = kwargs.get("ticker", None)
        if not ticker:
            raise TickerException("ticker required for ticker query")
        publisher = kwargs.get("publisher", "")
        kwargs["key_prefix"] = f'{ticker}:{publisher}'

        results_dict = super().query(*args, **kwargs)
        if results_dict:
            results_dict['publisher'] = publisher
            results_dict['ticker'] = ticker
        return results_dict

    def save(self, publish=False, pipeline=None, *args, **kwargs):
        self.value = '{:g}'.format(float('{:.{p}g}'.format(self.value, p=self.value_sig_figs)))
        return super().save(publish=publish, pipeline=pipeline, *args, **kwargs)
