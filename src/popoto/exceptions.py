"""
Custom exceptions for the Popoto Redis ORM library.

This module defines the exception hierarchy for Popoto, providing distinct base
exceptions for different subsystems. The design follows a deliberate separation
of concerns: core ORM errors are kept separate from domain-specific errors
(like finance) to allow consumers to catch and handle them independently.

Design Philosophy:
    Rather than using a single base exception, Popoto uses separate exception
    hierarchies for each major subsystem. This allows applications to:

    1. Catch all ORM-related errors with `except ModelException`
    2. Catch all finance-related errors with `except FinanceException`
    3. Let unexpected errors propagate naturally

    This separation is intentional: a data validation error in a Model field
    is fundamentally different from an error processing financial time-series
    data, and calling code may want to handle them very differently.

Example:
    Catching model validation errors during save operations::

        from popoto.exceptions import ModelException

        try:
            my_model.save()
        except ModelException as e:
            logger.error(f"Model validation failed: {e}")
            # Handle validation error (e.g., return 400 to client)

    Catching finance-specific errors::

        from popoto.exceptions import FinanceException

        try:
            price_data.compute_indicator()
        except FinanceException as e:
            logger.warning(f"Finance calculation failed: {e}")
            # Handle gracefully (e.g., skip this ticker)
"""


class ModelException(Exception):
    """
    Base exception for all Popoto ORM model-related errors.

    ModelException is raised when there's a problem with model definition,
    field configuration, data validation, or persistence operations. It serves
    as the catch-all for errors in the core ORM functionality.

    Common scenarios where ModelException is raised:

    - **Field naming violations**: Field names must start with lowercase letters
      and cannot use reserved names like 'limit', 'order_by', or 'values'.

    - **Invalid field types**: Using a type not supported by KeyField or Field.

    - **Relationship errors**: Passing an incorrect type to a Relationship field.

    - **Duplicate fields**: Attempting to add a field name that already exists.

    - **Public attribute violations**: Public model attributes must be Field
      instances; use underscore prefix for non-field attributes.

    - **Validation failures**: Required fields missing values, constraint
      violations, or data type mismatches during save operations.

    - **TTL/expiration conflicts**: Setting both `_ttl` and `_expire_at` on
      a model instance (only one is allowed).

    Example:
        Handling a field naming error::

            from popoto import Model, Field
            from popoto.exceptions import ModelException

            try:
                class BadModel(Model):
                    Capitalized = Field(type=str)  # Will raise
            except ModelException as e:
                print(f"Invalid field definition: {e}")

        Handling a save validation error::

            try:
                instance = MyModel()
                instance.required_field = None
                instance.save()
            except ModelException as e:
                print(f"Save failed: {e}")

    Note:
        When `Model.save(raise_exceptions=False)` is called, validation errors
        are logged instead of raising ModelException, and the method returns
        False. This is useful for batch operations where you want to continue
        processing despite individual failures.
    """

    pass


class FinanceException(Exception):
    """
    Base exception for all finance module errors.

    FinanceException serves as the parent class for domain-specific exceptions
    in Popoto's finance subsystem. This includes errors related to price data,
    volume calculations, OHLCV processing, technical indicators, and real-time
    ticker subscriptions.

    The finance module defines several specialized subclasses:

    - **PriceException**: Errors in price data processing
    - **VolumeException**: Errors in volume data handling
    - **TickerException**: Errors with ticker symbol operations
    - **OLHCVException**: Errors in Open/Low/High/Close/Volume processing
    - **IndicatorException**: Errors computing technical indicators
    - **SubscriberException**: Errors in real-time data subscription

    By catching FinanceException, you can handle any finance-related error
    without needing to know the specific subclass. This is useful when
    processing multiple data streams where individual failures shouldn't
    halt the entire pipeline.

    Example:
        Processing multiple tickers with graceful error handling::

            from popoto.exceptions import FinanceException

            for ticker in tickers:
                try:
                    process_ticker_data(ticker)
                except FinanceException as e:
                    logger.warning(f"Skipping {ticker}: {e}")
                    continue

    Note:
        FinanceException is intentionally separate from ModelException.
        Finance errors often relate to data quality or external data sources,
        while ModelException relates to ORM configuration and validation.
        Applications may want to retry finance errors (e.g., refetch data)
        but fail fast on model errors (which indicate code bugs).
    """

    pass
