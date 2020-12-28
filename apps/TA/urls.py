from django.urls import path

from apps.TA.views import market

app_name = "TA"

urlpatterns = [

    # THINGS THAT NEED A UI
    path('market/<str:ticker_symbol>.json', market.Market.as_view(), kwargs={'return_json': True}, name='ticker.json'),
    path('market/<str:ticker_symbol>', market.Market.as_view(), name='market'),

]
