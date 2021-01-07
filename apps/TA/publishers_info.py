
PUBLISHERS = {
    "stocks": "yahoo",
    "crypto": "polygon",
    "currencies": "yahoo",
    # "other": "yahoo",
}

yahoo_index_dict = {
    'open': "open_price",
    'high': "high_price",
    'low': "low_price",
    'close': "close_price",
    'volume': "close_volume",
}

def get_tradingview_ticker_symbol(asset_symbol, asset_class=None):
    from apps.TA.storages.asset import get_asset_class
    asset_class = asset_class or get_asset_class(asset_symbol)
    if asset_class == 'crypto':
        return f"COINBASE:{asset_symbol}USD"
    elif asset_class == "stocks":
        return f"NASDAQ:{asset_symbol}"
    return ""
