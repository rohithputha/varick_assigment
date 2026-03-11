"""Pure Python PO number parsing. No LLM, no side effects, deterministic."""
from __future__ import annotations

import re

from invoice_extraction.models import ParsedField


# Standard format: PO-YYYY-### (3+ digits)
_STANDARD_PO = re.compile(r'^PO-\d{4}-\d{3,}$', re.IGNORECASE)

# Loose: any token starting with PO followed by non-space chars
_LOOSE_PO = re.compile(r'\bPO[-\s]?[\w\-]+', re.IGNORECASE)


def parse_po_number(raw: str | None) -> ParsedField[str] | None:
    """
    Parse a PO number string.

    - None input → returns None (triggers MISSING_PO flag upstream)
    - Standard PO-YYYY-### format → confidence 1.0
    - Non-standard but parseable → confidence 0.80 (WARNING, not ERROR)
    - Cross-year POs (PO-2025-xxx on 2026 invoice) are VALID — no penalty
    - Empty string → returns None
    """
    if raw is None:
        return None

    if not isinstance(raw, str) or not raw.strip():
        return None

    raw = raw.strip()

    if _STANDARD_PO.match(raw):
        return ParsedField(
            value=raw.upper(),
            confidence=1.0,
            source="EXPLICIT",
        )

    # Loose match — extract the PO token if embedded in a longer string
    m = _LOOSE_PO.search(raw)
    if m:
        token = m.group(0).strip()
        return ParsedField(
            value=token.upper(),
            confidence=0.80,
            source="EXTRACTED",
            notes=f"non-standard PO format: {token!r}",
        )

    # Has content but doesn't look like a PO — treat as non-standard
    return ParsedField(
        value=raw.upper(),
        confidence=0.70,
        source="EXTRACTED",
        notes=f"unrecognized PO format: {raw!r}",
    )
