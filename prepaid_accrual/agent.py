"""
Prepaid & Accrual Recognition Agent — SOP Step 3 entry point.

Deterministic — no LLM calls.

Entry point: run_prepaid_accrual_agent(classified_invoice_dict) -> dict

Returns a RecognizedInvoice dict for the Approval Routing stage.
Never halts.
"""
from __future__ import annotations

from prepaid_accrual.tools.recognition_tools import process_all_lines
from prepaid_accrual.tools.note_tools import add_note


def run_prepaid_accrual_agent(classified_invoice_dict: dict) -> dict:
    """
    Apply prepaid and accrual recognition per SOP Step 3.

    Args:
        classified_invoice_dict: ClassifiedInvoice dict from GL Classification.
            Must contain "invoice", "line_classifications", "success".

    Returns:
        RecognizedInvoice dict:
        {
            halted:               False,
            success:              bool,
            classified_invoice:   dict,   # full ClassifiedInvoice, unmodified
            line_results:         list[dict],
            has_prepaid_lines:    bool,
            has_accrual_lines:    bool,
            prepaid_line_count:   int,
            accrual_line_count:   int,
            all_processed:        bool,
            notes:                list[str],
        }
    """
    notes: list[str] = []

    # ── STEP 1: GUARD ────────────────────────────────────────────────────────
    if not classified_invoice_dict.get("success", True):
        return {
            "halted":  False,
            "success": False,
            "error":   "GL Classification input invalid — success=False",
            "notes":   notes,
        }

    # ── STEP 2: PROCESS ALL LINES ────────────────────────────────────────────
    result = process_all_lines(classified_invoice_dict)

    if not result.get("success"):
        error = result.get("error", "process_all_lines returned success=False")
        add_note(notes, f"ERROR: {error}")
        return {
            "halted":  False,
            "success": False,
            "error":   error,
            "notes":   notes,
        }

    line_results = result["line_results"]

    # Merge any tool-level notes
    for n in result.get("notes", []):
        add_note(notes, n)

    # ── STEP 3: HANDLE RESULTS — notes for notable outcomes ──────────────────
    for r in line_results:
        ln          = r["line_number"]
        skip_reason = r.get("skip_reason")
        prepaid     = r.get("prepaid_result")
        accrual     = r.get("accrual_result")

        if r.get("skipped"):
            if skip_reason == "gl_flagged":
                add_note(notes, f"Line {ln}: skipped — GL Classification flagged this line")
            elif skip_reason == "capitalize_not_in_scope":
                add_note(notes, f"Line {ln}: fixed asset — prepaid/accrual recognition not applicable")
            elif skip_reason:
                add_note(notes, f"Line {ln}: skipped — {skip_reason}")

        elif accrual is not None:
            add_note(
                notes,
                f"Line {ln}: ACCRUAL → {accrual['accrual_account']} "
                f"({accrual['expense_account']} expense, reversal {accrual['reversal_trigger']})",
            )

        elif prepaid is not None:
            if prepaid.get("amortization_months", 0) == 0:
                add_note(notes, f"Line {ln}: PREPAID — amortization schedule not computed (service dates missing)")
            elif prepaid.get("is_reclassified"):
                add_note(
                    notes,
                    f"Line {ln}: insurance reclassified EXPENSE→PREPAID "
                    f"{prepaid['prepaid_account']} "
                    f"(amortize to {prepaid['expense_account']}, "
                    f"{prepaid['amortization_months']} months)",
                )
            else:
                add_note(
                    notes,
                    f"Line {ln}: PREPAID schedule computed — "
                    f"{prepaid['amortization_months']} monthly entries to "
                    f"{prepaid['expense_account']}",
                )

    # ── STEP 4: RETURN RecognizedInvoice dict ────────────────────────────────
    return {
        "halted":             False,
        "success":            True,
        "classified_invoice": classified_invoice_dict,
        "line_results":       line_results,
        "has_prepaid_lines":  result["has_prepaid_lines"],
        "has_accrual_lines":  result["has_accrual_lines"],
        "prepaid_line_count": result["prepaid_line_count"],
        "accrual_line_count": result["accrual_line_count"],
        "all_processed":      result["all_processed"],
        "notes":              notes,
    }
