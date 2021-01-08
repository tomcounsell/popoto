from django.urls import path

from apps.portfolio.views import portfolio, asset

app_name = "portfolio"

urlpatterns = [

    path('portfolio', portfolio.PortfolioView.as_view(), name='portfolio'),
    path('asset/create', asset.AssetView.as_view(), name='create_asset'),
    path('asset/<str:asset_symbol>', asset.AssetView.as_view(), name='asset'),

]
