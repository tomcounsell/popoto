import time
from abc import ABC
import pandas as pd
from ...exceptions import FinanceException
from ...models import TimeseriesModel
from ...finance import PriceStorage, VolumeStorage

# get_nearest_1day_score
# PERIODS_24HR, DEFAULT_PRICE_INDEXES, DEFAULT_VOLUME_INDEXES


class OLHCVException(FinanceException):
    pass


class OLHCVModel(ABC):  # candles data for an asset-symbol
    def __init__(self, symbol: str, publisher: str = "other", timestamp: int = 0, days_range: int = 1, period="daily"):
        self.symbol = symbol
        self.dataframe = pd.DataFrame()
        self.publisher = publisher
        self.timestamp = timestamp or int(time.time())
        self.period = period
        self.days_range = days_range


    def get_dataframe(self, timestamp: int = 0, days_range: int = 0):
        self.timestamp = timestamp or self.timestamp
        self.days_range = days_range or self.days_range

        dataframes = {}
        low_score = high_score = TimeseriesModel.score_from_timestamp(self.timestamp)

        for index in reversed(DEFAULT_PRICE_INDEXES):
            price_timeseries = PriceStorage.query(
                ticker=f"{self.symbol}_USD",
                index=index,
                publisher=self.publisher,
                timestamp=self.timestamp,
                periods_range=float(self.days_range) * PERIODS_24HR * 1.1  # n days x 24 hours * 110%
            )
            if 'scores' not in price_timeseries or int(price_timeseries['values_count']) < 1:
                continue

            dataframes[index] = pd.DataFrame(
                index=[get_nearest_1day_score(score) for score in price_timeseries['scores']],
                data={index: price_timeseries['values'],},
                dtype=float
            )
            low_score = min([low_score, price_timeseries['scores'][0]])
            high_score = max([high_score, price_timeseries['scores'][-1]])

        for index in DEFAULT_VOLUME_INDEXES:
            volume_timeseries = VolumeStorage.query(
                ticker=f"{self.symbol}_USD",
                index="close_volume",
                publisher=self.publisher,
                timestamp=self.timestamp,
                timestamp_tolerance=12 * 12,  # 12 hrs
                periods_range=float(self.days_range) * PERIODS_24HR * 1.1  # n days x 24 hours * 110%
            )
            if 'scores' in volume_timeseries:
                dataframes[index] = pd.DataFrame(
                    index=[get_nearest_1day_score(score) for score in volume_timeseries['scores']],
                    data={index: volume_timeseries['values'],},
                    dtype=float
                )
                low_score = min([low_score, volume_timeseries['scores'][0]])
                high_score = max([high_score, volume_timeseries['scores'][-1]])


        # new empty dataframe with every daily score included in the index
        self.dataframe = pd.DataFrame(
            index=range(get_nearest_1day_score(low_score),
                        get_nearest_1day_score(high_score),
                        PERIODS_24HR)
        )

        # merge all dataframes
        for index in reversed(DEFAULT_PRICE_INDEXES):  # close_price goes first
            self.dataframe = pd.merge(
                self.dataframe, dataframes[index],
                left_index=True, right_index=True,
                how="outer"
            )
        for index in DEFAULT_VOLUME_INDEXES:
            self.dataframe = pd.merge(
                self.dataframe, dataframes[index],
                left_index=True, right_index=True,
                how="outer"
            )

        # fill-forward missing close_prices (includes weekends for stocks)
        self.dataframe['close_price'].fillna(method="ffill", axis=0, inplace=True)
        for index in reversed(DEFAULT_PRICE_INDEXES):  # close_price goes first
            # fill all other missing prices with close price
            self.dataframe[index].fillna(self.dataframe['close_price'], axis=0, inplace=True)

        # fill missing volumes with 0
        for index in DEFAULT_VOLUME_INDEXES:
            self.dataframe[index].fillna(0, axis=0, inplace=True)

        # self.dataframe = self.dataframe.astype({'score': 'int64'})
        self.dataframe.index.name = "score"
        self.dataframe = self.dataframe.reset_index().drop_duplicates(subset='score', keep='last').set_index('score').sort_index()
        return self.dataframe

