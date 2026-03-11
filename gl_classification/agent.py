"""
GL Classification Agent — SOP Step 2 entry point.

Deterministic rule-based classifier. No LLM calls.

Entry point: run_gl_classification_agent(invoice_dict, po_match_dict) -> dict

Returns a ClassifiedInvoice dict for the Prepaid/Accrual Recognition stage.
"""
from __future__ import annotations

from rules_engine.rules_tools import get_rules
from gl_classification.tools.classify_tools import classify_all_lines
from gl_classification.tools.note_tools import add_note


def run_gl_classification_agent(
    invoice_dict: dict,
    po_match_dict: dict,
) -> dict:
    """
    Classify each line item per SOP Step 2 (7-rule priority order).

    Args:
        invoice_dict:   Full Invoice dict from the ingestion module.
        po_match_dict:  POMatchResult dict from the PO Matching stage.

    Returns:
        On success — ClassifiedInvoice dict:
        {
            halted:              False,
            success:             bool,
            invoice:             dict,      # original invoice, unmodified
            po_match:            dict,      # po_match_dict, unmodified
            line_classifications: list[dict],
            all_classified:      bool,
            flagged_lines:       list[int],
            overall_confidence:  float,
            notes:               list[str],
        }
    """
    notes: list[str] = []

    # ── STEP 1: LOAD RULES ───────────────────────────────────────────────────
    try:
        rules_config  = get_rules()
        rules_version = rules_config.get("version", "unknown")
    except Exception as e:
        rules_version = "unknown"
        add_note(notes, f"WARNING: failed to load rules_config: {e}")

    # ── STEP 2: CLASSIFY ALL LINES ───────────────────────────────────────────
    classify_result = classify_all_lines(invoice_dict)

    if not classify_result.get("success"):
        error = classify_result.get("error", "classify_all_lines returned success=False")
        add_note(notes, f"ERROR: {error}")
        return {
            "halted":  False,
            "success": False,
            "error":   error,
            "notes":   notes,
        }

    line_classifications = classify_result["results"]
    flagged_lines        = classify_result["flagged_lines"]

    # ── STEP 3: HALT if any lines could not be classified ────────────────────
    if flagged_lines:
        flag_reasons = {
            r["line_number"]: r.get("flag_reason", "unknown")
            for r in line_classifications
            if r.get("flagged")
        }
        for ln, reason in flag_reasons.items():
            add_note(notes, f"HALT: Line {ln} unclassified — {reason}")
        return {
            "halted":       True,
            "reason":       "unclassified_lines",
            "flagged_lines": flagged_lines,
            "flag_reasons": flag_reasons,
            "invoice":      invoice_dict,
            "po_match":     po_match_dict,
            "notes":        notes,
        }

    # ── STEP 4: HANDLE RESULTS — notes for low-conf / capitalize ─────────────
    for r in line_classifications:
        ln = r["line_number"]

        if r.get("confidence", 1.0) < 0.80:
            add_note(
                notes,
                f"Line {ln}: low confidence ({r['confidence']:.2f}) — {r.get('reasoning', '')}",
            )

        if r.get("treatment") == "CAPITALIZE":
            add_note(notes, f"Fixed asset line {ln}: requires VP Finance approval")

    # ── STEP 5: RETURN ClassifiedInvoice dict ────────────────────────────────
    return {
        "halted":              False,
        "success":             True,
        "invoice":             invoice_dict,
        "po_match":            po_match_dict,
        "line_classifications": line_classifications,
        "all_classified":      classify_result["all_classified"],
        "flagged_lines":       classify_result["flagged_lines"],
        "overall_confidence":  classify_result["overall_confidence"],
        "notes":               notes,
    }
