from django.db import models
from apps.common.behaviors import Timestampable


class Allocation(Timestampable, models.Model):

    portfolio = models.ForeignKey("Portfolio", null=False, unique=False, on_delete=models.CASCADE)
    asset = models.ForeignKey("Asset", null=False, on_delete=models.PROTECT)
    user_weight = models.FloatField(default=0.00001)
    system_weight = models.FloatField(default=0.00001)

    @property
    def proportion(self):
        if self.asset.latest_value > 0 and self.portfolio.latest_value > 0:
            return self.asset.latest_value / self.portfolio.latest_value
        return None  # unknown

    @property
    def desired_proportion(self):
        proportion = self.user_weight
        proportion += self.system_weight * self.portfolio.system_weight
        proportion /= 1 + self.portfolio.system_weight
        return proportion

    class Meta:
        unique_together = ('portfolio', 'asset')
