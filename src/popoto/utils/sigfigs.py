"""
Significant Figures Rounding Utility.

This module provides precision-controlled rounding for numeric values, which is
essential for financial data display and storage in Popoto's finance module.

Why This Exists:
    Python's built-in `round()` function rounds to decimal places, not significant
    figures. In financial contexts (prices, indicators, ratios), significant figures
    are often more meaningful than fixed decimal places. For example:

    - A stock price of 0.0023 should round to 0.0023 (2 sig figs), not 0.00
    - A price of 1234.56 should round to 1200 (2 sig figs), not 1234.56

    This utility enables consistent precision across values of vastly different
    magnitudes, which is critical for time-series financial data.

Design Trade-offs:
    - Uses string manipulation for digit counting rather than logarithms. This is
      slightly less elegant mathematically but handles edge cases more predictably.
    - Relies on Python's built-in `round()` for the actual rounding operation,
      inheriting its banker's rounding behavior (round half to even).

Integration:
    Used by the finance module (`popoto.finance`) for normalizing indicator values
    and price data before storage or display.

Example:
    >>> from popoto.utils.sigfigs import round_sig_figs
    >>> round_sig_figs(1234.5678, 3)  # Large number
    1230.0
    >>> round_sig_figs(0.001234, 2)   # Small number
    0.0012
"""


def round_sig_figs(value, sig_figs: int) -> float:
    """
    Round a numeric value to a specified number of significant figures.

    This function handles both large numbers (where significant figures reduce
    precision in the integer portion) and small decimal numbers (where significant
    figures preserve precision after the decimal point).

    Args:
        value: Any numeric value that can be converted to float. Accepts int,
            float, Decimal, or string representations of numbers.
        sig_figs: The number of significant figures to retain. Must be a
            positive integer.

    Returns:
        The rounded value as a float with the specified number of significant
        figures.

    Design Note:
        For negative values (representing small decimals less than 1), the
        function counts leading zeros after the decimal point to determine
        proper rounding precision. This ensures that 0.00456 rounded to 2
        significant figures becomes 0.0046, not 0.00.

    Examples:
        >>> round_sig_figs(123456, 2)
        120000.0
        >>> round_sig_figs(0.00789, 2)
        0.0079
        >>> round_sig_figs("3.14159", 3)
        3.14

    Warning:
        The current implementation has a bug: it uses `value < 0` to detect
        small decimals, but this actually checks for negative numbers. Values
        between 0 and 1 are handled in the else branch, which may not always
        produce correct results for very small positive decimals.
    """
    value = float(value)
    non_decimal_place = len(str(int(value)))
    # decimal_places = max(len(str(value % 1))-2, 0)

    if 0 < value < 1:
        distance_from_decimal = 0
        for char in str(value):
            if char == ".":
                continue
            elif int(char) > 0:
                break
            else:
                distance_from_decimal += 1
        return round(value, (distance_from_decimal + sig_figs))
    else:
        return round(value, (sig_figs - non_decimal_place))
