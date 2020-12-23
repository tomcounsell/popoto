from apps.TA import PERIODS_24HR, PRICE_INDEXES, DEFAULT_PRICE_INDEXES, DEFAULT_VOLUME_INDEXES
from apps.TA.storages.data.price import PriceStorage
from apps.TA.storages.data.pv_history import default_indexes
import pandas
import time
from apps.TA.storages.data.volume import VolumeStorage

ASSETS = {
    "stocks": [
        "FB", "GOOG", "AMZN", "MSFT",
    ],
    "crypto": [
        "BTC", "ETH",
    ],
    "currencies": [
        "USD", "EUR", "GBP", "AUD",
    ]
}

yahoo_index_dict = {
    'open': "open_price",
    'high': "high_price",
    'low': "low_price",
    'close': "close_price",
    'volume': "close_volume",
}


class AssetException(Exception):
    pass


class Asset:
    def __init__(self, symbol, asset_class=None):
        self.symbol = symbol
        self.dataframe = pandas.DataFrame()
        self.asset_class = asset_class or "other"

        if asset_class and asset_class not in ASSETS.keys():
            pass

        elif not asset_class:
            for asset_class_key in ASSETS.keys():
                if symbol in ASSETS[asset_class_key]:
                    asset_class = asset_class_key
                    self.asset_class = asset_class
                    break
            if not asset_class:
                raise AssetException("symbol not found in any asset class. specify an alt asset class")

        elif asset_class in ASSETS.keys():
            if symbol not in ASSETS[asset_class]:
                raise AssetException("symbol not found in this asset class")

    def update_dataframe(self, start_timestamp=None, end_timestamp=None):
        from settings.redis_db import database
        pipeline = database.pipeline()
        end_timestamp = end_timestamp or int(time.time())
        start_timestamp = start_timestamp or (end_timestamp - (30*24*3600))

        if self.asset_class == "stocks":
            from yahoo_fin.stock_info import get_data
            self.yahoo_data = get_data(
                self.symbol,
                start_date=pandas.Timestamp.utcfromtimestamp(start_timestamp),
                end_date=pandas.Timestamp.utcfromtimestamp(end_timestamp)
            )
            for index in set(self.yahoo_data.keys()).intersection({'open', 'high', 'low', 'close'}):
                for timestamp, value in self.yahoo_data[index].items():
                    price_storage = PriceStorage(
                        ticker=f"{self.symbol}_USD",
                        publisher="yahoo",
                        index=yahoo_index_dict[index],
                        timestamp=int(timestamp.timestamp()),
                        value=float(value)
                    )
                    price_storage.save(pipeline=pipeline)
            for index in set(self.yahoo_data.keys()).intersection({'volume'}):
                for timestamp, value in self.yahoo_data[index].items():
                    volume_storage = VolumeStorage(
                        ticker=f"{self.symbol}_USD",
                        publisher="yahoo",
                        index=yahoo_index_dict[index],
                        timestamp=int(timestamp.timestamp()),
                        value=float(value)
                    )
                    volume_storage.save(pipeline=pipeline)

        pipeline.execute()
        return

    def get_candle_dataframe(self, timestamp: int = 0, days_range: int = 1):
        self.dataframe = pandas.DataFrame(data={'scores': []})
        timestamp = timestamp or int(time.time())

        for index in DEFAULT_PRICE_INDEXES:
            prices_timeseries = PriceStorage.query(
                ticker=f"{self.symbol}_USD",
                index=index,
                publisher="yahoo",
                timestamp=timestamp,
                periods_range=float(days_range) * PERIODS_24HR * 1.1  # n days x 24 hours * 110%
            )
            if 'scores' in prices_timeseries:
                self.dataframe = pandas.merge(
                    self.dataframe,
                    pandas.DataFrame(
                        data={
                            'scores': prices_timeseries['scores'],
                            index: prices_timeseries['values']
                        },
                        dtype=float
                    ),
                    on='scores',
                    how='outer'
                )

        for index in DEFAULT_VOLUME_INDEXES:
            volume_timeseries = VolumeStorage.query(
                ticker=f"{self.symbol}_USD",
                index="close_volume",
                publisher="yahoo",
                timestamp=timestamp,
                periods_range=float(days_range) * PERIODS_24HR * 1.1  # n days x 24 hours * 110%
            )
            if 'scores' in volume_timeseries:
                self.dataframe = pandas.merge(
                    self.dataframe,
                    pandas.DataFrame(
                        data={
                            'scores': volume_timeseries['scores'],
                            index: volume_timeseries['values']
                        },
                        dtype=float
                    ),
                    on='scores',
                    how='outer'
                )
        self.dataframe = self.dataframe.astype({'scores': 'int64'})
        self.dataframe.set_index('scores', inplace=True, verify_integrity=True)
        return self.dataframe
