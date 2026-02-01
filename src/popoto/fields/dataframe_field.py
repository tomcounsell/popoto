import logging
from .field import Field

try:
    import pandas as pd

    _pandas_available = True
except ImportError:
    _pandas_available = False

logger = logging.getLogger("POPOTO.DataFrame")


class DataFrameField(Field):
    """
    A field that stores Pandas DataFrame objects
    required: pandas.DataFrame object

    Requires the 'dataframe' extra: pip install popoto[dataframe]
    """

    null: bool = False

    def __init__(self, **kwargs):
        if not _pandas_available:
            raise ImportError(
                "pandas is required to use DataFrameField. "
                "Install it with: pip install popoto[dataframe]"
            )
        super().__init__(**kwargs)
        self.type = pd.DataFrame
        dataframefield_defaults = {
            "type": pd.DataFrame,
            "null": True,
            "default": pd.DataFrame(),
        }
        self.field_defaults.update(dataframefield_defaults)
        # set field options, let kwargs override
        for k, v in dataframefield_defaults.items():
            setattr(self, k, kwargs.get(k, v))
