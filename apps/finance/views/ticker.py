import time
from django.shortcuts import render, redirect
from django.views.generic import View

from apps.TA.storages.abstract.ticker import TickerStorage


class Ticker(View):
    def dispatch(self, request, ticker_symbol, *args, **kwargs):
        return super(Ticker, self).dispatch(request, ticker_symbol, *args, **kwargs)

    def get(self, request, ticker_symbol):
        ticker_symbol = ticker_symbol.upper()

        if ticker_symbol.find("_") < 0:  # underscore not in ticker
            transaction_currency, counter_currency = ticker_symbol, "USDT"
            return redirect('finance:ticker', ticker_symbol=f"{transaction_currency}_{counter_currency}")
        else:
            transaction_currency, counter_currency = ticker_symbol.split("_")

        ticker_timeseries = TickerStorage.query(ticker=ticker_symbol, timestamp=time.time(), periods_range=30)

        context = {
            "ticker_symbol": ticker_symbol,
            "transaction_currency": transaction_currency,
            "counter_currency": counter_currency,
            # "ticker_timeseries": ticker_timeseries,
            # "price": price,
            # "volume": volume,
            # "tv_ticker_symbol": ticker_symbol.replace("_", ""),
            # "signals": signals,
        }
        return render(request, 'ticker.html', context)
