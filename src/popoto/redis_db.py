import os
import logging
import redis
# from redisgraph import Graph

logger = logging.getLogger('POPOTO-REDIS_DB')

global POPOTO_REDIS_DB
# global REDIS_GRAPH
BEGINNING_OF_TIME = 0
ENCODING = 'utf-8'

try:
    BEGINNING_OF_TIME = int(os.environ.get("BEGINNING_OF_TIME", 0))
except ValueError:
    logger.critical("BEGINNING_OF_TIME is set but should be an integer in unix time seconds where 0 equals 1970-01-01")
except Exception as e:
    logger.debug(e)

try:
    REDIS_URL = os.environ.get("REDIS_URL", "")
    if REDIS_URL:
        POPOTO_REDIS_DB = redis.from_url(REDIS_URL)
        logger.debug("Redis connection established.")
    else:
        REDIS_HOST, REDIS_PORT = "127.0.0.1:6379".split(":")
        pool = redis.ConnectionPool(host=REDIS_HOST, port=REDIS_PORT, db=0)
        POPOTO_REDIS_DB = redis.Redis(connection_pool=pool)
        # REDIS_GRAPH = Graph('social', POPOTO_REDIS_DB)

except Exception as e:
    logger.info(str(e))


def set_REDIS_DB_settings(env_partition_name: str = "", *args, **kwargs):
    # todo: use this to mark keys in redis db, so they can be separated and deleted
    env_partition_name = env_partition_name or os.environ.get('ENV', "")

    global POPOTO_REDIS_DB
    POPOTO_REDIS_DB = redis.Redis(*args, **kwargs)
    # global REDIS_GRAPH
    # REDIS_GRAPH = Graph('social', POPOTO_REDIS_DB)
    logger.debug("Redis connection reset.")


def get_REDIS_DB():
    return POPOTO_REDIS_DB


def print_redis_info():
    logger.info(POPOTO_REDIS_DB.info())

    used_memory, maxmemory = int(POPOTO_REDIS_DB.info()['used_memory']), int(POPOTO_REDIS_DB.info()['maxmemory'])
    maxmemory_human = POPOTO_REDIS_DB.info()['maxmemory_human']
    if maxmemory and maxmemory > 0:
        logger.info(f"Redis currently consumes {round(100 * used_memory / maxmemory, 2)}% out of {maxmemory_human}")


class PopotoException(Exception):
    def __init__(self, message):
        self.message = message
        logger.error(message)
