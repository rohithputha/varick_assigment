class Invoice:
    id: str                          # "INV-001", "UL-1", system-generated
    vendor_name: str
    invoice_date: date
    department: str                  # enum: Engineering/Legal/Marketing/Operations
    invoice_total: Decimal
    po_number: str | None            # nullable — INV-006
    service_period_start: date | None  # header-level — INV-004 only
    service_period_end: date | None
    status: str    # PENDING | READY_FOR_MATCHING | FLAGGED_* | APPROVED | POSTED
    received_at: datetime
    lines: List[InvoiceLine]
    flags: List[InvoiceFlag]

class InvoiceLine:
    id: str
    invoice_id: str
    line_number: int
    description: str                 # raw, preserved always
    amount: Decimal
    quantity: int | None             # extracted from description
    unit_cost: Decimal | None        # extracted or computed
    service_period_start: date | None  # extracted from description
    service_period_end: date | None
    billing_type: str | None         # "monthly" | "annual" | "usage-based" | None
    treatment: str | None            # set after ENRICH: EXPENSE/PREPAID/CAPITALIZE/ACCRUAL
    gl_account: str | None           # set after classification

class InvoiceFlag:
    invoice_id: str
    line_number: int | None          # None = invoice-level flag
    flag_type: str                   # MISSING_PO | AMOUNT_MISMATCH | AMBIGUOUS_TREATMENT | ...
    severity: str                    # ERROR | WARNING
    message: str
