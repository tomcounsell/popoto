from decimal import Decimal
import os
import sys
from datetime import datetime, timedelta
from src import popoto
from src.popoto.redis_db import POPOTO_REDIS_DB

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))


class AssetPrice(popoto.Model):
    _uid = popoto.UniqueKeyField(type=str, null=False)
    asset_id = popoto.KeyField(type=str, null=False)
    price = popoto.Field(type=Decimal)
    timestamp = popoto.SortedField(type=datetime, null=False, sort_by="asset_id")

    def pre_save(self, **kwargs):
        self._uid = f"{self.asset_id}{self.timestamp.timestamp()}"
        return super().pre_save(**kwargs)


price_history = {
    datetime(2021, 1, 1): {
        'BTC': Decimal("29374.15"),
        'ETH': Decimal("730.37"),
    },
    datetime(2021, 1, 2): {
        'BTC': Decimal("32127.27"),
        'ETH': Decimal("774.53"),
    },
    datetime(2021, 1, 3): {
        'BTC': Decimal("32782.02"),
        'ETH': Decimal("975.51"),
    }
}

for timestamp in price_history.keys():
    for asset_id in price_history[timestamp].keys():
        AssetPrice.create(
            asset_id=asset_id,
            price=price_history[timestamp][asset_id],
            timestamp=timestamp
        )

query_results = AssetPrice.query.filter(timestamp__gte=datetime(2021, 1, 1), asset_id="BTC")
assert len(query_results) == 3

# there should be 2 sortedsets based on the asset partition
assert len([key for key in AssetPrice.query.keys(True) if "SortF" in key.decode()]) == 2

for item in AssetPrice.query.all():
    item.delete()

# make sure even the special purpose keys were also deleted
assert len(AssetPrice.query.keys(True)) == 0
