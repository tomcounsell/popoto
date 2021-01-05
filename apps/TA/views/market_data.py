import logging
import time
from datetime import datetime

import pandas
from polygon import RESTClient as PolygonAPI

from apps.TA import PERIODS_24HR, DEFAULT_PRICE_INDEXES, DEFAULT_VOLUME_INDEXES
from apps.TA.storages.abstract.ticker_subscriber import get_nearest_1hr_timestamp, get_nearest_1day_timestamp, \
    get_nearest_1day_score
from apps.TA.storages.abstract.timeseries_storage import TimeseriesStorage
from apps.TA.storages.data.price import PriceStorage
from apps.TA.storages.data.volume import VolumeStorage
from settings import POLYGON_API_KEY

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
    def __init__(self, symbol: str, asset_class: str = None, timestamp: int = 0, days_range: int = 1, period="daily"):
        self.symbol = symbol
        self.dataframe = pandas.DataFrame()
        self.asset_class = asset_class or get_asset_class(symbol)
        self.timestamp = timestamp or int(time.time())
        self.period = period
        self.days_range = days_range

        if asset_class in ASSETS.keys():
            if symbol not in ASSETS[asset_class]:
                raise MarketException("symbol not found in this asset class")


    def update_dataframe(self, start_timestamp=None, end_timestamp=None):
        from settings.redis_db import database
        end_timestamp = end_timestamp or self.timestamp
        one_day_seconds = 24 * 3600
        start_timestamp = start_timestamp or end_timestamp - (self.days_range * one_day_seconds)
        start_timestamp -= one_day_seconds
        throttle_timeout = 20


        if database.ttl(f"throttle:{PUBLISHERS[self.asset_class]}") > 0:
            return
        database.set(f"throttle:{PUBLISHERS[self.asset_class]}", "slow up bro", ex=throttle_timeout) #set throttle

        pipeline = database.pipeline()

        if PUBLISHERS[self.asset_class] == "polygon":
            with PolygonAPI(POLYGON_API_KEY) as polygon_client:
                cryptoTicker, multiplier, timespan = f"X:{self.symbol}USD", 1, 'day'
                to = datetime.fromtimestamp(end_timestamp).strftime('%Y-%m-%d')
                from_ = datetime.fromtimestamp(start_timestamp).strftime('%Y-%m-%d')

                endpoint = f"{polygon_client.url}/v2/aggs/ticker/{cryptoTicker}/range/{multiplier}/{timespan}/{from_}/{to}"
                polygon_client_response = polygon_client._session.get(endpoint, params={},
                                                                      timeout=polygon_client.timeout)
                logging.debug(f"polygon responded with status: {polygon_client_response.status_code}")

            if polygon_client_response.status_code == 200 and polygon_client_response.json()['results']:

                for aggregate_result in polygon_client_response.json()['results']:
                    for agg_key, index_name in {
                        'o': 'open_price',
                        'h': 'high_price',
                        'l': 'low_price',
                        'c': 'close_price',
                    }.items():
                        price_storage = PriceStorage(
                            ticker=f"{self.symbol}_USD",
                            publisher="polygon",
                            index=index_name,
                            timestamp=get_nearest_1hr_timestamp(float(aggregate_result['t'])/1000),
                            value=float(aggregate_result[agg_key])
                        )
                        pipeline = price_storage.save(pipeline=pipeline)

                    volume_storage = VolumeStorage(
                        ticker=f"{self.symbol}_USD",
                        publisher='polygon',
                        index='close_volume',
                        timestamp=get_nearest_1hr_timestamp(float(aggregate_result['t']) / 1000),
                        value = float(aggregate_result['v'])
                    )
                    pipeline = volume_storage.save(pipeline=pipeline)



        if PUBLISHERS[self.asset_class] == "yahoo":
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
        self.dataframe = self.get_candle_dataframe()
        return

    def get_candle_dataframe(self, timestamp: int = 0, days_range: int = 0):
        self.timestamp = timestamp or self.timestamp
        self.days_range = days_range or self.days_range

        dataframes = {}
        low_score = high_score = TimeseriesStorage.score_from_timestamp(self.timestamp)

        for index in reversed(DEFAULT_PRICE_INDEXES):
            price_timeseries = PriceStorage.query(
                ticker=f"{self.symbol}_USD",
                index=index,
                publisher=PUBLISHERS.get(self.asset_class, "yahoo"),
                timestamp=self.timestamp,
                periods_range=float(self.days_range) * PERIODS_24HR * 1.1  # n days x 24 hours * 110%
            )
            if 'scores' not in price_timeseries or int(price_timeseries['values_count']) < 1:
                continue

            dataframes[index] = pandas.DataFrame(
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
                publisher=PUBLISHERS.get(self.asset_class, "yahoo"),
                timestamp=self.timestamp,
                timestamp_tolerance=12 * 12,  # 12 hrs
                periods_range=float(self.days_range) * PERIODS_24HR * 1.1  # n days x 24 hours * 110%
            )
            if 'scores' in volume_timeseries:
                dataframes[index] = pandas.DataFrame(
                    index=[get_nearest_1day_score(score) for score in volume_timeseries['scores']],
                    data={index: volume_timeseries['values'],},
                    dtype=float
                )
                low_score = min([low_score, volume_timeseries['scores'][0]])
                high_score = max([high_score, volume_timeseries['scores'][-1]])


        # new empty dataframe with every daily score included in the index
        self.dataframe = pandas.DataFrame(
            index=range(get_nearest_1day_score(low_score),
                        get_nearest_1day_score(high_score),
                        PERIODS_24HR)
        )

        # merge all dataframes
        for index in reversed(DEFAULT_PRICE_INDEXES):  # close_price goes first
            self.dataframe = pandas.merge(
                self.dataframe, dataframes[index],
                left_index=True, right_index=True,
                how="outer"
            )
        for index in DEFAULT_VOLUME_INDEXES:
            self.dataframe = pandas.merge(
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
