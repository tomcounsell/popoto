from django.urls import path

from apps.portfolio.views import portfolio, asset

app_name = "portfolio"

urlpatterns = [

    path('portfolio', portfolio.PortfolioView.as_view(), name='portfolio'),
    path('assets/create', asset.AssetView.as_view(), name='create_asset'),
    # path('assets/<str:asset_symbol>', asset.AssetView.as_view(), name='edit_asset'),
    # path('assets', asset.AssetsView.as_view(), name='assets'),

]
