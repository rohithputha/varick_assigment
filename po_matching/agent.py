"""
PO Matching Agent — v1 entry point.

v1 is a single deterministic tool call (no LLM, no agentic loop).
The "agent" wrapper exists for interface consistency with the orchestrator.
"""
from __future__ import annotations

from po_matching.tools.match_tools import match_po
from models import POMatchStatus


def run_po_matching_agent(invoice_dict: dict) -> dict:
    """
    Accept Invoice dict from the ingestion module.
    Return POMatchResult dict for the GL Classification stage.

    Args:
        invoice_dict: Full Invoice dict produced by finalize_invoice (ingestion output).
                      May be nested under result["invoice"] — callers must unwrap first.

    Returns:
        {
            "halted":     False,    # PO Matching v1 never halts
            "success":    bool,
            "invoice_id": str,
            "po_number":  str | None,
            "status":     str,      # POMatchStatus value
            "matched":    bool,
            "notes":      [str],
            "confidence": float,
        }

    v1 Workflow:
        STEP 1: match_po(invoice_dict)
        STEP 2: Flag and add notes based on result status
        STEP 3: Return POMatchResult dict (never halts in v1)
    """
    try:
        result = match_po(invoice_dict)

        if not result.get("success"):
            return {
                "halted":     False,
                "success":    False,
                "error":      result.get("error", "match_po returned success=False"),
                "confidence": 0.0,
            }

        status  = result["status"]
        matched = result["matched"]
        notes   = list(result["notes"])

        # Decision logic per plan:
        # MATCHED        → proceed, pass result to next stage
        # NO_PO          → flag, proceed (may be approved manually at Approval stage)
        # INVALID_FORMAT → flag, proceed (flag for human review at Approval stage)

        if status == POMatchStatus.NO_PO:
            notes.append("Invoice has no PO — halting for human review")
            return {
                "halted":     True,
                "reason":     status.value,
                "invoice_id": result["invoice_id"],
                "po_number":  result["po_number"],
                "notes":      notes,
            }
        elif status == POMatchStatus.INVALID_FORMAT:
            notes.append(
                f"PO format invalid — '{result['po_number']}' halting for human review"
            )
            return {
                "halted":     True,
                "reason":     status.value,
                "invoice_id": result["invoice_id"],
                "po_number":  result["po_number"],
                "notes":      notes,
            }

        return {
            "halted":     False,
            "success":    True,
            "invoice_id": result["invoice_id"],
            "po_number":  result["po_number"],
            "status":     status,
            "matched":    matched,
            "notes":      notes,
            "confidence": result["confidence"],
        }

    except Exception as e:
        return {
            "halted":     False,
            "success":    False,
            "error":      str(e),
            "confidence": 0.0,
        }
