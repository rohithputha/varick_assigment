"""
Business rule validators — check business logic constraints on parsed Invoice objects.
Pure functions, no LLM, no state access.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from models import (
    FlagType,
    InvoiceFlag,
    InvoiceHeader,
    LineItem,
    Severity,
    ValidationIssue,
)


def validate_line_total_matches_header(
    header: InvoiceHeader,
    line_items: list[LineItem],
) -> tuple[list[ValidationIssue], list[InvoiceFlag]]:
    """
    Rule: line_total_matches_header
    Sum of line item amounts must equal header.total_amount.
    Tolerance: $0.02 (rounding).

    Returns (issues, flags). AMOUNT_MISMATCH is an ERROR.
    """
    issues: list[ValidationIssue] = []
    flags:  list[InvoiceFlag]     = []

    if not line_items or header.total_amount is None:
        return issues, flags

    line_sum = sum(
        line.amount.value
        for line in line_items
        if isinstance(line.amount.value, Decimal)
    )
    expected = header.total_amount.value

    tolerance = Decimal("0.02")
    if abs(line_sum - expected) > tolerance:
        msg = (
            f"Line item total ({line_sum}) does not match "
            f"header total ({expected}); diff={line_sum - expected}"
        )
        issues.append(ValidationIssue(
            field_path="header.total_amount",
            message=msg,
            severity=Severity.ERROR,
            rule_name="line_total_matches_header",
        ))
        flags.append(InvoiceFlag(
            flag_type=FlagType.AMOUNT_MISMATCH,
            severity=Severity.ERROR,
            message=msg,
            line_index=None,
            field_path="header.total_amount",
        ))

    return issues, flags


def validate_po_number_present(
    header: InvoiceHeader,
) -> tuple[list[ValidationIssue], list[InvoiceFlag]]:
    """
    Rule: po_number_present
    A missing PO number is a WARNING, not an ERROR (some invoices are PO-less).
    """
    issues: list[ValidationIssue] = []
    flags:  list[InvoiceFlag]     = []

    if header.po_number is None:
        msg = "Invoice has no PO number"
        issues.append(ValidationIssue(
            field_path="header.po_number",
            message=msg,
            severity=Severity.WARNING,
            rule_name="po_number_present",
        ))
        flags.append(InvoiceFlag(
            flag_type=FlagType.MISSING_PO,
            severity=Severity.WARNING,
            message=msg,
            line_index=None,
            field_path="header.po_number",
        ))

    return issues, flags


def validate_invoice_date_not_future(
    header: InvoiceHeader,
    today: date | None = None,
) -> tuple[list[ValidationIssue], list[InvoiceFlag]]:
    """
    Rule: invoice_date_not_future
    invoice_date must not be in the future. WARNING only (could be pre-dated).
    """
    issues: list[ValidationIssue] = []
    flags:  list[InvoiceFlag]     = []

    if today is None:
        today = date.today()

    if header.invoice_date and header.invoice_date.confidence > 0.0:
        if header.invoice_date.value > today:
            msg = f"Invoice date {header.invoice_date.value} is in the future"
            issues.append(ValidationIssue(
                field_path="header.invoice_date",
                message=msg,
                severity=Severity.WARNING,
                rule_name="invoice_date_not_future",
            ))
            flags.append(InvoiceFlag(
                flag_type=FlagType.DATE_FUTURE,
                severity=Severity.WARNING,
                message=msg,
                line_index=None,
                field_path="header.invoice_date",
            ))

    return issues, flags


def validate_service_periods_sanity(
    line_items: list[LineItem],
) -> tuple[list[ValidationIssue], list[InvoiceFlag]]:
    """
    Rule: service_periods_sanity
    For each line item with service_period_start and service_period_end,
    end must be >= start. WARNING on invalid ranges.
    """
    issues: list[ValidationIssue] = []
    flags:  list[InvoiceFlag]     = []

    for i, line in enumerate(line_items):
        if line.parsed_description is None:
            continue

        start_str = line.parsed_description.service_period_start
        end_str   = line.parsed_description.service_period_end

        if not start_str or not end_str:
            continue

        try:
            s = date.fromisoformat(start_str)
            e = date.fromisoformat(end_str)
        except ValueError:
            issues.append(ValidationIssue(
                field_path=f"line_items[{i}].parsed_description.service_period",
                message=f"Line {i}: invalid service period date format",
                severity=Severity.WARNING,
                rule_name="service_periods_sanity",
            ))
            continue

        if e < s:
            msg = f"Line {i}: service_period_end ({end_str}) < service_period_start ({start_str})"
            issues.append(ValidationIssue(
                field_path=f"line_items[{i}].parsed_description.service_period",
                message=msg,
                severity=Severity.WARNING,
                rule_name="service_periods_sanity",
            ))

    return issues, flags
