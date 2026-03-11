"""
Shared domain models for the Varick AP automation pipeline.

Central models — all cross-module contract types are defined here.
Each stage imports only what it needs; no per-module models.py for contract types.

Used by:
  - invoice_extraction  (ParsedField, InvoiceHeader, LineItem, Invoice,
                         LineDescriptionResult, ValidationIssue, InvoiceFlag, …)
  - po_matching         (POMatchStatus, POMatchResult)
  - gl_classification   (TreatmentType, LineSignals, GLClassificationResult, ClassifiedInvoice)
  - prepaid_accrual     (TreatmentType, ClassifiedInvoice, AmortizationEntry — future)
  - approval_routing    (ClassifiedInvoice — future)
  - pipeline            (Invoice, POMatchResult, ClassifiedInvoice for stage I/O)
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
# Enums — GL Classification / treatment types
# ---------------------------------------------------------------------------

class TreatmentType(str, Enum):
    EXPENSE    = "EXPENSE"      # cost recognised in current period → 5xxx accounts
    PREPAID    = "PREPAID"      # annual software/cloud paid upfront → 1310/1300 accounts
    CAPITALIZE = "CAPITALIZE"   # long-lived asset → 1500, depreciate over useful life
    ACCRUAL    = "ACCRUAL"      # service received before invoice → 21xx accounts
                                # Note: only assigned by Prepaid/Accrual Recognition (Step 3)
                                # GL Classification (Step 2) never assigns ACCRUAL


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
    prompt_version:        str               # prompts.json["version"] at extraction time — for feedback


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


# ---------------------------------------------------------------------------
# Prepaid / Accrual Recognition types
# ---------------------------------------------------------------------------

@dataclass
class AmortizationEntry:
    """One monthly amortization installment for a PREPAID line."""
    period_label:    str      # "2026-01", "2026-02", …
    period_start:    date     # First calendar day of the period
    period_end:      date     # Last calendar day of the period
    amount:          Decimal  # Debit expense / credit prepaid asset this period
    prepaid_account: str      # Asset account to credit (e.g., "1310")
    expense_account: str      # Expense account to debit (e.g., "5010")


@dataclass
class PrepaidLineResult:
    """Prepaid recognition result for a single line item."""
    line_number:             int
    prepaid_account:         str              # "1320" (insurance) or inherited "1310"/"1300"
    expense_account:         str              # where monthly amortization posts
    total_amount:            Decimal
    service_period_start:    date
    service_period_end:      date
    amortization_months:     int
    amortization_entries:    list[AmortizationEntry]
    is_reclassified:         bool             # True if Step 3 overrode GL Classification
    reclassification_reason: str | None


@dataclass
class AccrualLineResult:
    """Accrual recognition result for a single line item."""
    line_number:        int
    accrual_account:    str    # "2110" professional services, "2100" all others
    expense_account:    str    # GL account the expense belongs to (from GL Classification)
    amount:             Decimal
    service_period_end: str    # ISO date — when service ended (drives accrual period)
    reversal_trigger:   str    # "on_payment" — always in v1


@dataclass
class RecognizedLineResult:
    """
    Full prepaid/accrual recognition result for one invoice line.
    Exactly one of prepaid_result / accrual_result is non-None for active lines,
    or neither for pass-through (no_action_required) and skipped lines.
    """
    line_number:         int
    original_treatment:  str | None    # TreatmentType from GL Classification
    final_treatment:     str | None    # Overridden by Step 3 or same as original
    original_gl_account: str | None    # gl_account from GL Classification
    final_gl_account:    str | None    # Overridden for insurance PREPAID and ACCRUAL
    prepaid_result:      PrepaidLineResult | None
    accrual_result:      AccrualLineResult | None
    no_action_required:  bool          # True for EXPENSE lines with no overrides
    skipped:             bool          # True if GL Classification flagged or CAPITALIZE
    skip_reason:         str | None    # "gl_flagged" | "capitalize_not_in_scope"
    notes:               list[str]     # per-line processing notes


@dataclass
class RecognizedInvoice:
    """
    Full prepaid/accrual recognition result for one invoice.
    Contract boundary passed to the Approval Routing stage.
    Carries the full ClassifiedInvoice so downstream stages never re-fetch.
    """
    classified_invoice: ClassifiedInvoice   # input, unmodified
    line_results:        list[RecognizedLineResult]
    has_prepaid_lines:   bool
    has_accrual_lines:   bool
    prepaid_line_count:  int
    accrual_line_count:  int
    all_processed:       bool     # False if any line was skipped due to gl_flagged
    notes:               list[str]


# ---------------------------------------------------------------------------
# GL Classification — rules engine input/output
# ---------------------------------------------------------------------------

@dataclass
class LineSignals:
    """
    Per-line signals fed into the GL rule engine (gl_classification/classifier/sop.py).
    Assembled by gl_classification from LineItem + LineDescriptionResult.
    Defined here (central models.py) for consistency; imported by sop.py.

    String fields are lower-cased and stripped before rule evaluation.
    Numeric fields use float (not Decimal) for direct comparison with JSON rule values.
    """
    line_number:     int
    category_hint:   str           # from LineDescriptionResult
    billing_type:    str           # from LineDescriptionResult
    unit_cost:       float | None  # from LineDescriptionResult.unit_cost (decimal string → float)
    line_amount:     float | None  # from LineItem.amount.value (Decimal → float)
    ambiguity_flags: list[str]     # from LineDescriptionResult
    reasoning:       str           # from LineDescriptionResult.reasoning — passed through for agent notes


@dataclass
class GLClassificationResult:
    """Rule engine output for a single invoice line."""
    line_number:          int
    gl_account:           str | None          # e.g. "5000", "1500", None if flagged
    treatment:            TreatmentType | None # EXPENSE/PREPAID/CAPITALIZE assigned by Step 2
                                               # ACCRUAL is only assigned by Step 3
    base_expense_account: str | None          # PREPAID lines only: e.g. 1310 amortises to 5010
    confidence:           float               # 1.0 clean match; 0.85 fallback; 0.0 flagged
    reasoning:            str                 # e.g. "annual software → PREPAID 1310 (rule3_software_prepaid)"
    applied_rule:         str                 # rule id: "rule3_software_prepaid" | "no_rule_matched" | ...
    rules_version:        str                 # rules.json["version"] at classification time — for feedback
    flagged:              bool                # True if no rule matched or ambiguity_flags present
    flag_reason:          str | None          # "ambiguous: could be legal or consulting" | "no_rule_matched"


@dataclass
class ClassifiedInvoice:
    """
    Full GL classification result for one invoice.
    Contract boundary passed to the Prepaid/Accrual Recognition stage.
    Carries the full Invoice and POMatchResult so downstream stages never re-fetch.
    """
    invoice:              Invoice                    # original Invoice from ingestion, unmodified
    po_match:             POMatchResult              # from PO Matching stage
    line_classifications: list[GLClassificationResult]
    all_classified:       bool                       # False if any line has flagged=True
    flagged_lines:        list[int]                  # line_numbers requiring human review
    overall_confidence:   float                      # mean of per-line confidences (unweighted)
    notes:                list[str]                  # agent processing notes


# ---------------------------------------------------------------------------
# Approval Routing types
# ---------------------------------------------------------------------------

class ApprovalOutcome(str, Enum):
    """Four possible routing decisions for an invoice."""
    AUTO_APPROVE = "AUTO_APPROVE"   # no human action required
    DEPT_MANAGER = "DEPT_MANAGER"   # department manager sign-off required
    VP_FINANCE   = "VP_FINANCE"     # VP Finance sign-off required
    DENY         = "DENY"           # rejected — fail-closed safety net


@dataclass
class ApprovalRoutingResult:
    """Routing decision for one invoice."""
    outcome:              ApprovalOutcome
    applied_rule:         str        # rule id that fired:
                                     # "capitalize_override" | "engineering_override" |
                                     # "marketing_override" | "base_auto" |
                                     # "dept_manager_base" | "vp_finance_base" |
                                     # "dept_manager_gl_flagged_override" |
                                     # "fail_closed_deny"
    total_amount:         Decimal    # invoice total used for routing
    department:           str | None # department from invoice header
    has_capitalize:       bool       # True if any non-skipped line has CAPITALIZE treatment
    all_lines_classified: bool       # False if GL Classification flagged any lines
    reasoning:            str        # human-readable explanation of the decision


@dataclass
class RoutedInvoice:
    """
    Full approval routing result for one invoice.
    Contract boundary passed to the Posting stage.
    Carries the full RecognizedInvoice so downstream stages never re-fetch.
    """
    recognized_invoice: RecognizedInvoice    # full upstream payload, unmodified
    routing:            ApprovalRoutingResult
    notes:              list[str]


# ---------------------------------------------------------------------------
# Post & Verify types
# ---------------------------------------------------------------------------

class PostingStatus(str, Enum):
    POSTED           = "POSTED"            # all lines posted, amounts reconciled
    PARTIAL_POSTED   = "PARTIAL_POSTED"    # some lines skipped (gl_flagged)
    PENDING_APPROVAL = "PENDING_APPROVAL"  # halted — waiting for human approval
    REJECTED         = "REJECTED"          # routing outcome was DENY
    FAILED           = "FAILED"            # unexpected error


@dataclass
class JournalEntry:
    """One double-entry journal line."""
    entry_id:       str        # deterministic hash — sha256(run_id+invoice_id+line_number+entry_type+period_label)[:16]
    line_number:    int        # source line number from invoice
    entry_type:     str        # "expense" | "capitalize" | "prepaid_initial" |
                               # "prepaid_amortization" | "accrual" | "accrual_reversal"
    debit_account:  str        # GL account to debit
    credit_account: str        # GL account to credit
    amount:         Decimal
    period_label:   str        # "2026-03" for calendar entries; "on_payment" for pending reversals
    description:    str        # human-readable


@dataclass
class PostingResult:
    """
    Final pipeline output.
    Contains all journal entries, verification status, and the full upstream payload.
    """
    routed_invoice:     RoutedInvoice    # full upstream payload, unmodified
    posting_status:     PostingStatus
    journal_entries:    list[JournalEntry]
    total_posted:       Decimal           # sum of line amounts for posted lines
    total_invoice:      Decimal           # invoice header total_amount
    amounts_reconciled: bool              # total_posted == sum of non-skipped line amounts
    skipped_lines:      list[int]         # line_numbers not posted (gl_flagged)
    notes:              list[str]
