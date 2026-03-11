"""
Invoice Ingestion Pipeline — main entry point.

Plain Python orchestration: no LLM, no agentic loop.
Calls tools step-by-step with explicit decision logic matching the original spec.

LLM usage:
  - description_extractor.py  → 1 haiku call per line item (semantic extraction)
  - line_item_structurizer.py → 1 haiku call per invoice, only when regex split fails
"""
from __future__ import annotations

from invoice_extraction.tools.input_tools    import load_invoice_from_file, load_invoice_from_dict
from invoice_extraction.tools.parse_tools    import parse_invoice_header, parse_invoice_line, parse_line_description
from invoice_extraction.tools.validate_tools import validate_structure, validate_business_rules, validate_single_rule
from invoice_extraction.tools.finalize_tools import compute_confidence, finalize_invoice
from invoice_extraction.tools.note_tools     import add_note
from invoice_extraction.state import StateManager


# Required header fields that trigger a HALT if unextractable
_REQUIRED_HEADER_FIELDS = ("vendor_name", "invoice_date", "total_amount")

# Confidence thresholds
_CONF_LOW      = 0.60
_CONF_WARN     = 0.75
_CONF_RETRY    = 0.70   # line-level: retry description if below this


def run_ingestion_agent(
    input: str | dict,
    resume_state_id: str | None = None,
) -> dict:
    """
    Transform raw invoice data into a validated Invoice dict.

    Args:
        input:           File path (str) or raw invoice dict.
        resume_state_id: Ignored in practice — HITL resume always re-runs from scratch
                         with the corrected input. Kept for interface compatibility.

    Returns:
        On success:  finalize_invoice() result dict  (contains "invoice", "success": True)
        On halt:     {"halted": True, "state_id": str, "reason": str}
    """
    # resume_state_id is kept for interface compatibility with the orchestrator.
    # HITL correction = re-run the full pipeline with corrected input from scratch.
    # The old state_id is for audit only; we always create a fresh state here.

    # ── STEP 1: LOAD ─────────────────────────────────────────────────────────
    if isinstance(input, str):
        load_result = load_invoice_from_file(input)
    else:
        load_result = load_invoice_from_dict(input)

    if not load_result.get("success"):
        return {
            "halted": True,
            "reason": "load_failed",
            "error":  load_result.get("error", "unknown load error"),
        }

    sid        = load_result["state_id"]
    line_count = load_result["line_count"]

    if load_result.get("structurizer_used"):
        add_note(sid, "structurizer used: regex split failed on line_items — fell back to haiku")

    # ── STEP 2: PARSE HEADER ─────────────────────────────────────────────────
    header_result = parse_invoice_header(sid)

    if not header_result.get("success"):
        return {
            "halted":   True,
            "reason":   "header_parse_failed",
            "state_id": sid,
            "error":    header_result.get("error", "unknown header parse error"),
        }

    header = header_result.get("header", {})

    # Note low-confidence fields
    for field in header_result.get("issues", []):
        conf = (header.get(field) or {}).get("confidence", 0.0)
        add_note(sid, f"Header field '{field}' has low confidence: {conf:.2f}")

    # Halt if any required field is completely unextractable (confidence == 0.0)
    for field in _REQUIRED_HEADER_FIELDS:
        field_data = header.get(field) or {}
        if field_data.get("confidence", 0.0) == 0.0:
            # Retry once
            add_note(sid, f"Required field '{field}' confidence=0.0 — retrying header parse")
            retry = parse_invoice_header(sid)
            retry_conf = ((retry.get("header") or {}).get(field) or {}).get("confidence", 0.0)
            if retry_conf == 0.0:
                add_note(sid, f"HALT: required field '{field}' still 0.0 after retry")
                return {
                    "halted":   True,
                    "reason":   "header_parse_failed",
                    "state_id": sid,
                    "field":    field,
                }
            # Update header reference with retry result
            header = retry.get("header", header)

    # ── STEP 3: PARSE LINES ──────────────────────────────────────────────────
    # Collect header context once for description retry calls
    dept_data   = header.get("department") or {}
    vendor_data = header.get("vendor_name") or {}
    date_data   = header.get("invoice_date") or {}
    department   = dept_data.get("value")   or "unknown"
    vendor       = vendor_data.get("value") or "unknown"
    invoice_date = date_data.get("value")   or "unknown"

    for i in range(line_count):
        line_result = parse_invoice_line(sid, i)

        if not line_result.get("success"):
            # Description extraction failed — flag and continue (never halt)
            issues = line_result.get("issues", [])
            add_note(sid, f"Line {i}: description extraction failed — {issues}")
            continue

        confidence = line_result.get("confidence", 0.0)
        line_item  = line_result.get("line_item", {})

        if confidence < _CONF_RETRY:
            # Low confidence — retry description parse only (amount is already stored)
            raw_desc   = line_item.get("raw_description", "")
            raw_amount = line_item.get("raw_amount", "")
            retry_desc = parse_line_description(
                description=raw_desc,
                amount=raw_amount,
                invoice_date=str(invoice_date),
                department=str(department),
                vendor=str(vendor),
            )
            retry_conf = retry_desc.get("confidence", 0.0)
            add_note(
                sid,
                f"Line {i}: low confidence ({confidence:.2f}) — retried description parse, "
                f"new confidence: {retry_conf:.2f}"
            )

        # Note any ambiguity flags
        parsed_desc = line_item.get("parsed_description") or {}
        ambiguity   = parsed_desc.get("ambiguity_flags") or []
        if ambiguity:
            add_note(sid, f"Line {i}: ambiguous category — {ambiguity}")

    # ── STEP 4: STRUCTURAL VALIDATION ────────────────────────────────────────
    struct_result = validate_structure(sid)

    if struct_result.get("has_errors"):
        for issue in struct_result.get("issues", []):
            if issue.get("severity") == "ERROR":
                rule = issue.get("rule_name", "")
                retry_struct = validate_single_rule(sid, rule)
                if retry_struct.get("has_errors"):
                    add_note(
                        sid,
                        f"HALT: structural error persists after retry on rule '{rule}': "
                        f"{issue.get('message')}"
                    )
                    return {
                        "halted":   True,
                        "state_id": sid,
                        "reason":   "structural_validation_failed",
                        "rule":     rule,
                        "message":  issue.get("message", ""),
                    }

    # ── STEP 5: BUSINESS VALIDATION ──────────────────────────────────────────
    biz_result = validate_business_rules(sid)

    for issue in biz_result.get("issues", []):
        rule     = issue.get("rule_name", "")
        severity = issue.get("severity", "")
        message  = issue.get("message", "")

        if rule == "line_total_matches_header" and severity == "ERROR":
            add_note(sid, "HALT: AMOUNT_MISMATCH — line item sum does not match header total")
            return {
                "halted":   True,
                "state_id": sid,
                "reason":   "AMOUNT_MISMATCH",
            }

        if rule == "po_number_present":
            add_note(sid, "Invoice has no PO number — proceeding with FLAGGED_NO_PO status")

        if rule == "invoice_date_not_future":
            add_note(sid, f"Invoice date is in the future — proceeding with flag: {message}")

    # ── STEP 6: FINALIZE ─────────────────────────────────────────────────────
    compute_confidence(sid)
    result = finalize_invoice(sid)
    if not result.get("success"):
        return {
            "halted":   True,
            "state_id": sid,
            "reason":   "finalize_failed",
            "error":    result.get("error", "finalize_invoice returned success=False"),
        }
    return result
