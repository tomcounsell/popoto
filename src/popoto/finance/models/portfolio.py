import logging

from src.popoto._archive.timeseries import TimeseriesException, TimeseriesModel

logger = logging.getLogger(__name__)


class PortfolioException(TimeseriesException):
    pass


class PortfolioStorage(TimeseriesModel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.id = str(kwargs.get('id'))
        self.value = kwargs.get('value')
        self.db_key_suffix = f':{self.id}'

    def save(self, *args, **kwargs):

        # meets basic requirements for saving
        if not all([self.id, self.value is not None, self.unix_timestamp]):
            logger.error("incomplete information, cannot save \n" + str(self.__dict__))
            raise PortfolioException("save error, missing data")

        if self.unix_timestamp % 3600 != 0:  # on the hour
            raise PortfolioException("price timestamp should be % 3600")

        return super().save(*args, **kwargs)

    @classmethod
    def query(cls, *args, **kwargs):

        key_suffix = kwargs.get("key_suffix", "")
        id = kwargs.get("id", "close_price")
        kwargs["key_suffix"] = f'{id}' + (f':{key_suffix}' if key_suffix else "")

        results_dict = super().query(*args, **kwargs)
        results_dict['id'] = id
        return results_dict
