from django.db import models
from django.db.models import Sum
from apps.common.behaviors import Timestampable


class Allocation(Timestampable, models.Model):

    portfolio = models.ForeignKey("Portfolio", null=False, unique=False, on_delete=models.CASCADE, related_name='allocations')
    asset = models.ForeignKey("Asset", null=False, on_delete=models.PROTECT)
    quantity_offline = models.FloatField(default=0.00001)
    user_votes = models.IntegerField(default=1)
    system_votes = models.IntegerField(default=1)

    @property
    def user_weight(self):
        total_user_votes = self.portfolio.allocations.aggregate(Sum('user_votes'))['user_votes__sum']
        return self.user_votes / total_user_votes if total_user_votes > 0 else 0

    @property
    def system_weight(self):
        total_system_votes = self.portfolio.allocations.aggregate(Sum('system_votes'))['system_votes__sum']
        return self.system_votes / total_system_votes if total_system_votes > 0 else 0

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
