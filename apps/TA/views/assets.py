
ASSETS = {
    "stocks": [
        "FB", "GOOG", "AMZN", "MSFT",
    ],
    "crypto": [
        "BTC", "ETH",
    ],
    "currencies": [
        "USD", "EUR", "GBP", "AUD",
    ]
}

class AssetException(Exception):
    pass

class Asset:
    def __init__(self, symbol, asset_class=None):
        self.symbol = symbol
        self.asset_class = asset_class or "other"

        if asset_class and asset_class not in ASSETS.keys():
            pass

        elif not asset_class:
            for asset_class_key in ASSETS.keys():
                if symbol in ASSETS[asset_class]:
                    asset_class = asset_class_key
                    self.asset_class = asset_class
                    break
            if not asset_class:
                raise AssetException("symbol not found in any asset class. specify an alt asset class")

        elif asset_class in ASSETS.keys():
            if symbol not in ASSETS[asset_class]:
                raise AssetException("symbol not found in this asset class")


    def get_data(self):
        from yahoo_fin.stock_info import get_data
        if self.asset_class == "stocks":
            data = get_data(self.symbol)
