"""
Structural validators — check that parsed Invoice objects have required fields
with the correct types. Pure functions, no LLM, no state access.
"""
from __future__ import annotations

from decimal import Decimal

from invoice_extraction.models import (
    InvoiceHeader,
    LineItem,
    ParsedField,
    Severity,
    ValidationIssue,
)


def validate_required_header_fields(header: InvoiceHeader) -> list[ValidationIssue]:
    """
    Rule: required_header_fields
    Check that vendor_name, invoice_date, total_amount are present and have
    non-zero confidence. currency must be present.
    """
    issues: list[ValidationIssue] = []

    required = [
        ("vendor_name",   header.vendor_name,   "header.vendor_name"),
        ("invoice_date",  header.invoice_date,   "header.invoice_date"),
        ("total_amount",  header.total_amount,   "header.total_amount"),
    ]

    for field_name, pf, path in required:
        if pf is None:
            issues.append(ValidationIssue(
                field_path=path,
                message=f"Required field '{field_name}' is missing",
                severity=Severity.ERROR,
                rule_name="required_header_fields",
            ))
        elif pf.confidence == 0.0:
            issues.append(ValidationIssue(
                field_path=path,
                message=f"Required field '{field_name}' could not be extracted (confidence=0.0)",
                severity=Severity.ERROR,
                rule_name="required_header_fields",
            ))

    # currency must be present
    if header.currency is None:
        issues.append(ValidationIssue(
            field_path="header.currency",
            message="Currency field is missing",
            severity=Severity.WARNING,
            rule_name="required_header_fields",
        ))

    return issues


def validate_line_item_structure(line_items: list[LineItem]) -> list[ValidationIssue]:
    """
    Rule: line_item_structure
    Each line item must have a non-empty raw_description.
    Amount must not have confidence=0.0 (signals parse failure).
    """
    issues: list[ValidationIssue] = []

    for i, line in enumerate(line_items):
        path_prefix = f"line_items[{i}]"

        if not line.raw_description or not line.raw_description.strip():
            issues.append(ValidationIssue(
                field_path=f"{path_prefix}.raw_description",
                message=f"Line {i}: raw_description is empty",
                severity=Severity.ERROR,
                rule_name="line_item_structure",
            ))

        if line.amount.confidence == 0.0:
            issues.append(ValidationIssue(
                field_path=f"{path_prefix}.amount",
                message=f"Line {i}: amount could not be parsed (confidence=0.0)",
                severity=Severity.WARNING,
                rule_name="line_item_structure",
            ))

    return issues


def validate_amount_types(header: InvoiceHeader, line_items: list[LineItem]) -> list[ValidationIssue]:
    """
    Rule: amount_types
    total_amount and all line item amounts must be Decimal (not float or int).
    Negative total amounts are flagged as WARNING.
    """
    issues: list[ValidationIssue] = []

    if header.total_amount and not isinstance(header.total_amount.value, Decimal):
        issues.append(ValidationIssue(
            field_path="header.total_amount",
            message=f"total_amount is {type(header.total_amount.value).__name__}, expected Decimal",
            severity=Severity.ERROR,
            rule_name="amount_types",
        ))
    elif header.total_amount and header.total_amount.value < 0:
        issues.append(ValidationIssue(
            field_path="header.total_amount",
            message=f"total_amount is negative: {header.total_amount.value}",
            severity=Severity.WARNING,
            rule_name="amount_types",
        ))

    for i, line in enumerate(line_items):
        if not isinstance(line.amount.value, Decimal):
            issues.append(ValidationIssue(
                field_path=f"line_items[{i}].amount",
                message=f"Line {i}: amount is {type(line.amount.value).__name__}, expected Decimal",
                severity=Severity.ERROR,
                rule_name="amount_types",
            ))

    return issues
