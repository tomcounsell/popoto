from django.db import models
from settings import AUTH_USER_MODEL


class BinanceAccount(models.Model):

    user = models.ForeignKey(AUTH_USER_MODEL, null=True, related_name="accounts", on_delete=models.SET_NULL)
    name = models.CharField(max_length=100, blank=True)
    api_key = models.CharField(max_length=128, blank=True)

