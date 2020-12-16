from django.urls import path

from apps.TA.views import ticker

app_name = "TA"

urlpatterns = [

    # THINGS THAT NEED A UI
    path('ticker/<str:ticker_symbol>.json', ticker.Ticker.as_view(), kwargs={'return_json': True}, name='ticker.json'),
    path('ticker/<str:ticker_symbol>', ticker.Ticker.as_view(), name='ticker'),

]
