"""Pure Python date parsing. No LLM, no side effects, deterministic."""
from __future__ import annotations

import re
from datetime import date, datetime
from calendar import monthrange

from invoice_extraction.models import ParsedField


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# ISO 8601: 2026-01-05
_ISO = re.compile(r'^\s*(\d{4})-(\d{2})-(\d{2})\s*$')

# Long form: "Jan 5, 2026" | "January 5, 2026" | "Jan. 5 2026"
_LONG = re.compile(
    r'^\s*([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})\s*$'
)

# Short numeric: "01/15/26" | "1/15/2026" | "01-15-2026"
_SHORT = re.compile(
    r'^\s*(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})\s*$'
)

# Partial: "Jan 2026" | "January 2026"
_PARTIAL = re.compile(r'^\s*([A-Za-z]+)\.?\s+(\d{4})\s*$')

_MONTH_NAMES = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
    'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
    'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'june': 6, 'july': 7, 'august': 8, 'september': 9,
    'october': 10, 'november': 11, 'december': 12,
}


def _month_num(s: str) -> int | None:
    return _MONTH_NAMES.get(s.lower().rstrip('.'))


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_invoice_date(raw: str) -> ParsedField[date]:
    """
    Parse a date string into a ParsedField[date].

    Priority:
      ISO 8601           → confidence 1.00
      Long form          → confidence 0.95
      Short numeric      → confidence 0.95
      Partial (no day)   → confidence 0.70, day=1
      Failure            → confidence 0.00, value=date(1900,1,1)
    """
    if not raw or not isinstance(raw, str):
        return ParsedField(value=date(1900, 1, 1), confidence=0.0, source="EXTRACTED",
                           notes="empty or non-string input")

    # ISO 8601
    m = _ISO.match(raw)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            return ParsedField(value=d, confidence=1.0, source="EXPLICIT")

    # Long form
    m = _LONG.match(raw)
    if m:
        month = _month_num(m.group(1))
        if month:
            d = _safe_date(int(m.group(3)), month, int(m.group(2)))
            if d:
                return ParsedField(value=d, confidence=0.95, source="EXTRACTED")

    # Short numeric
    m = _SHORT.match(raw)
    if m:
        part1, part2, part3 = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year = part3 + 2000 if part3 < 100 else part3
        # Assume MM/DD/YYYY
        d = _safe_date(year, part1, part2)
        if d:
            return ParsedField(value=d, confidence=0.95, source="EXTRACTED",
                               notes="assumed MM/DD/YYYY format")

    # Partial: "Jan 2026"
    m = _PARTIAL.match(raw)
    if m:
        month = _month_num(m.group(1))
        if month:
            d = _safe_date(int(m.group(2)), month, 1)
            if d:
                return ParsedField(value=d, confidence=0.70, source="INFERRED",
                                   notes="day defaulted to 1 (partial date)")

    return ParsedField(value=date(1900, 1, 1), confidence=0.0, source="EXTRACTED",
                       notes=f"could not parse date: {raw!r}")


def parse_date_range(
    start_raw: str,
    end_raw: str,
) -> tuple[ParsedField[date], ParsedField[date]]:
    """
    Parse start and end date strings.
    Cross-validates: if end < start, returns both with confidence=0.0 and a note.
    """
    start_pf = parse_invoice_date(start_raw)
    end_pf   = parse_invoice_date(end_raw)

    if start_pf.confidence > 0.0 and end_pf.confidence > 0.0:
        if end_pf.value < start_pf.value:
            note = f"end date {end_pf.value} precedes start date {start_pf.value}"
            start_pf = ParsedField(value=start_pf.value, confidence=0.0,
                                   source=start_pf.source, notes=note)
            end_pf   = ParsedField(value=end_pf.value, confidence=0.0,
                                   source=end_pf.source, notes=note)

    return start_pf, end_pf
