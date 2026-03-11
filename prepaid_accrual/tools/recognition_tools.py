"""
Recognition tools — agent-callable wrappers around the recognizer functions.

process_line(line_classification, line_item) -> dict
process_all_lines(classified_invoice_dict)   -> dict
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from decimal import Decimal

from prepaid_accrual.recognizer.accrual  import detect_accrual
from prepaid_accrual.recognizer.prepaid  import detect_prepaid


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _decimal_str(v) -> str | None:
    if v is None:
        return None
    return str(v)


def _date_str(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, date) and v == date.min:
        return None
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _serialize_prepaid(pr) -> dict | None:
    if pr is None:
        return None
    entries = [
        {
            "period_label":    e.period_label,
            "period_start":    _date_str(e.period_start),
            "period_end":      _date_str(e.period_end),
            "amount":          _decimal_str(e.amount),
            "prepaid_account": e.prepaid_account,
            "expense_account": e.expense_account,
        }
        for e in pr.amortization_entries
    ]
    return {
        "line_number":             pr.line_number,
        "prepaid_account":         pr.prepaid_account,
        "expense_account":         pr.expense_account,
        "total_amount":            _decimal_str(pr.total_amount),
        "service_period_start":    _date_str(pr.service_period_start),
        "service_period_end":      _date_str(pr.service_period_end),
        "amortization_months":     pr.amortization_months,
        "amortization_entries":    entries,
        "is_reclassified":         pr.is_reclassified,
        "reclassification_reason": pr.reclassification_reason,
    }


def _serialize_accrual(ar) -> dict | None:
    if ar is None:
        return None
    return {
        "line_number":        ar.line_number,
        "accrual_account":    ar.accrual_account,
        "expense_account":    ar.expense_account,
        "amount":             _decimal_str(ar.amount),
        "service_period_end": ar.service_period_end,
        "reversal_trigger":   ar.reversal_trigger,
    }


# ---------------------------------------------------------------------------
# process_line
# ---------------------------------------------------------------------------

def process_line(line_classification: dict, line_item: dict) -> dict:
    """
    Apply the 6-rule Step 3 decision tree to one invoice line.

    Priority:
      Rule 0: flagged=True           → skip (gl_flagged)
      Rule 1: treatment=CAPITALIZE   → skip (capitalize_not_in_scope)
      Rule 2: service_precedes_invoice → ACCRUAL
      Rule 3: insurance annual/long  → PREPAID 1320 reclassification
      Rule 4: treatment=PREPAID      → PREPAID (accounts from Step 2, add schedule)
      Rule 5: everything else        → no_action_required

    Never raises — exceptions → skipped=True, skip_reason="recognition_error: ...".
    """
    ln                  = line_classification.get("line_number", 0)
    original_treatment  = line_classification.get("treatment")
    original_gl_account = line_classification.get("gl_account")
    notes: list[str]    = []

    try:
        # Rule 0 — GL flagged
        if line_classification.get("flagged", False):
            return _skipped(ln, original_treatment, original_gl_account,
                            "gl_flagged", notes)

        # Rule 1 — CAPITALIZE
        if original_treatment == "CAPITALIZE":
            return _skipped(ln, original_treatment, original_gl_account,
                            "capitalize_not_in_scope", notes)

        # Rule 2 — ACCRUAL
        accrual = detect_accrual(line_classification, line_item)
        if accrual is not None:
            if accrual.service_period_end == "unknown":
                notes.append("service_period_end missing — accrual period must be confirmed manually")
            return {
                "line_number":          ln,
                "original_treatment":   original_treatment,
                "final_treatment":      "ACCRUAL",
                "original_gl_account":  original_gl_account,
                "final_gl_account":     accrual.accrual_account,
                "prepaid_result":       None,
                "accrual_result":       _serialize_accrual(accrual),
                "no_action_required":   False,
                "skipped":              False,
                "skip_reason":          None,
                "notes":                notes,
            }

        # Rules 3 & 4 — PREPAID
        prepaid = detect_prepaid(line_classification, line_item)
        if prepaid is not None:
            if prepaid.amortization_months == 0:
                notes.append("service dates missing — amortization schedule not computed")
            final_gl = prepaid.prepaid_account  # 1320 if reclassified, else original
            return {
                "line_number":          ln,
                "original_treatment":   original_treatment,
                "final_treatment":      "PREPAID",
                "original_gl_account":  original_gl_account,
                "final_gl_account":     final_gl,
                "prepaid_result":       _serialize_prepaid(prepaid),
                "accrual_result":       None,
                "no_action_required":   False,
                "skipped":              False,
                "skip_reason":          None,
                "notes":                notes,
            }

        # Rule 5 — no action
        return {
            "line_number":          ln,
            "original_treatment":   original_treatment,
            "final_treatment":      original_treatment,
            "original_gl_account":  original_gl_account,
            "final_gl_account":     original_gl_account,
            "prepaid_result":       None,
            "accrual_result":       None,
            "no_action_required":   True,
            "skipped":              False,
            "skip_reason":          None,
            "notes":                notes,
        }

    except Exception as e:
        return _skipped(ln, original_treatment, original_gl_account,
                        f"recognition_error: {e}", notes)


# ---------------------------------------------------------------------------
# process_all_lines
# ---------------------------------------------------------------------------

def process_all_lines(classified_invoice_dict: dict) -> dict:
    """
    Process every line in the classified invoice.

    Matches line_items to line_classifications by line_number (not by index).
    One line failing does NOT abort the rest.

    Returns:
        {
            success:            bool,
            line_results:       list[dict],
            has_prepaid_lines:  bool,
            has_accrual_lines:  bool,
            prepaid_line_count: int,
            accrual_line_count: int,
            all_processed:      bool,
            notes:              list[str],
        }
    """
    invoice            = classified_invoice_dict.get("invoice") or {}
    line_items         = invoice.get("line_items") or []
    line_classifications = classified_invoice_dict.get("line_classifications") or []

    # Build lookup: line_number → line_item dict
    item_by_ln: dict[int, dict] = {
        li.get("line_number", i): li
        for i, li in enumerate(line_items)
    }

    results: list[dict] = []
    tool_notes: list[str] = []

    for lc in line_classifications:
        ln = lc.get("line_number", 0)
        li = item_by_ln.get(ln)

        if li is None:
            # Mismatched line_number
            results.append(_skipped(
                ln,
                lc.get("treatment"),
                lc.get("gl_account"),
                f"recognition_error: no line_item found for line_number={ln}",
                [],
            ))
            tool_notes.append(f"Line {ln}: no matching line_item — skipped")
            continue

        results.append(process_line(lc, li))

    prepaid_count  = sum(1 for r in results if r.get("prepaid_result") is not None)
    accrual_count  = sum(1 for r in results if r.get("accrual_result") is not None)
    gl_flagged_any = any(
        r.get("skipped") and r.get("skip_reason") == "gl_flagged"
        for r in results
    )

    return {
        "success":            True,
        "line_results":       results,
        "has_prepaid_lines":  prepaid_count > 0,
        "has_accrual_lines":  accrual_count > 0,
        "prepaid_line_count": prepaid_count,
        "accrual_line_count": accrual_count,
        "all_processed":      not gl_flagged_any,
        "notes":              tool_notes,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _skipped(
    line_number: int,
    original_treatment: str | None,
    original_gl_account: str | None,
    skip_reason: str,
    notes: list[str],
) -> dict:
    return {
        "line_number":          line_number,
        "original_treatment":   original_treatment,
        "final_treatment":      original_treatment,
        "original_gl_account":  original_gl_account,
        "final_gl_account":     original_gl_account,
        "prepaid_result":       None,
        "accrual_result":       None,
        "no_action_required":   False,
        "skipped":              True,
        "skip_reason":          skip_reason,
        "notes":                notes,
    }
