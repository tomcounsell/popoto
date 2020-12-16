from django.urls import path

from apps.portfolio.views import portfolio

app_name = "portfolio"

urlpatterns = [

    path('portfolio', portfolio.Portfolio.as_view(), name='portfolio'),

]
