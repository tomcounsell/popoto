from django.test import TestCase

from ..test_behaviors import TimestampableTest
from apps.common.models import Currency


class CurrencyTest(TimestampableTest, TestCase):
    model = Currency
