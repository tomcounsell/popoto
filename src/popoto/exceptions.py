class ModelException(Exception):
    """Raised when a model operation fails (validation, save, delete, or load)."""

    pass


class FinanceException(Exception):
    """Raised for finance-related operation errors."""

    pass
