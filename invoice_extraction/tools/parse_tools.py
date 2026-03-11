"""
Parse tools — parse invoice header and line items from state.
"""
from __future__ import annotations

from decimal import Decimal

from invoice_extraction.models import (
    FlagType,
    InvoiceFlag,
    InvoiceHeader,
    LineDescriptionResult,
    LineItem,
    ParsedField,
    Severity,
)
from invoice_extraction.parsers.simple.date_parser   import parse_invoice_date
from invoice_extraction.parsers.simple.amount_parser import parse_amount, parse_currency
from invoice_extraction.parsers.simple.po_parser     import parse_po_number
from invoice_extraction.parsers import description_extractor
from invoice_extraction.state import StateManager


# ---------------------------------------------------------------------------
# Serialization helpers (ParsedField / domain objects → JSON-safe dicts)
# ---------------------------------------------------------------------------

def _pf_to_dict(pf: ParsedField | None) -> dict | None:
    if pf is None:
        return None
    return {
        "value":      str(pf.value),   # Decimal/date → str for JSON
        "confidence": pf.confidence,
        "source":     pf.source,
        "notes":      pf.notes,
    }


def _header_to_dict(h: InvoiceHeader) -> dict:
    return {
        "vendor_name":    _pf_to_dict(h.vendor_name),
        "invoice_date":   _pf_to_dict(h.invoice_date),
        "total_amount":   _pf_to_dict(h.total_amount),
        "po_number":      _pf_to_dict(h.po_number),
        "department":     _pf_to_dict(h.department),
        "currency":       _pf_to_dict(h.currency),
        "invoice_number": _pf_to_dict(h.invoice_number),
    }


def _desc_result_to_dict(r: LineDescriptionResult) -> dict:
    return {
        "quantity":               r.quantity,
        "unit_cost":              r.unit_cost,
        "quantity_source":        r.quantity_source,
        "billing_type":           r.billing_type,
        "billing_confidence":     r.billing_confidence,
        "service_period_start":   r.service_period_start,
        "service_period_end":     r.service_period_end,
        "service_period_days":    r.service_period_days,
        "period_source":          r.period_source,
        "category_hint":          r.category_hint,
        "category_confidence":    r.category_confidence,
        "service_precedes_invoice": r.service_precedes_invoice,
        "ambiguity_flags":        r.ambiguity_flags,
        "reasoning":              r.reasoning,
        "overall_confidence":     r.overall_confidence,
        "raw_description":        r.raw_description,
    }


def _line_to_dict(line: LineItem) -> dict:
    return {
        "line_number":        line.line_number,
        "raw_description":    line.raw_description,
        "raw_amount":         line.raw_amount,
        "amount":             _pf_to_dict(line.amount),
        "parsed_description": _desc_result_to_dict(line.parsed_description)
                              if line.parsed_description else None,
    }


# ---------------------------------------------------------------------------
# parse_invoice_header
# ---------------------------------------------------------------------------

