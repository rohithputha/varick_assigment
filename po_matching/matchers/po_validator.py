"""v1 PO format-only validation. Pure Python, no LLM, deterministic."""
from __future__ import annotations

from models import POMatchStatus

# Set to True in tests to bypass PO validation and force all invoices to MATCHED.
FORCE_MATCH: bool = False


def validate_po(po_number: str | None) -> tuple[POMatchStatus, float, str]:
    """
    v1 format-only PO check.

    Returns: (status, confidence, note)

    Rules:
      - None                → NO_PO (1.0)
      - starts with "PO"    → MATCHED (1.0)   (case-insensitive)
      - anything else       → INVALID_FORMAT (1.0)

    Confidence is always 1.0 in v1 — fully deterministic.
    """
    if FORCE_MATCH:
        return POMatchStatus.MATCHED, 1.0, f"FORCE_MATCH=True — treating as matched"

    if po_number is None:
        return POMatchStatus.NO_PO, 1.0, "No PO number present in invoice header"

    normalized = po_number.strip().upper()
    if normalized.startswith("PO"):
        return POMatchStatus.MATCHED, 1.0, f"PO {po_number} format valid"

    return (
        POMatchStatus.INVALID_FORMAT,
        1.0,
        f"PO number '{po_number}' does not match expected format (must start with 'PO')",
    )
