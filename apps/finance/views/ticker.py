import json
import time
from datetime import datetime, timedelta
from polygon import RESTClient as PolygonAPI
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.generic import View

from apps.TA.storages.abstract.ticker import TickerStorage
from apps.TA.storages.abstract.ticker_subscriber import get_nearest_1hr_timestamp
from apps.TA.storages.data.price import PriceStorage
from apps.TA.storages.data.pv_history import PriceVolumeHistoryStorage
from apps.TA.storages.data.volume import VolumeStorage
from settings import POLYGON_API_KEY
from settings.redis_db import database


class Ticker(View):
    def dispatch(self, request, ticker_symbol, return_json=False, *args, **kwargs):
        return super(Ticker, self).dispatch(request, ticker_symbol, return_json, *args, **kwargs)

    def get(self, request, ticker_symbol, return_json):
        ticker_symbol = ticker_symbol.upper()

        if ticker_symbol.find("_") < 0:  # underscore not in ticker
            transaction_currency, counter_currency = ticker_symbol, "USDT"
            return redirect('finance:ticker', ticker_symbol=f"{transaction_currency}_{counter_currency}")
        else:
            transaction_currency, counter_currency = ticker_symbol.split("_")

        ticker_timeseries = get_ticker_timeseries(
            ticker=ticker_symbol,
            timestamp=time.time(),
            periods_range=int(request.GET.get('periods', '30')) * 12 * 24
        )

        if return_json:
            return JsonResponse(ticker_timeseries, safe=False)

        context = {
            "ticker_symbol": ticker_symbol,
            "transaction_currency": transaction_currency,
            "counter_currency": counter_currency,
            "ticker_timeseries": json.dumps(ticker_timeseries),
            # "price": price,
            # "volume": volume,
            # "tv_ticker_symbol": ticker_symbol.replace("_", ""),
            # "signals": signals,
        }
        return render(request, 'ticker.html', context)



def get_ticker_timeseries(ticker, timestamp, periods_range):
    ticker_timeseries = PriceStorage.query(
        ticker=ticker,
        publisher='polygon',
        timestamp=int(timestamp),
        periods_range=periods_range
    )

    if ticker_timeseries['values_count'] < ticker_timeseries['periods_range']:  # missing values
        with PolygonAPI(POLYGON_API_KEY) as polygon_client:
            cryptoTicker, multiplier, timespan = f"X:{ticker.replace('_', '')}", 1, 'day'
            to = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d')
            from_ = (datetime.fromtimestamp(timestamp) - timedelta(days=periods_range+1)).strftime('%Y-%m-%d')

            endpoint = f"{polygon_client.url}/v2/aggs/ticker/{cryptoTicker}/range/{multiplier}/{timespan}/{from_}/{to}"
            polygon_client_response = polygon_client._session.get(endpoint, params={}, timeout=polygon_client.timeout)

        pipeline = database.pipeline()
        price_storage = PriceStorage(ticker=ticker, publisher='polygon', timestamp=timestamp)
        volume_storage = VolumeStorage(ticker=ticker, publisher='polygon', timestamp=timestamp)

        for aggregate_result in polygon_client_response.json()['results']:
            price_storage.unix_timestamp = get_nearest_1hr_timestamp(float(aggregate_result['t'])/1000)
            for agg_key, index_name in {
                'o': 'open_price',
                'h': 'high_price',
                'l': 'low_price',
                'c': 'close_price',
            }.items():
                price_storage.index = index_name
                price_storage.value = float(aggregate_result[agg_key])
                pipeline = price_storage.save(pipeline=pipeline)

            volume_storage.unix_timestamp = get_nearest_1hr_timestamp(float(aggregate_result['t']) / 1000)
            volume_storage.index = 'close_volume'
            volume_storage.value = float(aggregate_result['v'])
            pipeline = volume_storage.save(pipeline=pipeline)

        pipeline.execute()

    return ticker_timeseries