def parse_invoice_header(state_id: str) -> dict:
    """
    Parse the invoice header from raw_input using pure Python parsers (no LLM).

    Reads:  state.raw_input
    Writes: state.header, state.field_confidences (header.* keys), stage → HEADER_PARSED

    Returns:
        {
            "success": bool,
            "header": dict,
            "confidence": float,
            "issues": [list of low-confidence field names],
        }
    """
    try:
        state = StateManager.get(state_id)
        raw = state.raw_input or {}

        # --- vendor_name ---
        vendor_raw = raw.get("vendor_name") or raw.get("vendor") or ""
        vendor_pf = ParsedField(
            value=str(vendor_raw).strip(),
            confidence=1.0 if vendor_raw else 0.0,
            source="EXPLICIT" if vendor_raw else "EXTRACTED",
            notes="" if vendor_raw else "vendor_name not found in raw input",
        )

        # --- invoice_date ---
        date_raw = raw.get("invoice_date") or raw.get("date") or ""
        date_pf = parse_invoice_date(str(date_raw))

        # --- total_amount ---
        amount_raw = raw.get("total_amount") or raw.get("amount") or raw.get("total") or ""
        total_pf = parse_amount(str(amount_raw))

        # --- po_number ---
        po_raw = raw.get("po_number") or raw.get("purchase_order") or None
        po_pf = parse_po_number(str(po_raw) if po_raw is not None else None)

        # --- department ---
        dept_raw = raw.get("department") or raw.get("dept") or None
        dept_pf = ParsedField(
            value=str(dept_raw).strip(),
            confidence=1.0,
            source="EXPLICIT",
        ) if dept_raw else None

        # --- currency ---
        currency_pf = parse_currency(str(amount_raw))

        # --- invoice_number ---
        inv_num_raw = raw.get("invoice_number") or raw.get("invoice_id") or None
        inv_num_pf = ParsedField(
            value=str(inv_num_raw).strip(),
            confidence=1.0,
            source="EXPLICIT",
        ) if inv_num_raw else None

        header = InvoiceHeader(
            vendor_name=vendor_pf,
            invoice_date=date_pf,
            total_amount=total_pf,
            po_number=po_pf,
            department=dept_pf,
            currency=currency_pf,
            invoice_number=inv_num_pf,
        )

        # Write to state
        state.header = _header_to_dict(header)

        # Record field confidences
        confidences = {
            "header.vendor_name":    vendor_pf.confidence,
            "header.invoice_date":   date_pf.confidence,
            "header.total_amount":   total_pf.confidence,
            "header.currency":       currency_pf.confidence,
        }
        if po_pf:
            confidences["header.po_number"] = po_pf.confidence
        if dept_pf:
            confidences["header.department"] = dept_pf.confidence
        if inv_num_pf:
            confidences["header.invoice_number"] = inv_num_pf.confidence

        state.field_confidences.update(confidences)
        StateManager.update(state)
        StateManager.advance_stage(state_id, "HEADER_PARSED")

        low_confidence_fields = [k for k, v in confidences.items() if v < 0.75]

        header_dict = _header_to_dict(header)
        overall = sum(confidences.values()) / len(confidences)

        return {
            "success":    True,
            "header":     header_dict,
            "confidence": overall,
            "issues":     low_confidence_fields,
        }

    except Exception as e:
        return {"success": False, "error": str(e), "confidence": 0.0}


# ---------------------------------------------------------------------------
# parse_invoice_line
# ---------------------------------------------------------------------------

