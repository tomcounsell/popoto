from apps.TA import PERIODS_24HR, DEFAULT_PRICE_INDEXES, DEFAULT_VOLUME_INDEXES
from apps.TA.storages.data.price import PriceStorage
import pandas
import time
from apps.TA.storages.data.volume import VolumeStorage

ASSETS = {
    "stocks": [
        "FB", "GOOG", "AMZN", "MSFT", "ABNB", "RIOT", "UFO",
    ],
    "crypto": [
        "BTC", "ETH", "LTC",
    ],
    "currencies": [
        "USD", "EUR", "GBP", "AUD", "THB", "CZK",
    ]
}

def get_asset_class(symbol):
    asset_class = "other"
    for asset_class_key in ASSETS.keys():
        if symbol in ASSETS[asset_class_key]:
            return asset_class_key
    return asset_class

PUBLISHERS = {
    "stocks": "yahoo",
    "crypto": "polygon",
    "currencies": "yahoo",
    # "other": "yahoo",
}

yahoo_index_dict = {
    'open': "open_price",
    'high': "high_price",
    'low': "low_price",
    'close': "close_price",
    'volume': "close_volume",
}


def get_tradingview_ticker_symbol(asset_symbol, asset_class=None):
    asset_class = asset_class or get_asset_class(asset_symbol)
    if asset_class == 'crypto':
        return f"COINBASE:{asset_symbol}USD"
    elif asset_class == "stocks":
        return f"NASDAQ:{asset_symbol}"
    return ""


class MarketException(Exception):
    pass


class MarketData:  # candles data for an asset-symbol
    def __init__(self, symbol: str, asset_class: str = None, timestamp: int = 0, days_range: int = 1):
        self.symbol = symbol
        self.dataframe = pandas.DataFrame()
        self.asset_class = asset_class or get_asset_class(symbol)
        self.timestamp = timestamp or int(time.time())
        self.days_range = days_range

        if asset_class in ASSETS.keys():
            if symbol not in ASSETS[asset_class]:
                raise MarketException("symbol not found in this asset class")


    def update_dataframe(self, start_timestamp=None, end_timestamp=None):
        from settings.redis_db import database
        pipeline = database.pipeline()
        end_timestamp = end_timestamp or self.timestamp
        one_day_seconds = 24 * 3600
        start_timestamp = start_timestamp or end_timestamp - (self.days_range * one_day_seconds)
        start_timestamp -= one_day_seconds

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
        return self.get_candle_dataframe()

    def get_candle_dataframe(self, timestamp: int = 0, days_range: int = 0):
        self.dataframe = pandas.DataFrame(data={'score': []})
        self.timestamp = timestamp or self.timestamp
        self.days_range = days_range or self.days_range

        for index in DEFAULT_PRICE_INDEXES:
            prices_timeseries = PriceStorage.query(
                ticker=f"{self.symbol}_USD",
                index=index,
                publisher=PUBLISHERS.get(self.asset_class, "yahoo"),
                timestamp=self.timestamp,
                periods_range=float(self.days_range) * PERIODS_24HR * 1.1  # n days x 24 hours * 110%
            )
            if 'scores' in prices_timeseries:
                self.dataframe = pandas.merge(
                    self.dataframe,
                    pandas.DataFrame(
                        data={
                            'score': prices_timeseries['scores'],
                            index: prices_timeseries['values']
                        },
                        dtype=float
                    ),
                    on='score',
                    how='outer'
                )

        for index in DEFAULT_VOLUME_INDEXES:
            volume_timeseries = VolumeStorage.query(
                ticker=f"{self.symbol}_USD",
                index="close_volume",
                publisher=PUBLISHERS.get(self.asset_class, "yahoo"),
                timestamp=self.timestamp,
                timestamp_tolerance=12 * 12,  # 12 hrs
                periods_range=float(self.days_range) * PERIODS_24HR * 1.1  # n days x 24 hours * 110%
            )
            if 'scores' in volume_timeseries:
                self.dataframe = pandas.merge(
                    self.dataframe,
                    pandas.DataFrame(
                        data={
                            'score': volume_timeseries['scores'],
                            index: volume_timeseries['values']
                        },
                        dtype=float
                    ),
                    on='score',
                    how='outer'
                )
        self.dataframe = self.dataframe.astype({'score': 'int64'})
        self.dataframe.set_index('score', inplace=True, verify_integrity=False)
        return self.dataframe
