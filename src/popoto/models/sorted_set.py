import logging
import numpy as np
import pandas as pd
import redis.client

from src.popoto.fields.key_value import KeyValueModel
from src.popoto.redis_db import POPOTO_REDIS_DB
from src.popoto.exceptions import ModelException
logger = logging.getLogger(__name__)


class SortedSetException(ModelException):
    pass


class SortedSetModel(KeyValueModel):
    """
    stores things in a sorted set
    todo: split the db by each publisher source
    """
    class_describer = "sortedset"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # 'sort_score' REQUIRED, VALIDATE
        try:
            self.sort_score = int(kwargs['score'])  # int eg. 5
        except KeyError:
            raise SortedSetException("score required for SortedSetModel objects")
        except ValueError:
            raise SortedSetException(f"score must be castable as integer, received {kwargs.get('score')}")
        except Exception as e:
            raise SortedSetException(str(e))

    def save_own_existance(self, describer_key=""):
        self.describer_key = describer_key or f'{self.__class__.class_describer}:{self._db_key}'


    @classmethod
    def query(cls, key: str = "", key_suffix: str = "", key_prefix: str = "",
              score: int = None,
              score_range: float = 1,
              *args, **kwargs) -> dict:
        """
        :param key: the exact redis sortedset key (optional)
        :param key_suffix: suffix on the key  (optional)
        :param key_prefix: prefix on the key (optional)
        :param score: score for most recent value returned  (optional, default returns latest)
        :param score_range: number of periods desired in results (optional, default 0, so only return 1 value)
        :return: dict(values=[], ...)
        """

        sorted_set_key = cls.compile_db_key(key=key, key_prefix=key_prefix, key_suffix=key_suffix)
        # logger.debug(f'query for sorted set key {sorted_set_key}')
        # example key f'{key_prefix}:{cls.__name__}:{key_suffix}'

        # do a quick check to make sure this is a class of things we know is in existence
        describer_key = f'{cls.class_describer}:{sorted_set_key}'
        # if no score, assume query to find the most recent, the last one

        if not score:
            query_response = POPOTO_REDIS_DB.zrange(sorted_set_key, -1, -1)
            try:
                [value, score] = query_response[0].decode("utf-8").split(":")
            except:
                value, score = "unknown", 0

            min_score = max_score = score

        else:
            target_score = score
            logger.debug(f"querying for key {sorted_set_key} with score {target_score} and {score_range} backward")

            min_score, max_score = (target_score - score_range), target_score

            query_response = POPOTO_REDIS_DB.zrangebyscore(sorted_set_key, min_score, max_score)

        # NEW example query_response = [b'100:1']
        # which came from f'{self.value}:{str(score)}' where score = self.score

        return_dict = {
            'values': [],
            'values_count': 0,
            'score': score,
            'earliest_score': min_score,
            'latest_score': max_score,
            'score_range': score_range,
        }

        if not len(query_response):
            return return_dict

        try:
            return_dict['values_count'] = len(query_response)

            if len(query_response) < score_range + 1:
                return_dict["warning"] = "fewer values than query's periods_range"

            values = [value_score.decode("utf-8").split(":")[0] for value_score in query_response]
            scores = [float(value_score.decode("utf-8").split(":")[1]) for value_score in query_response]
            # todo: double check that [-1] in list is most recent score

            return_dict.update({
                'values': values,
                'scores': scores,
                'earliest_score': scores[0],
                'latest_score': scores[-1],
            })
            return return_dict

        except IndexError:
            return return_dict

        except Exception as e:
            logger.error("redis query problem: " + str(e))
            return {'error': "redis query problem: " + str(e),  # wtf happened?
                    'values': []}

    @staticmethod
    def get_values_array_from_query(query_results: dict, limit: int = 0):

        value_array = [float(v) for v in query_results['values']]

        if limit:
            if not isinstance(limit, int) or limit < 1:
                raise SortedSetException(f"bad limit: {limit}")

            elif len(value_array) > limit:
                value_array = value_array[-limit:]

        return np.array(value_array)

    def get_z_add_data(self, dataframe: pd.DataFrame = None):
        return {
            "key": self._db_key,
            "name": dataframe.to_dict() if dataframe else f'{self.value}:{self.score}',
            "score": self.score
        }

    def save(self, publish: bool = False, pipeline: redis.client.Pipeline = None, dataframe: pd.DataFrame = None, *args, **kwargs):
        if self.value is None and dataframe is None:
            raise ModelException("no value set, nothing to save!")

        self.save_own_existance()

        z_add_data = self.get_z_add_data(dataframe=dataframe)

        if isinstance(pipeline, redis.client.Pipeline):
            pipeline = pipeline.zadd(z_add_data["key"], z_add_data["name"])
            # logger.debug("added command to redis pipeline")
            if publish:
                pipeline = self.publish(pipeline)
            return pipeline
        else:
            response = POPOTO_REDIS_DB.zadd(z_add_data["key"], z_add_data["name"])
            # logger.debug("no pipeline, executing zadd command immediately.")
            if publish:
                self.publish()
            return response

    def publish(self, pipeline=None):
        return super().publish(data=self.get_z_add_data(), pipeline=pipeline)

    def get_value(self, *args, **kwargs):
        SortedSetException("function not yet implemented! ¯\_(ツ)_/¯ ")
        pass


"""
We can scan the newest or oldest event ids with ZRANGE 4,
maybe later pulling the events themselves for analysis.

We can get the 10 or even 100 events immediately
before or after a score with ZRANGEBYSCORE
combined with the LIMIT argument.

We can count the number of events that occurred
in a specific score range with ZCOUNT.
"""