def parse_invoice_line(state_id: str, line_index: int) -> dict:
    """
    Parse a single line item (amount + description) from raw_input.

    Amount: pure Python parser (deterministic).
    Description: 1 haiku call via description_extractor (with internal retries).

    Reads:  state.raw_input.line_items[line_index], state.header
    Writes: state.line_items[line_index], state.lines_parsed,
            state.field_confidences (line_items[N].*)

    Returns:
        {
            "success": bool,
            "line_index": int,
            "line_item": dict,
            "confidence": float,
            "issues": [list of issue strings],
        }
    """
    try:
        state = StateManager.get(state_id)
        raw_items = (state.raw_input or {}).get("line_items", [])

        if line_index < 0 or line_index >= len(raw_items):
            return {
                "success": False,
                "error": f"line_index {line_index} out of range (0..{len(raw_items)-1})",
                "line_index": line_index,
                "confidence": 0.0,
            }

        raw_item = raw_items[line_index]
        raw_description = str(raw_item.get("description") or "").strip()
        raw_amount_str  = str(raw_item.get("amount") or "0").strip()

        # Parse amount (deterministic)
        amount_pf = parse_amount(raw_amount_str)

        # Build description_extractor context from header
        header = state.header or {}
        dept_pf   = header.get("department")
        vendor_pf = header.get("vendor_name")
        date_pf   = header.get("invoice_date")

        department   = dept_pf["value"]   if dept_pf   else "unknown"
        vendor       = vendor_pf["value"] if vendor_pf else "unknown"
        invoice_date = date_pf["value"]   if date_pf   else "unknown"

        # Parse description (LLM — 1 haiku call with internal retries)
        desc_result = description_extractor.parse_line_description(
            description=raw_description,
            amount=raw_amount_str,
            invoice_date=invoice_date,
            department=department,
            vendor=vendor,
        )

        line = LineItem(
            line_number=line_index + 1,
            raw_description=raw_description,
            raw_amount=raw_amount_str,
            amount=amount_pf,
            parsed_description=desc_result,
        )

        line_dict = _line_to_dict(line)

        # Write to state
        StateManager.set_line_item(state_id, line_index, line_dict)

        # Update field confidences
        state = StateManager.get(state_id)
        prefix = f"line_items[{line_index}]"
        state.field_confidences[f"{prefix}.amount"] = amount_pf.confidence
        if desc_result:
            state.field_confidences[f"{prefix}.desc.category"]     = desc_result.category_confidence
            state.field_confidences[f"{prefix}.desc.billing"]      = desc_result.billing_confidence
            state.field_confidences[f"{prefix}.desc.overall"]      = desc_result.overall_confidence
        StateManager.update(state)

        issues = []
        success = True

        # description_extractor degraded result check
        if desc_result and desc_result.overall_confidence == 0.0:
            success = False
            issues.append(f"description extraction failed: {desc_result.ambiguity_flags}")
            # Add EXTRACTION_FAILED flag
            StateManager.add_flag(state_id, {
                "flag_type":  FlagType.EXTRACTION_FAILED,
                "severity":   Severity.WARNING,
                "message":    f"Line {line_index}: description extraction failed",
                "line_index": line_index,
                "field_path": f"line_items[{line_index}].parsed_description",
            })

        confidence = (amount_pf.confidence + (desc_result.overall_confidence if desc_result else 0.0)) / 2

        # Advance to LINES_PARSED when all lines have been processed
        state = StateManager.get(state_id)
        if (state.current_stage == "HEADER_PARSED"
                and state.lines_parsed >= state.lines_expected):
            try:
                StateManager.advance_stage(state_id, "LINES_PARSED")
            except ValueError:
                pass  # already advanced

        return {
            "success":    success,
            "line_index": line_index,
            "line_item":  line_dict,
            "confidence": confidence,
            "issues":     issues,
        }

    except Exception as e:
        return {"success": False, "error": str(e), "line_index": line_index, "confidence": 0.0}


# ---------------------------------------------------------------------------
# parse_line_description (stateless)
# ---------------------------------------------------------------------------

def parse_line_description(
    description:  str,
    amount:       str,
    invoice_date: str,
    department:   str,
    vendor:       str,
) -> dict:
    """
    STATELESS — directly calls description_extractor without touching state.
    Used by agent to re-parse a description after a low-confidence result,
    without re-running the full parse_invoice_line (which also re-parses amount).

    Returns:
        {
            "success": bool,
            "quantity": ...,
            "unit_cost": ...,
            "billing_type": ...,
            "service_period": {"start": ..., "end": ..., "days": ...},
            "category_hint": ...,
            "ambiguity_flags": [...],
            "service_precedes_invoice": bool,
            "reasoning": str,
            "confidence": float,
        }
    """
    try:
        result = description_extractor.parse_line_description(
            description=description,
            amount=amount,
            invoice_date=invoice_date,
            department=department,
            vendor=vendor,
        )
        return {
            "success":                  result.overall_confidence > 0.0,
            "quantity":                 result.quantity,
            "unit_cost":                result.unit_cost,
            "quantity_source":          result.quantity_source,
            "billing_type":             result.billing_type,
            "billing_confidence":       result.billing_confidence,
            "service_period": {
                "start": result.service_period_start,
                "end":   result.service_period_end,
                "days":  result.service_period_days,
            },
            "period_source":            result.period_source,
            "category_hint":            result.category_hint,
            "category_confidence":      result.category_confidence,
            "ambiguity_flags":          result.ambiguity_flags,
            "service_precedes_invoice": result.service_precedes_invoice,
            "reasoning":                result.reasoning,
            "confidence":               result.overall_confidence,
        }
    except Exception as e:
        return {"success": False, "error": str(e), "confidence": 0.0}
