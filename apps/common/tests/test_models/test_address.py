from django.test import TestCase

from ..test_behaviors import TimestampableTest
from apps.common.models import Address


class AddressTest(TimestampableTest, TestCase):
    model = Address
