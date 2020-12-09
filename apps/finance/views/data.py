import time

from django.http import JsonResponse
from django.views.generic import View
from requests import Response
from rest_framework import status

from apps.TA.storages.abstract.ticker import TickerStorage


class Ticker(View):
    def dispatch(self, request, ticker_symbol, *args, **kwargs):
        ticker_symbol = ticker_symbol.upper()
        if ticker_symbol.find("_") < 0:
            return Response(status=status.HTTP_406_NOT_ACCEPTABLE)
        return super(Ticker, self).dispatch(request, ticker_symbol, *args, **kwargs)

    def get(self, request, ticker_symbol):
        # transaction_currency, counter_currency = ticker_symbol.split("_")
        ticker_timeseries = TickerStorage.query(ticker=ticker_symbol, timestamp=time.time(), periods_range=30)

        if len(ticker_timeseries['values']) < 30:
            # new thread
            # polygon get data
            pass

        return JsonResponse(ticker_timeseries)
