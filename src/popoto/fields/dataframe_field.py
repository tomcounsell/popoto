"""
Field type for persisting Pandas DataFrame objects in Redis.

This module provides seamless integration between Pandas DataFrames and Popoto's
Redis-backed storage system. It enables storing, retrieving, and querying tabular
data without the overhead of managing CSV files or separate data stores.

Design Philosophy:
    DataFrames are a fundamental data structure in data science and machine learning
    workflows. Rather than forcing users to serialize DataFrames manually or maintain
    separate file storage, DataFrameField allows DataFrames to live alongside other
    model attributes as first-class citizens.

    The field leverages Pandas' built-in JSON serialization (via DataFrame.to_json())
    which preserves column types, index information, and handles NaN values correctly.
    This approach was chosen over pickle for safety and over CSV for type preservation.

Integration:
    DataFrameField works with Popoto's encoding system (see models/encoding.py) which
    registers a custom encoder/decoder for pd.DataFrame. When a model is saved, the
    DataFrame is converted to JSON, then packed with MessagePack. On retrieval, the
    process reverses automatically.

    The field bypasses the standard Field type validation (VALID_FIELD_TYPES) since
    pd.DataFrame is a complex type not in the base field's allowed types list.

Use Cases:
    - Machine learning: Store training data, predictions, and model evaluation metrics
    - Financial analysis: Persist OHLCV (Open/High/Low/Close/Volume) candlestick data
    - Data pipelines: Cache intermediate computation results in Redis
    - Analytics: Store aggregated reports alongside metadata

Example:
    class MLExperiment(Model):
        name = KeyField()
        training_data = DataFrameField()
        predictions = DataFrameField()
        metrics = DictField()

    # Store experiment data
    experiment = MLExperiment(name="experiment_001")
    experiment.training_data = pd.read_csv("features.csv")
    experiment.predictions = model.predict(X_test)
    experiment.save()

    # Retrieve later
    exp = MLExperiment.query.get(name="experiment_001")
    exp.training_data.describe()  # Full DataFrame functionality preserved

Limitations:
    - Very large DataFrames may hit Redis memory limits or cause performance issues
    - JSON serialization may lose some pandas-specific features (e.g., categorical dtypes)
    - Not suitable for streaming/appending data; consider TimeseriesModel for that use case
"""

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
    A field for storing Pandas DataFrame objects in Redis.

    DataFrameField extends the base Field to handle pd.DataFrame as a native type,
    enabling tabular data to be persisted alongside other model attributes. The
    DataFrame is automatically serialized to JSON on save and deserialized on
    retrieval, preserving column names, data types, and index structure.

    Unlike most Field subclasses that simply set a type (see shortcuts.py),
    DataFrameField requires special handling because pd.DataFrame is not in the
    VALID_FIELD_TYPES set. It overrides field_defaults to ensure proper type
    registration and provides sensible defaults (null=True, empty DataFrame default).

    Requires the 'dataframe' extra: pip install popoto[dataframe]

    Attributes:
        type: Always pd.DataFrame. Cannot be overridden.
        default: An empty DataFrame by default. Prevents None-related errors when
            accessing DataFrame methods on unset fields.
        null: True by default, allowing the field to be omitted. Unlike KeyFields
            which default to null=False, DataFrames are typically optional data.

    Example:
        class DataModel(Model):
            name = KeyField()
            df = DataFrameField()

        # Create and save
        model = DataModel(name="car_prices")
        model.df = pd.DataFrame({"brand": ["Honda", "Toyota"], "price": [22000, 25000]})
        model.save()

        # Query and use
        loaded = DataModel.query.get(name="car_prices")
        assert isinstance(loaded.df, pd.DataFrame)
        print(loaded.df["price"].mean())  # 23500.0

    See Also:
        - models/encoding.py: Contains the TYPE_ENCODER_DECODERS entry for pd.DataFrame
        - finance/models/ohlcv.py: Real-world usage for financial time-series data
    """

    null: bool = False

    # Export/import: the DataFrame is stored as the field's plain value
    # (JSON-serialized via TYPE_ENCODER_DECODERS, captured by to_dict()) with
    # no independent secondary Redis structure, so it round-trips with no
    # carried state.
    roundtrip_policy: str = "rebuild"

    def __init__(self, **kwargs):
        """
        Initialize a DataFrameField with DataFrame-specific defaults.

        Extends the parent Field.__init__ to register pd.DataFrame as the field type
        and set appropriate defaults. The initialization follows a two-phase pattern:
        first calling super().__init__() to set base field defaults, then updating
        with DataFrame-specific settings.

        This design allows kwargs to override any default while ensuring the field
        always uses pd.DataFrame as its type (the type kwarg is effectively ignored
        if passed, as it gets overwritten by dataframefield_defaults).

        Args:
            **kwargs: Field options. Common options include:
                - null (bool): Whether None is allowed. Defaults to True.
                - default (pd.DataFrame): Default value for new instances.
                    Defaults to an empty DataFrame.

        Raises:
            ImportError: If pandas is not installed.

        Note:
            The 'type' parameter, if passed in kwargs, will be ignored. DataFrameField
            always uses pd.DataFrame as its type to maintain type safety.
        """
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
