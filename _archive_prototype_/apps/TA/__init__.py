import os
import logging

# from settings import SHORT, MEDIUM, LONG
# PERIODS_1HR, PERIODS_4HR, PERIODS_24HR = SHORT/5, MEDIUM/5, LONG/5

JAN_1_2017_TIMESTAMP = int(1483228800)
HORIZONS = [PERIODS_1HR, PERIODS_4HR, PERIODS_24HR] = [12, 48, 288]  # num of 5 min samples

DEFAULT_PRICE_INDEXES = ["open_price", "high_price", "low_price", "close_price", ]
DERIVED_PRICE_INDEXES = ["midpoint_price", "mean_price", "price_variance", ]
PRICE_INDEXES = DEFAULT_PRICE_INDEXES + DERIVED_PRICE_INDEXES
DEFAULT_VOLUME_INDEXES = ["close_volume", ]
DERIVED_VOLUME_INDEXES = ["open_volume", "high_volume", "low_volume", ]
VOLUME_INDEXES = DEFAULT_VOLUME_INDEXES + DERIVED_VOLUME_INDEXES
ALL_INDEXES = PRICE_INDEXES + VOLUME_INDEXES

SMA_LIST = [20, 50, 200]

deployment_type = os.environ.get('DEPLOYMENT_TYPE', 'LOCAL')
if deployment_type == 'LOCAL':
    logging.basicConfig(level=logging.DEBUG)

logger = logging.getLogger('core.apps.TA')


class TAException(Exception):
    def __init__(self, message):
        self.message = message
        logger.error(message)

class SuchWowException(Exception):
    def __init__(self, message):
        self.message = message
        such_wow = "==============SUCH=====WOW==============="
        logger.error(f'\n\n{such_wow}\n\n{message}\n\n{such_wow}')
