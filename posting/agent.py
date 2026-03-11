"""
Posting Agent — SOP Step 5 entry point.

Deterministic — no LLM calls.

Entry point: run_posting_agent(routed_invoice_dict) -> dict

HALTs if human approval is pending.
Returns a PostingResult dict as the final pipeline output.
"""
from __future__ import annotations

from posting.tools.posting_tools import post_all_lines, verify_invoice_total
from posting.tools.note_tools import add_note


def run_posting_agent(routed_invoice_dict: dict, run_id: str = "") -> dict:
    """
    Post journal entries and verify per SOP Step 5.

    The `run_id` parameter is used to generate deterministic (idempotent) entry IDs.
    It is injected by PostingRunner so that resuming a run always produces the same IDs.

    On entry:
        routed_invoice_dict: RoutedInvoice dict from Approval Routing stage.
            May also contain {"approved": True/False} injected by the CLI on resume.

    Returns:
        PostingResult dict:
        {
          "halted":             bool,   # True only when pending approval
          "success":            bool,
          "routed_invoice":     dict,
          "posting_status":     str,    # PostingStatus value
          "journal_entries":    list[dict],
          "total_posted":       str,
          "total_invoice":      str,
          "amounts_reconciled": bool,
          "skipped_lines":      list[int],
          "notes":              list[str],
        }
    """
    notes: list[str] = []

    # ── STEP 1: GUARD — check upstream success ────────────────────────────────
    if not routed_invoice_dict.get("success", True):
        return {
            "halted":  False,
            "success": False,
            "error":   "Approval Routing input invalid — success=False from upstream",
            "notes":   notes,
        }

    # ── STEP 2: APPROVAL GATE ────────────────────────────────────────────────
    routing = routed_invoice_dict.get("routing", {})
    outcome = routing.get("outcome", "")
    approved = routed_invoice_dict.get("approved")   # True | False | None (absent)

    # Explicit rejection by approver on resume — check BEFORE HALT gates
    # (approved=False is falsy, which would otherwise trigger the HALT branch)
    if approved is False:
        add_note(notes, "Invoice rejected by approver on resume.")
        return {
            "halted":             False,
            "success":            False,
            "posting_status":     "REJECTED",
            "reason":             "rejected_by_approver",
            "routed_invoice":     routed_invoice_dict,
            "journal_entries":    [],
            "total_posted":       "0",
            "total_invoice":      "0",
            "amounts_reconciled": False,
            "skipped_lines":      [],
            "notes":              notes,
        }

    if outcome == "DEPT_MANAGER" and not approved:
        return {
            "halted":  True,
            "reason":  "pending_dept_manager_approval",
            "routing": routing,
        }

    if outcome == "VP_FINANCE" and not approved:
        return {
            "halted":  True,
            "reason":  "pending_vp_finance_approval",
            "routing": routing,
        }

    if outcome == "DENY":
        add_note(notes, "Invoice denied by routing stage — no entries posted.")
        return {
            "halted":             False,
            "success":            False,
            "posting_status":     "REJECTED",
            "reason":             "invoice_denied_by_routing",
            "routed_invoice":     routed_invoice_dict,
            "journal_entries":    [],
            "total_posted":       "0",
            "total_invoice":      "0",
            "amounts_reconciled": False,
            "skipped_lines":      [],
            "notes":              notes,
        }

    # ── STEP 3: POST ALL LINES ────────────────────────────────────────────────
    posting_result = post_all_lines(routed_invoice_dict, run_id=run_id)

    if not posting_result.get("success"):
        error = posting_result.get("error", "post_all_lines returned success=False")
        add_note(notes, f"ERROR: {error}")
        return {
            "halted":  False,
            "success": False,
            "error":   error,
            "notes":   notes,
        }

    journal_entries = posting_result["journal_entries"]
    skipped_lines   = posting_result["skipped_lines"]

    for n in posting_result.get("notes", []):
        add_note(notes, n)

    # ── STEP 4: VERIFY TOTALS ─────────────────────────────────────────────────
    verify_result = verify_invoice_total(routed_invoice_dict, journal_entries)
    amounts_reconciled = verify_result.get("amounts_reconciled", False)
    total_posted       = verify_result.get("total_posted", "0")
    total_invoice      = verify_result.get("total_invoice", "0")
    delta              = verify_result.get("delta", "0")

    # ── STEP 5: ADD NOTES ─────────────────────────────────────────────────────
    for ln in skipped_lines:
        # Look up skip reason from line_results
        skip_reason = "gl_flagged"
        try:
            line_results = routed_invoice_dict["recognized_invoice"]["line_results"]
            for lr in line_results:
                if lr["line_number"] == ln:
                    skip_reason = lr.get("skip_reason", "gl_flagged") or "gl_flagged"
                    break
        except (KeyError, TypeError):
            pass
        add_note(notes, f"Line {ln}: not posted — {skip_reason}, human review required")

    if not amounts_reconciled:
        add_note(notes, f"RECONCILIATION MISMATCH — delta {delta}, manual review required")

    non_skipped = [
        lr for lr in (
            routed_invoice_dict.get("recognized_invoice", {}).get("line_results", [])
        )
        if not lr.get("skipped")
    ]
    add_note(
        notes,
        f"Posted {len(journal_entries)} entr{'y' if len(journal_entries) == 1 else 'ies'} "
        f"across {len(non_skipped)} line(s)",
    )

    if approved:
        add_note(notes, f"Human approval granted — outcome was {outcome}, proceeding to post.")

    # ── STEP 6: DETERMINE posting_status ─────────────────────────────────────
    posting_status = "PARTIAL_POSTED" if skipped_lines else "POSTED"

    # ── STEP 7: RETURN PostingResult dict ────────────────────────────────────
    return {
        "halted":             False,
        "success":            True,
        "routed_invoice":     routed_invoice_dict,
        "posting_status":     posting_status,
        "journal_entries":    journal_entries,
        "total_posted":       total_posted,
        "total_invoice":      total_invoice,
        "amounts_reconciled": amounts_reconciled,
        "skipped_lines":      skipped_lines,
        "notes":              notes,
    }
