from django.urls import path

from apps.TA.views import market

app_name = "TA"

urlpatterns = [

    # THINGS THAT NEED A UI
    path('<str:ticker_symbol>.json', market.Market.as_view(), kwargs={'return_json': True}, name='market.json'),
    path('<str:ticker_symbol>.csv', market.Market.as_view(), kwargs={'return_csv': True}, name='market.csv'),
    path('<str:ticker_symbol>', market.Market.as_view(), name='market'),

]
