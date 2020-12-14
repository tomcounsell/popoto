import json
import logging
import time
from datetime import datetime, timedelta
from polygon import RESTClient as PolygonAPI
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.generic import View

from apps.TA import DEFAULT_PRICE_INDEXES
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

        ohlc_timeserieses = {}
        now_timestamp = int(time.time())
        days_range = request.GET.get('periods', '30')

        for index in DEFAULT_PRICE_INDEXES:
            ohlc_timeserieses[index] = PriceStorage.query(
                ticker=ticker_symbol,
                index='close_price',
                publisher='polygon',
                timestamp=now_timestamp,
                periods_range=int(days_range) * 12 * 24  # n=30 days x 24 hours
            )

        # if len(ohlc_timeserieses['close_price']['values_count']) < days_range:  # missing values
        #     refresh_ticker_timeseries(ticker_symbol, now_timestamp, days_range)

        if return_json:
            return JsonResponse(ohlc_timeserieses, safe=False)

        context = {
            "ticker_symbol": ticker_symbol,
            "transaction_currency": transaction_currency,
            "counter_currency": counter_currency,
            "ticker_timeseries": json.dumps(ohlc_timeserieses),
            # "price": price,
            # "volume": volume,
            # "tv_ticker_symbol": ticker_symbol.replace("_", ""),
            # "signals": signals,
        }
        return render(request, 'ticker.html', context)


def refresh_ticker_timeseries(ticker, timestamp, days_range):

    with PolygonAPI(POLYGON_API_KEY) as polygon_client:
        cryptoTicker, multiplier, timespan = f"X:{ticker.replace('_', '')}", 1, 'day'
        to = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d')
        from_ = (datetime.fromtimestamp(timestamp) - timedelta(days=days_range+1)).strftime('%Y-%m-%d')

        endpoint = f"{polygon_client.url}/v2/aggs/ticker/{cryptoTicker}/range/{multiplier}/{timespan}/{from_}/{to}"
        polygon_client_response = polygon_client._session.get(endpoint, params={}, timeout=polygon_client.timeout)
        logging.debug(f"polygon responded with status: {polygon_client_response.status_code}")

    try:
        if polygon_client_response.status_code == 200 and polygon_client_response.json()['results']:
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
    except:
        pass

