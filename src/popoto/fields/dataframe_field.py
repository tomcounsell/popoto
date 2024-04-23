import logging
import pandas as pd
from .field import Field

logger = logging.getLogger("POPOTO.DataFrame")


class DataFrameField(Field):
    """
    A field that stores Pandas DataFrame objects
    required: pandas.DataFrame object
    """

    type: type = pd.DataFrame
    default: pd.DataFrame = pd.DataFrame()
    null: bool = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        dataframefield_defaults = {
            "type": pd.DataFrame,
            "null": True,
            "default": pd.DataFrame(),
        }
        self.field_defaults.update(dataframefield_defaults)
        # set field options, let kwargs override
        for k, v in dataframefield_defaults.items():
            setattr(self, k, kwargs.get(k, v))
