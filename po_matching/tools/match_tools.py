"""
match_po tool — the single agent-callable tool for PO matching v1.
"""
from __future__ import annotations

from po_matching.matchers.po_validator import validate_po
from models import POMatchStatus


def match_po(invoice_dict: dict) -> dict:
    """
    Run v1 PO format check against an invoice dict.

    Reads:
        invoice_dict["header"]["po_number"]   — the ParsedField dict for po_number
        invoice_dict["invoice_id"]

    Returns:
        {
            "success":    bool,
            "matched":    bool,           # True = MATCHED
            "po_number":  str | None,
            "status":     str,            # POMatchStatus value
            "invoice_id": str,
            "notes":      [str, ...],
            "confidence": float,
        }
        On unexpected error:
        {
            "success":  False,
            "matched":  False,
            "error":    str,
            "confidence": 0.0,
        }
    """
    try:
        invoice_id = invoice_dict.get("invoice_id", "unknown")

        header = invoice_dict.get("header") or {}
        po_field = header.get("po_number")

        # po_number is stored as a ParsedField dict {"value": ..., "confidence": ...}
        # or as a plain string. Handle both.
        if isinstance(po_field, dict):
            po_number = po_field.get("value")
            # Treat empty string same as None
            if po_number == "" or po_number is None:
                po_number = None
        elif isinstance(po_field, str):
            po_number = po_field.strip() or None
        else:
            po_number = None  # None, missing, or unexpected type

        status, confidence, note = validate_po(po_number)

        return {
            "success":    True,
            "matched":    status == POMatchStatus.MATCHED,
            "po_number":  po_number,
            "status":     status.value,
            "invoice_id": invoice_id,
            "notes":      [note],
            "confidence": confidence,
        }

    except Exception as e:
        return {
            "success":    False,
            "matched":    False,
            "error":      str(e),
            "confidence": 0.0,
        }
