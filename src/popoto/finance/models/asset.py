import logging
from datetime import datetime

from src.popoto.finance import PriceStorage, VolumeStorage, OLHCVModel

# import pandas
# from polygon import RESTClient as PolygonAPI
# from settings import POLYGON_API_KEY
# get_nearest_1hr_timestamp
# PUBLISHERS, yahoo_index_dict

logger = logging.getLogger(__name__)


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


class AssetException(Exception):
    pass


class AssetModel(OLHCVModel):

    def __init__(self, symbol: str, asset_class: str = None, timestamp: int = 0, days_range: int = 1, period="daily"):
        if asset_class in ASSETS.keys():
            if symbol not in ASSETS[asset_class]:
                raise AssetException("symbol not found in this asset class")

        self.asset_class = asset_class or get_asset_class(symbol)
        super().__init__(symbol=symbol, publisher=PUBLISHERS.get(self.asset_class, "yahoo"),
                         timestamp=timestamp, days_range=days_range, period=period)


    @classmethod
    def query(cls, symbol, publisher="", *args, **kwargs):
        from apps.portfolio.models import Asset
        assets = Asset.objects.filter(name__icontains=symbol)
        asset = assets.filter(name__icontains=publisher).first() if publisher else assets.first()
        if asset:
            return AssetModel(
                symbol=asset.symbol, asset_class=asset.asset_class, days_range=90
            ).get_dataframe()


    def renew(self, start_timestamp=None, end_timestamp=None):  # get up-to-date with newest data
        from ...redis_db import POPOTO_REDIS_DB
        end_timestamp = end_timestamp or self.timestamp
        one_day_seconds = 24 * 3600
        start_timestamp = start_timestamp or end_timestamp - (self.days_range * one_day_seconds)
        start_timestamp -= one_day_seconds
        throttle_timeout = 20


        if POPOTO_REDIS_DB.ttl(f"throttle:{PUBLISHERS[self.asset_class]}") > 0:
            return
        POPOTO_REDIS_DB.set(f"throttle:{PUBLISHERS[self.asset_class]}", "slow up bro", ex=throttle_timeout) #set throttle

        pipeline = POPOTO_REDIS_DB.pipeline()

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
        self.dataframe = self.get_dataframe()
        return
