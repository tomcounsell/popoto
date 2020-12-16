import logging
from datetime import datetime, timedelta
import json
import time
from django.shortcuts import render, redirect
from django.views.generic import View

from apps.TA.storages.abstract.timeseries_storage import TimeseriesStorage
from apps.TA.storages.data.portfolio import PortfolioStorage
from apps.TA.storages.data.price import PriceStorage
from settings.redis_db import database


class Portfolio(View):
    def dispatch(self, request, *args, **kwargs):
        return super(Portfolio, self).dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):

        now_timestamp = int(time.time())
        days_range = int(request.GET.get('days', '30'))
        price_timeseries = PortfolioStorage.query(
            id=request.user.username,
            timestamp=now_timestamp,
            timestamp_tolerance=12,  # 1 hr
            periods_range=float(days_range) * 12 * 24 * 1.1  # n days x 24 hours * 110%
        )

        context = {
            "price_timeseries": json.dumps(price_timeseries)
        }
        return render(request, 'portfolio.html', context)


    def post(self, request, *args, **kwargs):
        assets = {
            "crypto": {
                "BTC": 10,
                "ETH": 3,
            },
        }

        today = datetime.today()
        today = datetime(today.year, today.month, today.day)
        one_month_ago = today - timedelta(days=31)

        portfolio_storage = PortfolioStorage(id=request.user.username, timestamp=int(today.timestamp()))

        # delete portfolio timeseries
        for db_key in database.keys(f"*{portfolio_storage.db_key}*"):
            database.zremrangebyscore(db_key, 0, TimeseriesStorage.score_from_timestamp(int(time.time())))

        # repopulate portfolio timeseries
        for timestamp in range(int(one_month_ago.timestamp()), int(today.timestamp()), 3600 * 24):  # daily
            capital = 0

            crypto_assets = assets['crypto']
            for asset in crypto_assets.keys():
                price_timeseries = PriceStorage.query(
                    ticker=f"{asset}_USD", publisher='polygon', timestamp=timestamp, timestamp_tolerance=3600*12
                )
                # logging.debug(price_timeseries['values'])
                if price_timeseries['values_count'] == 0:
                    continue

                capital += float(crypto_assets[asset]) * float(price_timeseries['values'][0])

            if capital > 0:
                portfolio_storage.unix_timestamp = timestamp
                portfolio_storage.value = capital
                portfolio_storage.save()

        return redirect('finance:portfolio')
