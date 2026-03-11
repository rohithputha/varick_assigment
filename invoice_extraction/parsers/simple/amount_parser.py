"""Pure Python amount/currency parsing. No LLM, no side effects, deterministic."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from models import ParsedField


# ---------------------------------------------------------------------------
# Regexes
# ---------------------------------------------------------------------------

# Handles: $24,000  |  USD 5400  |  24,000.00  |  (250.00) for negatives
# Strips currency symbols: $, £, €, ¥, USD, EUR, GBP
_CURRENCY_PREFIX = re.compile(
    r'^\s*(?P<negative_outer>\()?'
    r'\s*(?P<symbol>\$|£|€|¥|USD|EUR|GBP|CAD|AUD)?\s*'
    r'(?P<negative_inner>-)?'
    r'(?P<digits>[\d,]+(?:\.\d+)?)'
    r'\s*(?P<negative_close>\))?\s*$',
    re.IGNORECASE,
)

_CURRENCY_SYMBOL_RE = re.compile(
    r'\b(USD|EUR|GBP|CAD|AUD)\b|\$|£|€|¥',
    re.IGNORECASE,
)

_KNOWN_CURRENCIES = {
    '$': 'USD', '£': 'GBP', '€': 'EUR', '¥': 'JPY',
    'USD': 'USD', 'EUR': 'EUR', 'GBP': 'GBP',
    'CAD': 'CAD', 'AUD': 'AUD',
}


def parse_amount(raw: str) -> ParsedField[Decimal]:
    """
    Parse an amount string into ParsedField[Decimal].

    Handles:
      "$24,000"    → Decimal("24000")
      "USD 5400"   → Decimal("5400")
      "(250.00)"   → Decimal("-250.00")   (accounting negative notation)
      "-250.00"    → Decimal("-250.00")
      "5400"       → Decimal("5400")

    Returns confidence=0.0 on failure (never raises).
    """
    if not raw or not isinstance(raw, str):
        return ParsedField(value=Decimal("0"), confidence=0.0, source="EXTRACTED",
                           notes="empty or non-string input")

    m = _CURRENCY_PREFIX.match(raw)
    if not m:
        return ParsedField(value=Decimal("0"), confidence=0.0, source="EXTRACTED",
                           notes=f"could not parse amount: {raw!r}")

    digits_str = m.group("digits").replace(",", "")
    is_negative = bool(m.group("negative_outer") and m.group("negative_close")) \
                  or bool(m.group("negative_inner"))

    try:
        value = Decimal(digits_str)
    except InvalidOperation:
        return ParsedField(value=Decimal("0"), confidence=0.0, source="EXTRACTED",
                           notes=f"invalid decimal: {digits_str!r}")

    if is_negative:
        value = -value

    # Confidence: explicit symbol → 1.0; bare number → 0.90
    has_symbol = bool(m.group("symbol"))
    confidence = 1.0 if has_symbol else 0.90

    return ParsedField(
        value=value,
        confidence=confidence,
        source="EXPLICIT" if has_symbol else "EXTRACTED",
    )


def parse_currency(raw: str) -> ParsedField[str]:
    """
    Extract currency code from a string.
    Defaults to "USD" with source=DEFAULT if no currency symbol found.
    """
    if not raw or not isinstance(raw, str):
        return ParsedField(value="USD", confidence=1.0, source="DEFAULT",
                           notes="no input — defaulted to USD")

    m = _CURRENCY_SYMBOL_RE.search(raw)
    if m:
        sym = m.group(0).upper()
        currency = _KNOWN_CURRENCIES.get(sym, "USD")
        return ParsedField(value=currency, confidence=1.0, source="EXPLICIT")

    return ParsedField(value="USD", confidence=1.0, source="DEFAULT",
                       notes="no currency symbol found — defaulted to USD")
