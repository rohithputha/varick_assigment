"""
Validation tools — structural and business rule validation on parsed Invoice state.
"""
from __future__ import annotations

from decimal import Decimal
from datetime import date

from invoice_extraction.models import (
    FlagType,
    InvoiceFlag,
    InvoiceHeader,
    LineItem,
    ParsedField,
    Severity,
    ValidationIssue,
)
from invoice_extraction.validators import structural, business
from invoice_extraction.state import StateManager


# ---------------------------------------------------------------------------
# Helpers: rebuild domain objects from state dicts
# ---------------------------------------------------------------------------

def _dict_to_pf(d: dict | None, coerce=str) -> ParsedField | None:
    if d is None:
        return None
    value = d.get("value")
    if coerce is Decimal:
        try:
            value = Decimal(str(value))
        except Exception:
            value = Decimal("0")
    elif coerce is date:
        try:
            value = date.fromisoformat(str(value))
        except Exception:
            value = date(1900, 1, 1)
    else:
        value = coerce(value) if value is not None else ""
    return ParsedField(
        value=value,
        confidence=float(d.get("confidence", 0.0)),
        source=str(d.get("source", "EXTRACTED")),
        notes=str(d.get("notes", "")),
    )


def _rebuild_header(header_dict: dict) -> InvoiceHeader:
    return InvoiceHeader(
        vendor_name=    _dict_to_pf(header_dict.get("vendor_name"),    coerce=str),
        invoice_date=   _dict_to_pf(header_dict.get("invoice_date"),   coerce=date),
        total_amount=   _dict_to_pf(header_dict.get("total_amount"),   coerce=Decimal),
        po_number=      _dict_to_pf(header_dict.get("po_number"),      coerce=str),
        department=     _dict_to_pf(header_dict.get("department"),     coerce=str),
        currency=       _dict_to_pf(header_dict.get("currency"),       coerce=str)
                        or ParsedField("USD", 1.0, "DEFAULT"),
        invoice_number= _dict_to_pf(header_dict.get("invoice_number"), coerce=str),
    )


def _rebuild_line_items(line_dicts: list[dict]) -> list[LineItem]:
    items = []
    for d in line_dicts:
        if not d:
            continue
        amount_pf = _dict_to_pf(d.get("amount"), coerce=Decimal)
        if amount_pf is None:
            amount_pf = ParsedField(Decimal("0"), 0.0, "EXTRACTED")
        items.append(LineItem(
            line_number=     int(d.get("line_number", 0)),
            raw_description= str(d.get("raw_description", "")),
            raw_amount=      str(d.get("raw_amount", "")),
            amount=          amount_pf,
            parsed_description=None,  # not needed for validation
        ))
    return items


def _issue_to_dict(issue: ValidationIssue) -> dict:
    return {
        "field_path": issue.field_path,
        "message":    issue.message,
        "severity":   issue.severity,
        "rule_name":  issue.rule_name,
    }


def _flag_to_dict(flag: InvoiceFlag) -> dict:
    return {
        "flag_type":  flag.flag_type,
        "severity":   flag.severity,
        "message":    flag.message,
        "line_index": flag.line_index,
        "field_path": flag.field_path,
    }


# ---------------------------------------------------------------------------
# validate_structure
# ---------------------------------------------------------------------------

def validate_structure(state_id: str) -> dict:
    """
    Run structural validators on the parsed invoice data in state.

    Checks:
      - required_header_fields
      - line_item_structure
      - amount_types

    Reads:  state.header, state.line_items
    Writes: state.structural_issues

    Returns:
        {
            "success": bool,
            "has_errors": bool,
            "has_warnings": bool,
            "issues": [{"field_path", "message", "severity", "rule_name"}, ...]
        }
    """
    try:
        state = StateManager.get(state_id)

        if not state.header:
            return {"success": False, "error": "header not parsed yet", "confidence": 0.0}

        header = _rebuild_header(state.header)
        line_items = _rebuild_line_items(state.line_items)

        all_issues: list[ValidationIssue] = []
        all_issues += structural.validate_required_header_fields(header)
        all_issues += structural.validate_line_item_structure(line_items)
        all_issues += structural.validate_amount_types(header, line_items)

        state.structural_issues = [_issue_to_dict(i) for i in all_issues]
        StateManager.update(state)

        has_errors   = any(i.severity == Severity.ERROR   for i in all_issues)
        has_warnings = any(i.severity == Severity.WARNING for i in all_issues)

        return {
            "success":      True,
            "has_errors":   has_errors,
            "has_warnings": has_warnings,
            "issues":       state.structural_issues,
            "confidence":   1.0,
        }

    except Exception as e:
        return {"success": False, "error": str(e), "confidence": 0.0}


# ---------------------------------------------------------------------------
# validate_business_rules
# ---------------------------------------------------------------------------

