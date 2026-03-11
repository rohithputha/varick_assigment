"""
Approval Routing Agent — SOP Step 4 entry point.

Deterministic — no LLM calls.

Entry point: run_approval_routing_agent(recognized_invoice_dict) -> dict

Returns a RoutedInvoice dict for the Posting stage.
Never halts.
"""
from __future__ import annotations

from approval_routing.tools.routing_tools import route
from approval_routing.tools.note_tools import add_note


def run_approval_routing_agent(recognized_invoice_dict: dict) -> dict:
    """
    Determine approval routing per SOP Step 4.

    Args:
        recognized_invoice_dict:
            RecognizedInvoice dict from the Prepaid/Accrual Recognition stage.
            Must contain {"success": True, "classified_invoice": {...}, "line_results": [...]}.

    Returns:
        RoutedInvoice dict:
        {
          "halted":             False,
          "success":            bool,
          "recognized_invoice": dict,   # full RecognizedInvoice, unmodified
          "routing": {
              "outcome", "applied_rule", "total_amount", "department",
              "has_capitalize", "all_lines_classified", "reasoning"
          },
          "notes":              list[str],
        }
    """
    notes: list[str] = []

    # ── STEP 1: GUARD — check upstream success ────────────────────────────────
    if not recognized_invoice_dict.get("success", True):
        return {
            "halted":  False,
            "success": False,
            "error":   "Prepaid/Accrual input invalid — success=False from upstream",
            "notes":   notes,
        }

    # ── STEP 2: ROUTE ─────────────────────────────────────────────────────────
    routing = route(recognized_invoice_dict)

    # ── STEP 3: ADD NOTES for notable outcomes ────────────────────────────────
    outcome      = routing["outcome"]
    applied_rule = routing["applied_rule"]
    total_amount = routing["total_amount"]

    if outcome == "AUTO_APPROVE":
        add_note(notes, f"Invoice AUTO_APPROVED — {applied_rule} (total: {total_amount})")

    elif outcome == "DEPT_MANAGER":
        add_note(notes, f"Invoice requires DEPT_MANAGER approval — {applied_rule} (total: {total_amount})")

    elif outcome == "VP_FINANCE":
        add_note(notes, f"Invoice requires VP_FINANCE approval — {applied_rule} (total: {total_amount})")

    elif outcome == "DENY":
        add_note(notes, f"Invoice DENIED — {applied_rule}")

    # Additional detail notes
    if routing.get("has_capitalize"):
        add_note(notes, "Fixed asset line present — VP Finance approval required regardless of amount")

    # Note if gl_flagged downgraded an AUTO_APPROVE
    if (
        not routing.get("all_lines_classified")
        and applied_rule == "dept_manager_gl_flagged_override"
    ):
        add_note(notes, "GL-flagged lines present — auto-approve downgraded to DEPT_MANAGER")

    # ── STEP 4: RETURN RoutedInvoice dict ────────────────────────────────────
    return {
        "halted":             False,
        "success":            True,
        "recognized_invoice": recognized_invoice_dict,
        "routing":            routing,
        "notes":              notes,
    }
