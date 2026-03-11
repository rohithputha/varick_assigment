"""
Shared domain models for the Varick AP automation pipeline.

Used by:
  - invoice_extraction  (ParsedField, InvoiceHeader, LineItem, Invoice, …)
  - po_matching         (POMatchStatus, POMatchResult)
  - rules_engine        (LineSignals, GLClassificationResult, ClassifiedInvoice)
  - gl_classification   (LineSignals, GLClassificationResult, ClassifiedInvoice)
  - pipeline            (imports Invoice / POMatchResult for stage I/O)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Generic, TypeVar

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Enums — invoice domain
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    ERROR   = "ERROR"
    WARNING = "WARNING"
    INFO    = "INFO"


class FlagType(str, Enum):
    MISSING_PO         = "MISSING_PO"
    AMOUNT_MISMATCH    = "AMOUNT_MISMATCH"
    LOW_CONFIDENCE     = "LOW_CONFIDENCE"
    AMBIGUOUS_CATEGORY = "AMBIGUOUS_CATEGORY"
    EXTRACTION_FAILED  = "EXTRACTION_FAILED"
    MISSING_DATA       = "MISSING_DATA"
    DATE_FUTURE        = "DATE_FUTURE"


class InvoiceStatus(str, Enum):
    READY_FOR_MATCHING      = "READY_FOR_MATCHING"
    FLAGGED_NO_PO           = "FLAGGED_NO_PO"
    FLAGGED_AMOUNT_MISMATCH = "FLAGGED_AMOUNT_MISMATCH"
    FLAGGED_AMBIGUOUS       = "FLAGGED_AMBIGUOUS"
    FLAGGED_MISSING_DATA    = "FLAGGED_MISSING_DATA"
    FAILED                  = "FAILED"


# ---------------------------------------------------------------------------
# Enums — PO matching
# ---------------------------------------------------------------------------

class POMatchStatus(str, Enum):
    MATCHED        = "MATCHED"         # PO present, valid format (v1) / fully verified (v2)
    NO_PO          = "NO_PO"           # po_number is None in invoice header
    INVALID_FORMAT = "INVALID_FORMAT"  # po_number present but does not start with "PO"
    # v2 statuses (not implemented yet):
    # NOT_FOUND       = "NOT_FOUND"
    # AMOUNT_EXCEEDED = "AMOUNT_EXCEEDED"
    # VENDOR_MISMATCH = "VENDOR_MISMATCH"
    # DUPLICATE       = "DUPLICATE"


# ---------------------------------------------------------------------------
# ParsedField[T]
# ---------------------------------------------------------------------------

@dataclass
class ParsedField(Generic[T]):
    """Generic wrapper for any extracted field. Carries value + extraction metadata."""
    value:      T
    confidence: float    # 0.0–1.0
    source:     str      # "EXPLICIT" | "EXTRACTED" | "INFERRED" | "DEFAULT"
    notes:      str = "" # "" if none


# ---------------------------------------------------------------------------
# Validation / Flag types
# ---------------------------------------------------------------------------

@dataclass
class ValidationIssue:
    field_path: str   # e.g. "header.po_number" | "line_items[2].amount"
    message:    str
    severity:   Severity
    rule_name:  str   # e.g. "po_number_present"


@dataclass
class InvoiceFlag:
    flag_type:  FlagType
    severity:   Severity
    message:    str
    line_index: int | None   # None for header-level flags
    field_path: str | None


# ---------------------------------------------------------------------------
# Invoice domain objects
# ---------------------------------------------------------------------------

@dataclass
class InvoiceHeader:
    vendor_name:    ParsedField[str]
    invoice_date:   ParsedField[date]
    total_amount:   ParsedField[Decimal]
    po_number:      ParsedField[str] | None    # None → MISSING_PO flag
    department:     ParsedField[str] | None    # None if not extractable
    currency:       ParsedField[str]           # default "USD"
    invoice_number: ParsedField[str] | None


@dataclass
class LineDescriptionResult:
    """
    Output of description_extractor.parse_line_description().
    Carries classification signals for the GL Classification stage.
    Ingestion extracts these signals but does NOT act on them.
    """
    # Quantity / unit cost
    quantity:              int | None
    unit_cost:             str | None        # Decimal-compatible string e.g. "1800.00"
    quantity_source:       str               # "explicit_pattern" | "inferred" | "not_present"

    # Billing type
    billing_type:          str               # "annual"|"monthly"|"usage-based"|"one-time"|"unknown"
    billing_confidence:    float

    # Service period
    service_period_start:  str | None        # ISO date string
    service_period_end:    str | None
    service_period_days:   int | None        # derived from start/end
    period_source:         str               # "explicit_range"|"inferred_from_keyword"|"not_present"

    # Classification hint
    category_hint:         str               # "equipment"|"software"|"cloud"|...
    category_confidence:   float

    # Accrual signal
    service_precedes_invoice: bool

    # Ambiguity
    ambiguity_flags:       list[str]
    reasoning:             str

    # Meta
    overall_confidence:    float
    raw_description:       str               # original, always preserved


@dataclass
class LineItem:
    line_number:         int
    raw_description:     str
    raw_amount:          str
    amount:              ParsedField[Decimal]
    parsed_description:  LineDescriptionResult | None    # None if extraction failed


@dataclass
class Invoice:
    """Contract boundary output from the Invoice Ingestion module."""
    invoice_id:         str
    raw_data:           dict
    header:             InvoiceHeader
    line_items:         list[LineItem]
    status:             InvoiceStatus
    flags:              list[InvoiceFlag]
    overall_confidence: float
    processing_notes:   list[str]
    state_id:           str


# ---------------------------------------------------------------------------
# PO Matching result
# ---------------------------------------------------------------------------

@dataclass
class POMatchResult:
    invoice_id: str
    po_number:  str | None      # raw PO number from invoice header, or None
    status:     POMatchStatus
    matched:    bool            # True only for MATCHED
    notes:      list[str]       # agent notes / mismatch details
    confidence: float           # 1.0 for v1 (deterministic)


# # ---------------------------------------------------------------------------
# # AmortizationEntry (shared contract for downstream stages)
# # ---------------------------------------------------------------------------

# @dataclass
# class AmortizationEntry:
#     """Computed by Prepaid/Accrual Recognition stage, not this module."""
#     period_label: str
#     period_start: date
#     period_end:   date
#     amount:       Decimal


# ---------------------------------------------------------------------------
# GL Classification — rules engine input/output
# ---------------------------------------------------------------------------

@dataclass
class LineSignals:
    """
    Per-line signals fed into the GL rule engine.
    Assembled by gl_classification from LineItem + LineDescriptionResult.

    String fields are lower-cased and stripped before rule evaluation.
    """
    line_number:     int
    category_hint:   str           # from LineDescriptionResult
    billing_type:    str           # from LineDescriptionResult
    unit_cost:       float | None  # from LineDescriptionResult.unit_cost (decimal string → float)
    line_amount:     float | None  # from LineItem.amount.value (Decimal → float)
    ambiguity_flags: list[str]     # from LineDescriptionResult


@dataclass
class GLClassificationResult:
    """Rule engine output for a single invoice line."""
    line_number:          int
    gl_account:           str | None   # e.g. "5000", "1500"
    treatment:            str | None   # "EXPENSE" | "CAPITALIZE" | "PREPAID"
    base_expense_account: str | None   # only set for PREPAID treatment
    confidence:           float
    flagged:              bool         # True if no rule matched or ambiguity present
    flag_reason:          str | None
    rules_version:        str          # rules.json["version"] at classification time


@dataclass
class ClassifiedInvoice:
    """Full GL classification result for one invoice."""
    invoice_id:    str
    rules_version: str
    line_results:  list[GLClassificationResult]
