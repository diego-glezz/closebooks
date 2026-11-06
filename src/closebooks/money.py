"""Money helpers. All amounts are Decimal, never float.

Parsing accepts the formats that show up in Spanish/European and US exports:
"1.234,56", "1,234.56", "1234.56", "-1.234,56 €", "(1,234.56)", 1234.56 (float/int).
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
DEFAULT_TOLERANCE = Decimal("0.01")

_CURRENCY_RE = re.compile(r"[€$£\s]|EUR|USD|GBP", re.IGNORECASE)


def to_decimal(value: object) -> Decimal:
    """Parse a monetary value into Decimal. Raises ValueError if it cannot be parsed."""
    if value is None:
        raise ValueError("amount is None")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("bool is not an amount")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if value != value:  # NaN
            raise ValueError("amount is NaN")
        return Decimal(repr(value))

    text = _CURRENCY_RE.sub("", str(value)).strip()
    if not text:
        raise ValueError(f"empty amount: {value!r}")

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    if text.endswith("-"):
        negative, text = True, text[:-1]
    if text.startswith("-"):
        negative, text = not negative, text[1:]
    if text.startswith("+"):
        text = text[1:]

    text = _normalize_separators(text)
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"cannot parse amount: {value!r}") from exc
    return -result if negative else result


def _normalize_separators(text: str) -> str:
    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        # The right-most separator is the decimal one.
        if text.rfind(",") > text.rfind("."):
            return text.replace(".", "").replace(",", ".")
        return text.replace(",", "")
    if has_comma:
        # "1,5" / "1234,56" -> decimal comma; "1,234,567" -> thousands
        parts = text.split(",")
        if len(parts) == 2 and len(parts[1]) != 3:
            return text.replace(",", ".")
        if len(parts) > 2:
            return text.replace(",", "")
        # Ambiguous "1,234": assume thousands separator only if left part has 1-3 digits
        return text.replace(",", "") if len(parts[0]) <= 3 else text.replace(",", ".")
    if has_dot:
        parts = text.split(".")
        if len(parts) > 2:  # "1.234.567" -> thousands
            return text.replace(".", "")
    return text


def round2(amount: Decimal) -> Decimal:
    """Round to cents, half-up (the accounting convention, not banker's rounding)."""
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def approx_equal(a: Decimal, b: Decimal, tolerance: Decimal = DEFAULT_TOLERANCE) -> bool:
    return abs(a - b) <= tolerance


def is_round_amount(amount: Decimal, multiple: int = 100) -> bool:
    """True for amounts like 5000.00 or 12300.00 (a classic weak fraud signal)."""
    return amount != 0 and amount == amount.to_integral_value() and int(amount) % multiple == 0