def validate_business_rules(state_id: str) -> dict:
    """
    Run business rule validators. Skips if structural errors are present.

    Checks:
      - line_total_matches_header
      - po_number_present
      - invoice_date_not_future
      - service_periods_sanity

    Reads:  state.header, state.line_items, state.structural_issues
    Writes: state.business_issues, state.flags, state.validation_passed,
            stage → VALIDATED (if no errors)

    Returns:
        {
            "success": bool,
            "has_errors": bool,
            "has_warnings": bool,
            "issues": [...],
            "flags_added": int,
            "confidence": 1.0,
        }
    """
    try:
        state = StateManager.get(state_id)

        if not state.header:
            return {"success": False, "error": "header not parsed yet", "confidence": 0.0}

        # Skip business validation if structural errors exist
        struct_errors = [
            i for i in state.structural_issues
            if i.get("severity") == Severity.ERROR
        ]
        if struct_errors:
            return {
                "success":      True,
                "skipped":      True,
                "reason":       "structural errors must be resolved first",
                "has_errors":   True,
                "has_warnings": False,
                "issues":       [],
                "flags_added":  0,
                "confidence":   0.0,
            }

        header = _rebuild_header(state.header)
        line_items = _rebuild_line_items(state.line_items)

        all_issues: list[ValidationIssue] = []
        all_flags:  list[InvoiceFlag]     = []

        for validator in [
            business.validate_line_total_matches_header,
            business.validate_po_number_present,
            business.validate_invoice_date_not_future,
            business.validate_service_periods_sanity,
        ]:
            if validator == business.validate_line_total_matches_header:
                issues, flags = validator(header, line_items)
            elif validator == business.validate_po_number_present:
                issues, flags = validator(header)
            elif validator == business.validate_invoice_date_not_future:
                issues, flags = validator(header)
            elif validator == business.validate_service_periods_sanity:
                issues, flags = validator(line_items)
            else:
                issues, flags = [], []

            all_issues += issues
            all_flags  += flags

        state.business_issues = [_issue_to_dict(i) for i in all_issues]

        # Add flags to state (dedup by flag_type + line_index)
        existing_keys = {
            (f.get("flag_type"), f.get("line_index"))
            for f in state.flags
        }
        new_flags = [
            _flag_to_dict(f) for f in all_flags
            if (f.flag_type, f.line_index) not in existing_keys
        ]
        state.flags += new_flags

        has_errors   = any(i.severity == Severity.ERROR   for i in all_issues)
        has_warnings = any(i.severity == Severity.WARNING for i in all_issues)

        state.validation_passed = not has_errors
        StateManager.update(state)

        # Advance stage: LINES_PARSED → VALIDATED (if no errors)
        # Also handle HEADER_PARSED → LINES_PARSED → VALIDATED for zero-line invoices
        current = StateManager.get(state_id).current_stage
        if not has_errors:
            if current == "HEADER_PARSED":
                # Zero-line invoice: skip directly through LINES_PARSED
                try:
                    StateManager.advance_stage(state_id, "LINES_PARSED")
                except ValueError:
                    pass
            if StateManager.get(state_id).current_stage == "LINES_PARSED":
                try:
                    StateManager.advance_stage(state_id, "VALIDATED")
                except ValueError:
                    pass

        return {
            "success":      True,
            "has_errors":   has_errors,
            "has_warnings": has_warnings,
            "issues":       state.business_issues,
            "flags_added":  len(new_flags),
            "confidence":   1.0,
        }

    except Exception as e:
        return {"success": False, "error": str(e), "confidence": 0.0}


# ---------------------------------------------------------------------------
# validate_single_rule
# ---------------------------------------------------------------------------

_RULE_MAP = {
    "required_header_fields": "structural",
    "line_item_structure":     "structural",
    "amount_types":            "structural",
    "line_total_matches_header": "business",
    "po_number_present":         "business",
    "invoice_date_not_future":   "business",
    "service_periods_sanity":    "business",
}


def validate_single_rule(state_id: str, rule_name: str) -> dict:
    """
    Re-run exactly one named validation rule. Does NOT change current_stage.
    Used by agent after fixing a specific field.

    Valid rule_name values:
      "required_header_fields" | "line_item_structure" | "amount_types" |
      "line_total_matches_header" | "po_number_present" |
      "invoice_date_not_future" | "service_periods_sanity"

    Returns:
        {
            "success": bool,
            "rule_name": str,
            "has_errors": bool,
            "issues": [...],
            "confidence": 1.0,
        }
    """
    try:
        if rule_name not in _RULE_MAP:
            return {
                "success":   False,
                "error":     f"Unknown rule_name: {rule_name!r}. "
                             f"Valid: {list(_RULE_MAP.keys())}",
                "confidence": 0.0,
            }

        state = StateManager.get(state_id)
        header     = _rebuild_header(state.header or {})
        line_items = _rebuild_line_items(state.line_items)

        issues: list[ValidationIssue] = []
        flags:  list[InvoiceFlag]     = []

        if rule_name == "required_header_fields":
            issues = structural.validate_required_header_fields(header)
        elif rule_name == "line_item_structure":
            issues = structural.validate_line_item_structure(line_items)
        elif rule_name == "amount_types":
            issues = structural.validate_amount_types(header, line_items)
        elif rule_name == "line_total_matches_header":
            issues, flags = business.validate_line_total_matches_header(header, line_items)
        elif rule_name == "po_number_present":
            issues, flags = business.validate_po_number_present(header)
        elif rule_name == "invoice_date_not_future":
            issues, flags = business.validate_invoice_date_not_future(header)
        elif rule_name == "service_periods_sanity":
            issues, flags = business.validate_service_periods_sanity(line_items)

        has_errors = any(i.severity == Severity.ERROR for i in issues)

        return {
            "success":    True,
            "rule_name":  rule_name,
            "has_errors": has_errors,
            "issues":     [_issue_to_dict(i) for i in issues],
            "confidence": 1.0,
        }

    except Exception as e:
        return {"success": False, "error": str(e), "rule_name": rule_name, "confidence": 0.0}
