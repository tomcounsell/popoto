from django.urls import path
from django.views.generic import TemplateView

from apps.finance.views import ticker, data

app_name = "finance"

urlpatterns = [

    # THINGS THAT NEED A UI
    path('ticker/<str:ticker_symbol>', ticker.Ticker.as_view(), name='ticker'),

    path('data/<str:ticker_symbol>.json', data.Ticker.as_view(), name='data'),

]
