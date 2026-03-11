"""
Posting tools — wrappers over journal builder and reconciliation verifier.

post_all_lines(routed_invoice_dict, run_id) → dict
verify_invoice_total(routed_invoice_dict, journal_entries) → dict

Never raise — all exceptions caught and returned as failure dicts.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from posting.journal.builder import build_entries_for_line
from posting.verifier.reconcile import verify_totals


def _extract_run_id(routed_invoice_dict: dict) -> str:
    """Best-effort extraction of run_id for entry ID hashing. Falls back to empty string."""
    return routed_invoice_dict.get("run_id", "")


def _extract_invoice_id(routed_invoice_dict: dict) -> str:
    """Walk the nested dict to find invoice_id. Falls back to empty string."""
    try:
        return (
            routed_invoice_dict["recognized_invoice"]
            ["classified_invoice"]["invoice"]["invoice_id"]
        )
    except (KeyError, TypeError):
        return ""


def post_all_lines(routed_invoice_dict: dict, run_id: str = "") -> dict:
    """
    Generate journal entries for every line in the invoice.

    Args:
        routed_invoice_dict: RoutedInvoice dict from Approval Routing stage.
        run_id:              Pipeline run ID for deterministic entry ID generation.
                             Passed in by the agent; not always present in the dict.

    Returns:
        {
          "success":         bool,
          "journal_entries": list[dict],  # serialised JournalEntry dicts
          "skipped_lines":   list[int],
          "notes":           list[str],
        }
    """
    notes: list[str] = []

    try:
        recognized = routed_invoice_dict["recognized_invoice"]
        classified  = recognized["classified_invoice"]
        invoice     = classified["invoice"]

        line_results: list[dict] = recognized.get("line_results", [])
        line_items:   list[dict] = invoice.get("line_items", [])

        # Parse invoice date
        inv_date_raw = invoice["header"]["invoice_date"]
        if isinstance(inv_date_raw, dict):
            inv_date_value = inv_date_raw.get("value")
        else:
            inv_date_value = inv_date_raw

        from datetime import date as _date
        if isinstance(inv_date_value, _date):
            invoice_date = inv_date_value
        else:
            from datetime import datetime
            invoice_date = datetime.fromisoformat(str(inv_date_value)).date()

        invoice_id = _extract_invoice_id(routed_invoice_dict)

        # Build a dict: line_number → line_item
        items_by_ln: dict[int, dict] = {
            li["line_number"]: li for li in line_items if "line_number" in li
        }

        all_entries: list[dict] = []
        skipped_lines: list[int] = []

        for lr in line_results:
            ln = lr["line_number"]

            if lr.get("skipped"):
                skipped_lines.append(ln)
                continue

            li = items_by_ln.get(ln)
            if li is None:
                notes.append(f"Line {ln}: no matching line_item found — skipped")
                skipped_lines.append(ln)
                continue

            entries = build_entries_for_line(lr, li, invoice_date, run_id, invoice_id)
            for e in entries:
                all_entries.append({
                    "entry_id":       e.entry_id,
                    "line_number":    e.line_number,
                    "entry_type":     e.entry_type,
                    "debit_account":  e.debit_account,
                    "credit_account": e.credit_account,
                    "amount":         str(e.amount),
                    "period_label":   e.period_label,
                    "description":    e.description,
                })

        return {
            "success":         True,
            "journal_entries": all_entries,
            "skipped_lines":   skipped_lines,
            "notes":           notes,
        }

    except Exception as exc:
        return {
            "success":         False,
            "error":           f"posting_error: {exc}",
            "journal_entries": [],
            "skipped_lines":   [],
            "notes":           notes,
        }


def verify_invoice_total(
    routed_invoice_dict: dict,
    journal_entries: list[dict],
) -> dict:
    """
    Verify that line amounts sum to the invoice header total.

    Returns:
        {
          "success":            bool,
          "amounts_reconciled": bool,
          "total_posted":       str,   # Decimal as string
          "total_invoice":      str,
          "delta":              str,
          "note":               str,
        }
    """
    try:
        recognized  = routed_invoice_dict["recognized_invoice"]
        classified  = recognized["classified_invoice"]
        invoice     = classified["invoice"]
        line_results: list[dict] = recognized.get("line_results", [])
        line_items:   list[dict] = invoice.get("line_items", [])

        inv_total_raw = invoice["header"]["total_amount"]
        if isinstance(inv_total_raw, dict):
            inv_total_value = inv_total_raw.get("value")
        else:
            inv_total_value = inv_total_raw
        invoice_total = Decimal(str(inv_total_value))

        reconciled, total_posted, delta, note = verify_totals(
            line_results, line_items, invoice_total
        )

        return {
            "success":            True,
            "amounts_reconciled": reconciled,
            "total_posted":       str(total_posted),
            "total_invoice":      str(invoice_total),
            "delta":              str(delta),
            "note":               note,
        }

    except Exception as exc:
        return {
            "success":            False,
            "error":              f"verification_error: {exc}",
            "amounts_reconciled": False,
            "total_posted":       "0",
            "total_invoice":      "0",
            "delta":              "0",
            "note":               str(exc),
        }
