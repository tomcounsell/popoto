from django.test import TestCase

from ..test_behaviors import TimestampableTest
from apps.common.models import Upload


class UploadTest(TimestampableTest, TestCase):
    model = Upload
