from django.db import models
from apps.common.behaviors import Timestampable
from settings import AUTH_USER_MODEL


class Portfolio(Timestampable, models.Model):
    user = models.ForeignKey(AUTH_USER_MODEL, related_name="portfolios", on_delete=models.PROTECT)
    name = models.CharField(max_length=100, default="")
    system_weight = models.FloatField(default=0)

    @property
    def latest_value(self):
        return 100
