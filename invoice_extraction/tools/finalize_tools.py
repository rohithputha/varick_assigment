"""
Finalize tools — compute overall confidence and assemble the final Invoice dict.
"""
from __future__ import annotations

import re
import uuid
from decimal import Decimal

from invoice_extraction.models import FlagType, InvoiceStatus, Severity
from invoice_extraction.state import StateManager


# ---------------------------------------------------------------------------
# compute_confidence
# ---------------------------------------------------------------------------

def compute_confidence(state_id: str) -> dict:
    """
    Compute overall invoice confidence as a weighted mean of field_confidences.

    Weights:
      "header.*"             → 1.0
      "line_items[N].amount" → 1.0
      "line_items[N].desc.*" → 0.7

    Penalties applied after weighted mean:
      -0.15 per ERROR issue
      -0.05 per WARNING issue

    Writes: state.overall_confidence

    Returns:
        {
            "success": bool,
            "overall_confidence": float,
            "breakdown": {
                "header_mean": float,
                "line_amount_mean": float,
                "line_desc_mean": float,
                "penalty_total": float,
                "final_confidence": float,
            },
        }
    """
    try:
        state = StateManager.get(state_id)
        fc = state.field_confidences

        header_vals  = []
        amount_vals  = []
        desc_vals    = []

        for key, val in fc.items():
            if key.startswith("header."):
                header_vals.append(val)
            elif re.match(r'^line_items\[\d+\]\.amount$', key):
                amount_vals.append(val)
            elif re.match(r'^line_items\[\d+\]\.desc\.', key):
                desc_vals.append(val)

        header_mean      = sum(header_vals)  / len(header_vals)  if header_vals  else 0.0
        line_amount_mean = sum(amount_vals)  / len(amount_vals)  if amount_vals  else 0.0
        line_desc_mean   = sum(desc_vals)    / len(desc_vals)    if desc_vals    else 0.0

        # Weighted mean — desc weighted at 0.7
        all_weighted = (
            [(v, 1.0) for v in header_vals] +
            [(v, 1.0) for v in amount_vals] +
            [(v, 0.7) for v in desc_vals]
        )

        if all_weighted:
            total_weight = sum(w for _, w in all_weighted)
            weighted_sum = sum(v * w for v, w in all_weighted)
            base_confidence = weighted_sum / total_weight
        else:
            base_confidence = 0.0

        # Count issues for penalties
        all_issues = state.structural_issues + state.business_issues
        error_count   = sum(1 for i in all_issues if i.get("severity") == Severity.ERROR)
        warning_count = sum(1 for i in all_issues if i.get("severity") == Severity.WARNING)

        penalty = error_count * 0.15 + warning_count * 0.05
        final = max(0.0, min(1.0, base_confidence - penalty))

        state.overall_confidence = round(final, 4)
        StateManager.update(state)

        return {
            "success":            True,
            "overall_confidence": state.overall_confidence,
            "breakdown": {
                "header_mean":      round(header_mean,      4),
                "line_amount_mean": round(line_amount_mean, 4),
                "line_desc_mean":   round(line_desc_mean,   4),
                "penalty_total":    round(penalty,          4),
                "final_confidence": round(final,            4),
            },
            "confidence": 1.0,
        }

    except Exception as e:
        return {"success": False, "error": str(e), "confidence": 0.0}


# ---------------------------------------------------------------------------
# finalize_invoice
# ---------------------------------------------------------------------------

def finalize_invoice(state_id: str) -> dict:
    """
    Assemble the final Invoice dict from state and advance stage to COMPLETE.

    Determines InvoiceStatus from flags:
      AMOUNT_MISMATCH flag  → FLAGGED_AMOUNT_MISMATCH
      MISSING_PO flag       → FLAGGED_NO_PO
      AMBIGUOUS_CATEGORY    → FLAGGED_AMBIGUOUS
      MISSING_DATA flag     → FLAGGED_MISSING_DATA
      (checked in priority order — first match wins)
      No flags              → READY_FOR_MATCHING
      Unresolved ERROR issues → FAILED

    Writes: state.final_invoice, stage → COMPLETE (or FAILED)

    Returns:
        {
            "success": bool,
            "invoice": <full Invoice dict>,
            "state_id": str,
            "overall_confidence": float,
        }
    """
    try:
        state = StateManager.get(state_id)

        raw = state.raw_input or {}

        # --- Determine invoice_id ---
        invoice_id = (
            raw.get("invoice_id")
            or raw.get("invoice_number")
            or str(uuid.uuid4())
        )

        # --- Determine status from flags ---
        flag_types = {f.get("flag_type") for f in state.flags}

        # Check for unresolved ERROR issues
        all_issues = state.structural_issues + state.business_issues
        has_unresolved_errors = any(
            i.get("severity") == Severity.ERROR for i in all_issues
        )

        # Flag checks take priority over generic error check
        # (AMOUNT_MISMATCH note: "should have halted earlier" — safety net)
        if FlagType.AMOUNT_MISMATCH in flag_types:
            status = InvoiceStatus.FLAGGED_AMOUNT_MISMATCH
        elif has_unresolved_errors:
            status = InvoiceStatus.FAILED
        elif FlagType.MISSING_PO in flag_types:
            status = InvoiceStatus.FLAGGED_NO_PO
        elif FlagType.AMBIGUOUS_CATEGORY in flag_types:
            status = InvoiceStatus.FLAGGED_AMBIGUOUS
        elif FlagType.MISSING_DATA in flag_types:
            status = InvoiceStatus.FLAGGED_MISSING_DATA
        else:
            status = InvoiceStatus.READY_FOR_MATCHING

        invoice = {
            "invoice_id":         invoice_id,
            "raw_data":           raw,
            "header":             state.header,
            "line_items":         state.line_items,
            "status":             status,
            "flags":              state.flags,
            "overall_confidence": state.overall_confidence,
            "processing_notes":   state.agent_notes,
            "state_id":           state_id,
        }

        state.final_invoice = invoice
        StateManager.update(state)

        new_stage = "COMPLETE" if status != InvoiceStatus.FAILED else "FAILED"
        try:
            StateManager.advance_stage(state_id, new_stage)
        except ValueError:
            # Already at target stage — safe to ignore
            pass

        return {
            "success":            True,
            "invoice":            invoice,
            "state_id":           state_id,
            "overall_confidence": state.overall_confidence,
            "confidence":         1.0,
        }

    except Exception as e:
        return {"success": False, "error": str(e), "confidence": 0.0}
