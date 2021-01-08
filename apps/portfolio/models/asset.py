from django.db import models

from apps.TA.publishers_info import PUBLISHERS
from apps.common.behaviors import Timestampable


class Asset(Timestampable, models.Model):

    name = models.CharField(max_length=100, blank=True)
    symbol = models.CharField(max_length=100, blank=True)
    alternative_symbols = models.JSONField(default=list)

    ASSET_CLASS_CHOICES = [(asset_class_name, asset_class_name) for asset_class_name in [
        "other", "crypto", "stocks",
    ]]
    asset_class = models.CharField(choices=ASSET_CLASS_CHOICES, max_length=15, default="other")

    # todo: refactor publishers to be independent of asset classes
    # PUBLISHER_CHOICES = [(publisher_name, publisher_name) for publisher_name in set(PUBLISHERS.values())]
    # publisher = models.CharField(max_length=100, choices=PUBLISHER_CHOICES)

    EXCHANGE_CHOICES = [(publisher_name, publisher_name) for publisher_name in ["Coinbase", ]]
    exchange = models.CharField(max_length=15, blank=True)

    @property
    def publisher(self):
        return PUBLISHERS.get(self.asset_class, "yahoo")

    @property
    def asset_storage(self):
        from apps.TA.storages.asset import AssetStorage
        return AssetStorage(symbol=self.symbol, asset_class=self.asset_class)

    @property
    def latest_value(self):
        return 10